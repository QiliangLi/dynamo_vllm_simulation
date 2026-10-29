import importlib.util
from pathlib import Path
import sys
import torch
from sim.compute_time import estimate_step
from sim.control_events import ControlEvent
import vllm.platforms
from vllm.platforms.cpu import CpuPlatform

# Only device discovery is overridden; the scheduler and KV allocator are real.
vllm.platforms._current_platform = CpuPlatform()
from vllm.config import (
    CacheConfig,
    DeviceConfig,
    KVTransferConfig,
    ModelConfig,
    ParallelConfig,
    SchedulerConfig,
    VllmConfig,
)
from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
from vllm.sampling_params import SamplingParams
from vllm.utils.hashing import sha256
from vllm.v1.core.kv_cache_utils import get_request_block_hasher, init_none_hash
from vllm.v1.core.sched.scheduler import Scheduler
from vllm.v1.kv_cache_interface import (
    FullAttentionSpec,
    KVCacheConfig,
    KVCacheGroupSpec,
)
from vllm.v1.outputs import KVConnectorOutput, ModelRunnerOutput
from vllm.v1.request import Request
from vllm.v1.structured_output import StructuredOutputManager

ROOT = Path(__file__).resolve().parents[1]
KVConnectorFactory.register_connector("SimConnector", "sim.connector", "SimConnector")
init_none_hash(sha256)


def scheduler_class(mode):
    if mode == "upstream":
        return Scheduler
    if mode not in ("ascend_default", "ascend_balance"):
        raise ValueError(mode)
    path = ROOT / "upstream/ascend/vllm_ascend/patch/platform/patch_balance_schedule.py"
    if mode == "ascend_balance":
        path = ROOT / "patched/ascend_balance_schedule.py"
        if not path.exists():
            raise RuntimeError(
                "Run scripts/apply_ascend_patch.py for this opt-in experiment"
            )
    name = "sim_" + mode
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].BalanceScheduler


def make_request(row, block_size, policy):
    priority = row.get("priority", 0)
    if policy == "priority":
        from sim.priority import edf_priority
        if 'deadline_s' not in row:
            raise ValueError('EDF request requires a frozen deadline_s')
        priority = edf_priority(row['deadline_s'])
    return Request(
        request_id=row["id"],
        prompt_token_ids=row["prompt_token_ids"],
        sampling_params=SamplingParams(
            max_tokens=row["output_tokens"], ignore_eos=True, temperature=0
        ),
        pooling_params=None,
        arrival_time=row["arrival_s"],
        priority=priority,
        block_hasher=get_request_block_hasher(block_size, sha256),
    )


