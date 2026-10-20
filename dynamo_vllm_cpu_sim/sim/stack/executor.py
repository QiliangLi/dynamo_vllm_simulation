"""SimExecutor：vLLM Executor 扩展点上的 mock 执行器（设计文档 §5.1，W2）。

经 ``--distributed-executor-backend sim.stack.executor.SimExecutor`` 限定名注入
真实 vLLM 进程（vLLM 零源码修改，Executor.get_class 的 qualname 分支）。

边界（设计文档 D2，与单进程 DES sim/engine.py 同一原则）：只替换
SchedulerOutput → 执行 → ModelRunnerOutput 这一段；不 mock schedule() 返回、
不重写 KV allocator、不绕过 update_from_output()。

时间模型与 DES Engine.schedule()/complete() 完全一致::

    T = step_overhead_s + prefill * prefill_token_s + decode * decode_token_s

壁钟模式（模式 1）以 time.sleep(T) 兑现；采样 token 恒为 SIM_TOKEN_ID
（与 DES 相同，ignore_eos 语义由请求侧 max_tokens 决定）。

请求表自愈机制：EngineCore.step() 只把 SchedulerOutput 交给 executor，executor
看不到 Request 对象。本类从 ``scheduled_new_reqs``（新请求：prompt 长度与初始
num_computed_tokens）与 ``scheduled_cached_reqs``（每步携带 scheduler 侧
post-advance 的 num_computed_tokens / num_output_tokens，scheduler.py 构建
SchedulerOutput 时读取）重建每请求的 computed/outputs 视图，因此抢占/重置/
KV 恢复后不会漂移。采样条件与 DES 相同：computed_after >= prompt_len + outputs。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import Future
from pathlib import Path

import torch

from vllm.v1.core.sched.output import SchedulerOutput
from vllm.v1.executor.abstract import Executor
from vllm.v1.kv_cache_interface import FullAttentionSpec
from vllm.v1.outputs import ModelRunnerOutput

ROOT = Path(__file__).resolve().parents[2]

SIM_TOKEN_ID = int(os.environ.get("SIM_STACK_TOKEN_ID", "100"))  # 默认与 DES 一致；
# GPT-2 词表中 100 是 UTF-8 部分字节，解码为空文本；演示可设 SIM_STACK_TOKEN_ID=61 等
# 可见 token，不影响调度（停止条件由请求侧 max_tokens 决定）。

# 进程内当前实例（后续 storage-sim 接线与测试用；每进程至多一个 EngineCore）。
CURRENT: "SimExecutor | None" = None


def load_stack_config() -> dict:
    """加载与 DES 同源的实验配置（configs/demo.json 风格）。"""
    path = os.environ.get("SIM_STACK_CONFIG", str(ROOT / "configs/demo.json"))
    with open(path) as f:
        cfg = json.load(f)
    for key in ("step_overhead_s", "prefill_token_s", "decode_token_s"):
        if key not in cfg.get("compute", {}):
            raise ValueError(f"config {path} lacks compute.{key}")
    return cfg


class SimExecutor(Executor):
    def _init_executor(self) -> None:
        global CURRENT
        self.cfg = load_stack_config()
        self.compute = self.cfg["compute"]
        self.block_size = self.cfg["block_size"]
        # worker 身份仅用于日志与后续 storage-sim 记账
        self.worker_id = int(os.environ.get("SIM_STACK_WORKER_ID", "0"))
        # 请求表：rid -> prompt_len（新请求事件时登记；outputs/computed 每步
        # 从 scheduled_cached_reqs 重新对齐，不跨步保存，避免抢占后漂移）
        self._prompt_len: dict[str, int] = {}
        self._base_computed: dict[str, int] = {}
        self.steps = 0
        self.sleep_s = 0.0
        # 可选逐步日志（A1 与 DES 对拍、观测）：SIM_STACK_STEP_LOG=<path>
        self._step_log_path = os.environ.get("SIM_STACK_STEP_LOG")
        self._step_log = None
        if self._step_log_path:
            self._step_log = open(self._step_log_path, "w", buffering=1)
        CURRENT = self

    # ------------------------------------------------------------------
    # EngineCore 初始化链（_initialize_kv_caches）所需
    # ------------------------------------------------------------------

    def determine_available_memory(self) -> list[int]:
        # 逻辑内存值；真实块数由 --num-gpu-blocks-override（与 DES 的
        # num_blocks 同值）钳制，保证与 DES 的 KVCacheConfig.num_blocks 一致。
        return [int(os.environ.get("SIM_STACK_AVAILABLE_BYTES", 1 << 40))]

    def get_kv_cache_specs(self) -> list[dict[str, FullAttentionSpec]]:
        # 与 DES Engine 相同的合成 spec：单层 FullAttention，1 head × size 1。
        return [
            {
                "layer": FullAttentionSpec(
                    block_size=self.block_size,
                    num_kv_heads=1,
                    head_size=1,
                    dtype=torch.float32,
                )
            }
        ]

    def initialize_from_config(self, kv_cache_config) -> None:
        self.kv_cache_config = kv_cache_config
        num_blocks = [c.num_blocks for c in kv_cache_config]
        if self.worker_id == 0:
            print(
                f"[SimExecutor] worker {self.worker_id} logical num_blocks={num_blocks}",
                flush=True,
            )

    # ------------------------------------------------------------------
    # 核心路径：EngineCore.step() -> execute_model(scheduler_output, non_block=True)
    # -> future.result() -> scheduler.update_from_output()
    # ------------------------------------------------------------------

    def execute_model(
        self, scheduler_output: SchedulerOutput, *args, non_block: bool = False, **kwargs
    ):
        out = scheduler_output

        # 1) 登记本步新请求（prompt 长度与初始 computed 来自 NewRequestData；
        #    其 num_computed_tokens 是 schedule 推进前的值）。
        for nd in out.scheduled_new_reqs:
            self._prompt_len[nd.req_id] = len(nd.prompt_token_ids or [])
            self._base_computed[nd.req_id] = nd.num_computed_tokens

        # 2) scheduler 真值视图（对齐索引；构建时在 schedule() 推进之后读取）。
        cached_computed = dict(
            zip(out.scheduled_cached_reqs.req_ids, out.scheduled_cached_reqs.num_computed_tokens)
        )
        cached_outputs = dict(
            zip(out.scheduled_cached_reqs.req_ids, out.scheduled_cached_reqs.num_output_tokens)
        )

        # 3) 逐请求决定采样与 prefill/decode 拆分（与 DES Engine.schedule 相同）。
        #    实测 0.20.2 语义：cached 与 new 的 num_computed_tokens 都是本步
        #    推进前的值，computed_after = before + count。
        ids: list[str] = []
        sampled: list[list[int]] = []
        prefill = decode = 0
        for rid, count in out.num_scheduled_tokens.items():
            ids.append(rid)
            prompt_len = self._prompt_len[rid]
            if rid in cached_computed:
                before = cached_computed[rid]
                outputs = cached_outputs[rid]
            else:
                before = self._base_computed[rid]
                outputs = 0
            computed = before + count
            p = min(count, max(0, prompt_len - before))
            prefill += p
            decode += count - p
            if computed >= prompt_len + outputs:
                sampled.append([SIM_TOKEN_ID])
            else:
                # 与 DES 一致：每请求恰好一个条目；未到采样点时为空
                # （chunked prefill 中间块不出 token）。
                sampled.append([])

        for rid in out.finished_req_ids:
            self._prompt_len.pop(rid, None)
            self._base_computed.pop(rid, None)

        model_output = ModelRunnerOutput(
            req_ids=ids,
            req_id_to_index={r: i for i, r in enumerate(ids)},
            sampled_token_ids=sampled,
        )
        if os.environ.get("SIM_STACK_DEBUG"):
            print(
                f"[SimExecutor] step {self.steps}: ids={ids} sampled={[bool(s) for s in sampled]} "
                f"prefill={prefill} decode={decode} new={[nd.req_id for nd in out.scheduled_new_reqs]}",
                flush=True,
            )

        # 4) 壁钟兑现计算时延（模式 1），再以已完成 Future 返回。
        if ids:
            c = self.compute
            duration = (
                c["step_overhead_s"]
                + prefill * c["prefill_token_s"]
                + decode * c["decode_token_s"]
            )
            if duration <= 0:
                raise ValueError("nonpositive compute time")
            time.sleep(duration)
            self.sleep_s += duration
            self.steps += 1
            if self._step_log is not None:
                self._step_log.write(
                    json.dumps(
                        {
                            "step": self.steps,
                            "t_wall": time.time(),
                            "req_ids": ids,
                            "num_scheduled_tokens": dict(out.num_scheduled_tokens),
                            "sampled": [bool(s) for s in sampled],
                            "prefill": prefill,
                            "decode": decode,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        future: Future = Future()
        future.set_result(model_output)
        return future

    def sample_tokens(self, grammar_output=None):
        # 仅当 execute_model 返回 None 时 EngineCore 才会调用；本执行器恒返回
        # 非 None，此方法不应被触达。
        raise RuntimeError("SimExecutor.sample_tokens must not be called")

    # ------------------------------------------------------------------
    # 其余 Executor 协议（抽象方法 + 默认覆盖）
    # ------------------------------------------------------------------

    def collective_rpc(self, method: str, *args, timeout=None, **kwargs):
        if method == "get_supported_tasks":
            # 基类 supported_tasks 经此 RPC 取值；本执行器只承载生成类任务。
            return [("generate",)]
        return [None]

    def check_health(self) -> None:
        return None

    def shutdown(self) -> None:
        if self._step_log is not None:
            self._step_log.close()
            self._step_log = None
        if self.worker_id == 0:
            print(
                f"[SimExecutor] worker {self.worker_id} steps={self.steps} "
                f"compute_sleep_s={self.sleep_s:.6f}",
                flush=True,
            )
