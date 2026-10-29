"""进程栈 HTTP 回放器（W4）：按 arrival_s 开环注入 trace，记录 TTFT/E2E。

与 DES 同源的 trace JSONL 输入（id/arrival_s/prompt_token_ids/output_tokens）。
前端 HTTP 接口只收文本：prompt_token_ids 经 tokenizer 解码为文本后发送，
逐条做 decode→encode 往返校验，不回环的行默认跳过并计数（可用
--allow-lossy 关闭校验，token 数以服务端 usage 为准）。

用法（栈已在跑，见 scripts/launch_stack.sh）::

    .venv/bin/python scripts/replay_http.py configs/demo.jsonl \
        --model sim-opt --url http://127.0.0.1:8765 --output results/stack_demo

输出 <output>/report.json（每请求 ttft_s/e2e_s/usage）与 <output>/trace.meta.json。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sim.stack.clock import TimeScale  # noqa: E402


async def one_request(
    client: httpx.AsyncClient, url: str, model: str, rid: str, text: str,
    max_tokens: int, record: dict, api: str, admission_metadata: dict | None = None,
    time_scale: float = 1.,
) -> None:
    clock = TimeScale(time_scale)
    if api == "completions":
        payload = {"model": model, "prompt": text, "max_tokens": max_tokens,
                   "stream": True, "temperature": 0}
        url = url.replace("/v1/chat/completions", "/v1/completions")
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": max_tokens,
            "stream": True,
            "temperature": 0,
        }
    if admission_metadata is not None:
        payload["_sim_admission"] = admission_metadata
    t_send = time.perf_counter()
    ttft = None
    finish = None
    usage = None
    try:
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                    continue
                chunk = json.loads(line[len("data: ") :])
                delta = chunk["choices"][0].get("delta", {}) if chunk.get("choices") else {}
                if ttft is None and chunk.get("choices") and (
                    delta.get("content") or delta.get("role") or chunk["choices"][0].get("text")
                ):
                    ttft = time.perf_counter() - t_send
                if chunk.get("usage"):
                    usage = chunk["usage"]
                if chunk.get("choices") and chunk["choices"][0].get("finish_reason"):
                    finish = chunk["choices"][0]["finish_reason"]
        elapsed = time.perf_counter() - t_send
        record[rid] = {
            "ttft_s": clock.to_model(ttft) if ttft is not None else None,
            "e2e_s": clock.to_model(elapsed),
            "ttft_wall_s": ttft, "e2e_wall_s": elapsed, "sent_perf_s": t_send,
            "finish": finish,
            "usage": usage,
            "ok": True,
        }
    except Exception as exc:  # noqa: BLE001
        record[rid] = {"ok": False, "sent_perf_s": t_send,
                       "error": f"{type(exc).__name__}: {exc}"}


async def replay(
    trace_path: Path, url: str, model: str, out_dir: Path, allow_lossy: bool, api: str,
    *, admission: bool = False, time_scale: float = 1.,
) -> int:
    clock = TimeScale(time_scale)
    chat_url = url.rstrip("/") + (
        "/v1/completions" if api == "completions" else "/v1/chat/completions"
    )
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(str(ROOT / "configs/model"))
    from sim.trace_io import load_trace
    rows = load_trace(trace_path)
    jobs = []
    records: dict[str, dict] = {}
    skipped = 0
    async with httpx.AsyncClient(timeout=clock.to_wall(120),trust_env=False) as client:
        for row in rows:
            ids = row.get("prompt_token_ids")
            text = row.get("prompt_text")
            if text is None:
                text = tok.decode(ids)
                if not allow_lossy and tok.encode(text) != list(ids):
                    skipped += 1
                    records[row["id"]] = {"ok": False, "error": "lossy-token-roundtrip", "skipped": True}
                    continue
            delay = row["arrival_s"]
            meta = dict(id=row["id"],estimated_remote_tokens=row.get("remote_prefix_tokens",0),
                        class_id=row.get("class_id","unclassified")) if admission else None
            jobs.append((delay, row["id"], text, row["output_tokens"],meta))
        t0 = time.perf_counter()
        tasks = []
        for delay, rid, text, out_tokens, meta in jobs:
            await asyncio.sleep(max(0.0, clock.to_wall(delay) - (time.perf_counter() - t0)))
            kwargs = {"admission_metadata":meta} if admission else {}
            tasks.append(asyncio.create_task(
                one_request(client, chat_url, model, rid, text, out_tokens, records, api,
                            time_scale=clock.factor,**kwargs)))
        await asyncio.gather(*tasks)

    ok = [r for r in records.values() if r.get("ok")]
    ttfts = sorted(r["ttft_s"] for r in ok if r["ttft_s"] is not None)
    elapsed_wall = time.perf_counter() - t0
    for row in rows:
        record = records[row["id"]]
        if "sent_perf_s" in record:
            record["planned_arrival_s"] = row["arrival_s"]
            record["actual_arrival_s"] = clock.to_model(record["sent_perf_s"]-t0)
    report = {
        "trace": str(trace_path),
        "url": url,
        "model": model,
        "n_total": len(rows),
        "n_ok": len(ok),
        "n_skipped": skipped,
        "ttft_mean_s": sum(ttfts) / len(ttfts) if ttfts else None,
        "ttft_max_s": ttfts[-1] if ttfts else None,
        "requests": records,
        "wall_clock_s": elapsed_wall,
        "model_elapsed_s": clock.to_model(elapsed_wall),
        "wall_time_scale": clock.factor,
        "ttft_mean_wall_s": sum(r["ttft_wall_s"] for r in ok if r["ttft_wall_s"] is not None) / len(ttfts) if ttfts else None,
        "time_units": "ttft_s/e2e_s/arrival_s are model seconds; *_wall_s and wall_clock_s are wall seconds",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(
        f"replayed {len(rows)}: ok={len(ok)} skipped={skipped} "
        f"ttft_mean={report['ttft_mean_s'] and round(report['ttft_mean_s'], 4)}s "
        f"-> {out_dir / 'report.json'}"
    )
    return 0 if len(ok) > 0 and skipped < len(rows) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", type=Path, help="trace JSONL（DES 同格式）")
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--model", default="sim-opt")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--allow-lossy", action="store_true", help="跳过 decode→encode 往返校验")
    ap.add_argument("--admission",action="store_true",help="向 TFS 服务发送公开画像元数据")
    ap.add_argument("--time-scale",type=float,default=1.,help="wall seconds per model second (>=1)")
    ap.add_argument("--api", choices=["chat", "completions"], default="completions",
                    help="completions=裸 prompt（token 精确，远端 KV 实验用）；chat=套模板")
    args = ap.parse_args()
    return asyncio.run(replay(args.trace, args.url, args.model, args.output, args.allow_lossy, args.api,
                             admission=args.admission,time_scale=args.time_scale))


if __name__ == "__main__":
    raise SystemExit(main())
