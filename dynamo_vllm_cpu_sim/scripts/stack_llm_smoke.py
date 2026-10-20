"""SimExecutor 最小冒烟：vLLM 真实 EngineCore 子进程 + token-id 请求（W2）。

用法（在 dynamo_vllm_cpu_sim/ 下）::

    VLLM_PLUGINS='' HF_HUB_OFFLINE=1 .venv/bin/python scripts/stack_llm_smoke.py

校验点：EngineCore 初始化链在 empty-device 上走通（KV spec/内存推导/块数
override）；每请求恰好产出 max_tokens 个合成 token（100）；请求全部完成。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vllm import LLM, SamplingParams


def main() -> int:
    llm = LLM(
        model="configs/model",
        skip_tokenizer_init=True,
        dtype="float32",
        max_model_len=2048,
        block_size=16,
        num_gpu_blocks_override=128,
        max_num_seqs=8,
        max_num_batched_tokens=128,
        enforce_eager=True,
        seed=0,
        distributed_executor_backend="sim.stack.executor.SimExecutor",
    )
    sp = SamplingParams(max_tokens=8, ignore_eos=True, temperature=0)
    outs = llm.generate(
        [{"prompt_token_ids": [11 + i, 12, 13, 14, 15, 16, 17, 18]} for i in range(4)], sp
    )
    ok = True
    for o in outs:
        toks = list(o.outputs[0].token_ids)
        good = toks == [100] * 8 and o.outputs[0].finish_reason == "length"
        ok &= good
        print("req", o.request_id, "tokens", toks, "finish", o.outputs[0].finish_reason, "OK" if good else "BAD")
    print("SMOKE", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
