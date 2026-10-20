"""dynamo.vllm worker 包装入口：注入 SimExecutor 并补齐 0.20.2↔0.28 兼容层（W3/W4）。

为什么需要包装（两件事，均不改 vLLM 与 Dynamo 任何源码，设计文档 D1/D2）：

1. executor 注入：vLLM CLI 的 ``--distributed-executor-backend`` 用 argparse
   choices 限定 ``mp/ray/uni/external_launcher``，限定名字符串会被拒绝；程序内
   赋值则走 ``Executor.get_class`` 的 qualname 分支（0.20.2 支持）。本脚本在
   CLI 解析完成后把 ``ParallelConfig.distributed_executor_backend`` 覆盖为
   ``SIM_STACK_EXECUTOR``（默认 ``sim.stack.executor.SimExecutor``）。

2. 运行期 API 漂移（设计文档风险 R1，实测两处）：
   - ``vllm.exceptions.VLLMClientError`` / ``VLLMUnprocessableEntityError``：
     0.28 存在、0.20.2 没有，且 dynamo 的 handlers.py 在请求路径上**延迟**
     import（静态 import 探测不触发）。dynamo 只捕获它们做 HTTP 状态映射，
     0.20.2 不会抛出；定义为 ``VLLMError`` 子类即语义等价。
   - ``EngineCore.get_kv_cache_group_metadata``：0.28 的工具方法，dynamo 的
     cache_info.py 经 ``call_utility_async`` 调用；0.20.2 没有。dynamo 侧
     失败会回退到 cache_config.block_size（非致命），这里用 SimExecutor 持有
     的真实 KVCacheConfig 提供正确值。

用法（在 dynamo_vllm_cpu_sim/ 下，配合 scripts/launch_stack.sh）::

    PYTHONPATH="$PWD" .venv/bin/python scripts/stack_vllm_worker.py \
        --model configs/model --served-model-name sim-opt <其余 dynamo.vllm/vLLM 参数>
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXECUTOR_QUALNAME = os.environ.get(
    "SIM_STACK_EXECUTOR", "sim.stack.executor.SimExecutor"
)

# 提前 import 一次，尽早暴露限定名拼写错误，而不是等到 EngineCoreProc 里。
from sim.stack.executor import SimExecutor  # noqa: F401,E402

# ---------------------------------------------------------------- 兼容层 1：异常符号
import vllm.exceptions as _ve  # noqa: E402

if not hasattr(_ve, "VLLMClientError"):

    class VLLMClientError(Exception):
        """0.28 客户端可重试错误的基类；0.20.2 侧无人抛出，仅供 dynamo 捕获。"""

    class VLLMUnprocessableEntityError(VLLMClientError):
        pass

    _ve.VLLMClientError = VLLMClientError
    _ve.VLLMUnprocessableEntityError = VLLMUnprocessableEntityError

# ---------------------------------------------------------------- 兼容层 2：工具方法
from vllm.v1.engine.core import EngineCore  # noqa: E402


def _get_kv_cache_group_metadata(self) -> list[dict]:
    # 0.28 契约：list[{kind, block_size, ...}]；dynamo 消费 kind/block_size
    # 选主注意力块大小。数据来自 SimExecutor.initialize_from_config 保存的
    # 真实 KVCacheConfig（list[每 worker 一份]）。
    executor = self.model_executor
    configs = getattr(executor, "kv_cache_config", None) or []
    out: list[dict] = []
    for config in configs:
        for group in config.kv_cache_groups:
            spec = group.kv_cache_spec
            out.append(
                {
                    "kind": type(spec).__name__.replace("Spec", "").lower(),
                    "block_size": spec.block_size,
                    "layer_names": list(group.layer_names),
                }
            )
    return out


if not hasattr(EngineCore, "get_kv_cache_group_metadata"):
    EngineCore.get_kv_cache_group_metadata = _get_kv_cache_group_metadata

# ---------------------------------------------------------------- executor 注入
from vllm.engine.arg_utils import AsyncEngineArgs  # noqa: E402

_create_engine_config = AsyncEngineArgs.create_engine_config


def _patched_create_engine_config(self, *args, **kwargs):
    engine_config = _create_engine_config(self, *args, **kwargs)
    engine_config.parallel_config.distributed_executor_backend = EXECUTOR_QUALNAME
    return engine_config


AsyncEngineArgs.create_engine_config = _patched_create_engine_config

from dynamo.vllm.main import main  # noqa: E402

if __name__ == "__main__":
    main()
