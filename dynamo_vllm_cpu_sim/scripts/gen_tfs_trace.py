#!/usr/bin/env python3
"""生成独立前缀 A/B/C 负载；参数与文件 SHA 同时保存。"""

import argparse
import hashlib
import json
from pathlib import Path
import random

PRESETS = {
    "abc_ample": {"total": 24, "pattern": "continuous", "interval_s": 0.04,
                  "remote_a": 512, "unread_a": 16, "remote_b": 32, "unread_b": 256,
                  "unread_c": 128, "output_tokens": 4, "burst_size": 6},
    "abc_burst": {"total": 24, "pattern": "burst", "interval_s": 0.02,
                  "remote_a": 512, "unread_a": 16, "remote_b": 32, "unread_b": 256,
                  "unread_c": 128, "output_tokens": 4, "burst_size": 24},
    "abc_sustained": {"total": 24, "pattern": "clustered", "interval_s": 0.0005,
                      "remote_a": 512, "unread_a": 16, "remote_b": 32, "unread_b": 256,
                      "unread_c": 128, "output_tokens": 4, "burst_size": 6},
}


def generate(seed, params):
    """每个请求前两个 token 编码独立序号，避免本地前缀共享。"""
    rng = random.Random(seed)
    p = dict(params)
    if p["total"] <= 0 or p["total"] >= 31999**2 or p["burst_size"] <= 0 or p["interval_s"] < 0:
        raise ValueError("bad generator size/time")
    if not 0 < p["output_tokens"] < 16:
        raise ValueError("main experiment requires output_tokens < block_size=16")
    if p["pattern"] not in ("continuous", "burst", "clustered"):
        raise ValueError("unknown arrival pattern")
    rows = []
    for i in range(p["total"]):
        cls = "ABC"[i % 3]
        remote = p["remote_a"] if cls == "A" else p["remote_b"] if cls == "B" else 0
        unread = p["unread_" + cls.lower()]
        if remote < 0 or remote % 16 or unread < 2:
            raise ValueError("prefix must be block aligned and unread >= 2")
        if p["pattern"] == "burst":
            t = (i // p["burst_size"]) * p["interval_s"]
        elif p["pattern"] == "clustered":
            # 每组六条中的三类到达紧邻；簇间持续到达，间距带固定种子抖动。
            t = (i // p["burst_size"]) * p["burst_size"] * p["interval_s"]
            t += (i % p["burst_size"]) * p["interval_s"] * 0.05
        else:
            t = i * p["interval_s"]
        if i and p["pattern"] == "continuous":
            t += rng.uniform(0, p["interval_s"] * 0.1)
        tokens = [i // 31999 + 1, i % 31999 + 1]
        tokens += [rng.randrange(32000) for _ in range(remote + unread - 2)]
        rows.append({"id": f"r{i:05d}", "class_id": cls, "arrival_s": round(t, 12),
                     "prompt_token_ids": tokens, "remote_prefix_tokens": remote,
                     "output_tokens": p["output_tokens"]})
    return rows


def write_trace(path, seed, params):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = generate(seed, params)
    text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    if not path.exists() or path.read_text() != text:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text)
        temporary.replace(path)
    metadata = {"schema_version": 1, "seed": seed, "parameters": params,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "unique_prefixes": True, "class_source": "class_id",
                "token_semantics": "synthetic; not a calibrated model workload"}
    path.with_suffix(".meta.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--preset", choices=PRESETS, default="abc_burst")
    p.add_argument("--parameters", help="JSON 参数文件，覆盖 preset 的负载参数")
    p.add_argument("--total", type=int)
    p.add_argument("--pattern", choices=["continuous", "burst", "clustered"])
    a = p.parse_args()
    params = dict(PRESETS[a.preset])
    if a.parameters:
        overrides = json.loads(Path(a.parameters).read_text())
        if set(overrides) - set(params):
            p.error("parameters contains unknown workload fields")
        params.update(overrides)
    for name in ("total", "pattern"):
        if getattr(a, name) is not None:
            params[name] = getattr(a, name)
    write_trace(a.output, a.seed, params)


if __name__ == "__main__":
    main()