class Engine:
    def __init__(self, worker_id, c, store):
        self.id = worker_id
        self.c = c
        self.store = store
        size = c["block_size"]
        model = ModelConfig(
            model=str(ROOT / "configs/model"),
            dtype="float32",
            max_model_len=c["max_model_len"],
            seed=0,
            skip_tokenizer_init=True,
            enforce_eager=True,
        )
        sc = SchedulerConfig(
            max_num_seqs=c["max_num_seqs"],
            max_num_batched_tokens=c["max_num_batched_tokens"],
            max_model_len=c["max_model_len"],
            enable_chunked_prefill=True,
            async_scheduling=False,
            policy=c["policy"],
            is_encoder_decoder=False,
        )
        cache = CacheConfig(
            block_size=size, enable_prefix_caching=c["enable_prefix_caching"]
        )
        cache.num_gpu_blocks = c["num_blocks"]
        vc = VllmConfig(
            model_config=model,
            scheduler_config=sc,
            cache_config=cache,
            device_config=DeviceConfig("cpu"),
            parallel_config=ParallelConfig(),
            kv_transfer_config=KVTransferConfig(
                kv_connector=c.get("connector_name", "SimConnector"), kv_role="kv_both"
            ),
            additional_config={
                "enable_balance_scheduling": c["scheduler"] == "ascend_balance"
            },
        )
        kc = KVCacheConfig(
            num_blocks=c["num_blocks"],
            kv_cache_tensors=[],
            kv_cache_groups=[
                KVCacheGroupSpec(
                    ["layer"],
                    FullAttentionSpec(
                        block_size=size,
                        num_kv_heads=1,
                        head_size=1,
                        dtype=torch.float32,
                    ),
                )
            ],
        )
        self.scheduler = scheduler_class(c["scheduler"])(
            vllm_config=vc,
            kv_cache_config=kc,
            structured_output_manager=StructuredOutputManager(vc),
            block_size=size,
            log_stats=False,
        )
        self.scheduler.connector.store = store
        self.inflight = None
        self.pending_recvs = set()
        self.compute_s = 0.0
        self.stall_s = 0.0
        self.idle_s = 0.0
        self.steps = 0
        self.control_events = []
        self.step_events = []
        self.load_attempts = {}
        self.load_resolved = set()
        self._control_sequence = 0
        self.zero_token_steps = 0

    def emit_control(self, kind, rid, now, payload=None):
        """仅输出请求级事实，由连接层决定反馈可见时间。"""
        self._control_sequence += 1
        self.control_events.append(ControlEvent(
            f"{self.id}:{self._control_sequence}", kind, rid, self.id,
            now, now, payload or {}))

    def load_completed(self, rid, now):
        self.emit_control("load_completed", rid, now,
                          {"attempt_id": self.load_attempts[rid]})

    def account(self, dt):
        if self.inflight is not None:
            self.compute_s += dt
        elif self.scheduler.has_requests():
            self.stall_s += dt
        else:
            self.idle_s += dt

    def schedule(self, now):
        if self.inflight is not None or (
            not self.scheduler.has_requests() and not self.pending_recvs
        ):
            return None
        if self.c["scheduler"] == "ascend_balance":
            self.scheduler.balance_queue[0].fill_(len(self.scheduler.running))
        out = self.scheduler.schedule()
        for rid, blocks, demand in self.scheduler.connector.submissions:
            self.store.submit(now, (self.id, rid), blocks, demand)
            self.load_attempts[rid] = self.load_attempts.get(rid, 0) + 1
            self.load_resolved.add(rid)
            self.emit_control("load_submitted", rid, now, {
                "attempt_id": self.load_attempts[rid], "num_blocks": len(blocks),
                "submitted_read_bytes": len(blocks) * self.store.block_bytes})
        self.scheduler.connector.submissions.clear()
        ids = list(out.num_scheduled_tokens)
        sampled = []
        prefill = decode = 0
        shapes = []
        for rid, count in out.num_scheduled_tokens.items():
            req = self.scheduler.requests[rid]
            if rid not in self.load_resolved:
                # 只在真实 Scheduler 已决定计算时确认，不把临时匹配失败当作无加载。
                self.load_resolved.add(rid)
                self.emit_control("no_external_load", rid, now)
            before = req.num_computed_tokens - count
            p = min(count, max(0, req.num_prompt_tokens - before))
            prefill += p
            decode += count - p
            if p:
                shapes.append((before, p))
            sampled.append([100] if req.num_computed_tokens >= req.num_tokens else [])
        model = ModelRunnerOutput(
            req_ids=ids,
            req_id_to_index={r: i for i, r in enumerate(ids)},
            sampled_token_ids=sampled,
        )
        if not ids:
            self.zero_token_steps += 1
            return out, model, now
        timing = estimate_step(self.c["compute"], shapes, decode)
        duration = timing["duration_s"]
        if duration <= 0:
            raise ValueError("nonpositive compute time")
        self.steps += 1
        self.step_events.append({"t": now, "event": "step_scheduled", "worker": self.id,
                                 "step_id": self.steps, "num_requests": len(ids),
                                 "num_scheduled_tokens": dict(out.num_scheduled_tokens),
                                 "prefill_tokens": prefill, "decode_tokens": decode,
                                 "duration_s": duration, "timing": timing})
        self.inflight = out, model, now + duration
        return None

    def complete(self, scheduled):
        out, model, _ = scheduled
        if self.pending_recvs:
            model.kv_connector_output = KVConnectorOutput(
                finished_recving=set(self.pending_recvs)
            )
            self.pending_recvs.clear()
        results = self.scheduler.update_from_output(out, model)
        self.inflight = None
        return [entry for client in results.values() for entry in client.outputs]
