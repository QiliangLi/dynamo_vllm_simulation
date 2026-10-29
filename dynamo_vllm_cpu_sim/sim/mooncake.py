"""Mooncake 离线首现命中；按 D1/D2/D3/D7 固定每条请求的 h/u。"""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
import re

BLOCK = 512
MODEL = "first_seen(D1/D2/D3/D7)"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def derive_rows(rows):
    """每次调用对应一个文件；同刻组处理完后才更新已见集合。"""
    ordered = []
    for line, original in enumerate(rows, 1):
        r = dict(original)
        for field in ("timestamp", "input_length", "output_length"):
            if type(r[field]) is not int or r[field] < 0:
                raise ValueError(f"line {line}: invalid {field}")
        if r["input_length"] < 1:
            raise ValueError(f"line {line}: empty prompt cannot retain one token")
        if (not isinstance(r["hash_ids"], list)
                or any(type(h) is not int for h in r["hash_ids"])
                or len(r["hash_ids"]) != (r["input_length"]+BLOCK-1)//BLOCK):
            raise ValueError(f"line {line}: invalid 512-token hash list")
        ordered.append((line, r))
    if not ordered:
        raise ValueError("empty Mooncake trace")
    ordered.sort(key=lambda x: (x[1]["timestamp"], x[0]))
    seen, result = set(), []
    i = 0
    while i < len(ordered):
        j = i+1
        while j < len(ordered) and ordered[j][1]["timestamp"] == ordered[i][1]["timestamp"]:
            j += 1
        for line, r in ordered[i:j]:
            k = 0
            for h in r["hash_ids"]:
                if h not in seen:
                    break
                k += 1
            n = r["input_length"]
            hit = n-1 if k > n//BLOCK else min(BLOCK*k, n-1)
            r.update(hit_tokens=hit, u_tokens=n-hit, full_hit_adjusted=hit >= n-1)
            result.append((line, r))
        # 首个未命中后的后缀、尾块也进入历史；组内和请求内互不可见。
        for _, r in ordered[i:j]:
            seen.update(r["hash_ids"])
        i = j
    return result


def derive_file(source, output, check_only=False):
    source, output = Path(source), Path(output)
    lines = source.read_text(encoding="utf-8").splitlines()
    if any(not s.strip() for s in lines):
        raise ValueError("blank trace lines would change source_line identities")
    derived = derive_rows([json.loads(s) for s in lines])
    encoded = "".join(json.dumps(r, separators=(",", ":"))+"\n" for _, r in derived).encode()
    if check_only:
        if not output.is_file() or output.read_bytes() != encoded:
            raise ValueError(f"derived file mismatch: {output}")
    else:
        if source.resolve() == output.resolve():
            raise ValueError("source must not be overwritten")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(encoded)
    groups = Counter(r["timestamp"] for _, r in derived)
    inputs = sum(r["input_length"] for _, r in derived)
    hits = sum(r["hit_tokens"] for _, r in derived)
    return derived, dict(algorithm=MODEL, input_sha256=sha256_file(source),
        derived_sha256=hashlib.sha256(encoded).hexdigest(), n_rows=len(derived),
        timestamps=len(groups), max_same_ts_group=max(groups.values()),
        token_hit_ratio=hits/inputs,
        request_hit_ratio=sum(r["hit_tokens"] > 0 for _, r in derived)/len(derived),
        full_hit_rows=sum(r["full_hit_adjusted"] for _, r in derived))


def export_stack_rows(derived, source_name, safe_tokens, lines=None, time_scale=1.0):
    """完整预扫后选行；合成文本只承载形状，不重建真实内容共享。"""
    if not math.isfinite(time_scale) or time_scale <= 0:
        raise ValueError("time_scale must be positive and finite")
    selected = [(line, r) for line, r in derived if lines is None or line in lines]
    if not selected or (lines is not None and {line for line, _ in selected} != set(lines)):
        raise ValueError("invalid source line selection")
    start = selected[0][1]["timestamp"]
    rows = []
    for line, r in selected:
        rng = random.Random(hashlib.sha256(f"{source_name}:{line}".encode()).digest())
        tokens = [rng.choice(safe_tokens) for _ in range(r["input_length"])]
        rows.append(dict(id=f"{source_name}:L{line}",
            arrival_s=(r["timestamp"]-start)*time_scale/1000,
            prompt_token_ids=tokens, output_tokens=1, remote_prefix_tokens=r["hit_tokens"],
            hit_tokens=r["hit_tokens"], u_tokens=r["u_tokens"],
            full_hit_adjusted=r["full_hit_adjusted"], cache_model="first_seen",
            mooncake_source=dict(file=source_name,line=line,timestamp=r["timestamp"],
                                 hash_ids=r["hash_ids"],output_length=r["output_length"])))
    return rows


def validate_hit_row(row, block_size):
    n, h = len(row["prompt_token_ids"]), row["hit_tokens"]
    if (row.get("cache_model") != "first_seen" or type(h) is not int
            or h < 0 or h >= n or type(row["u_tokens"]) is not int or row["u_tokens"] != n-h
            or type(row["remote_prefix_tokens"]) is not int or row["remote_prefix_tokens"] != h
            or type(row["full_hit_adjusted"]) is not bool
            or row["full_hit_adjusted"] != (h == n-1)):
        raise ValueError("invalid first_seen row contract")
    if h % block_size:
        raise ValueError("first_seen hit requires exact block alignment; use block_size=1")


def engine_key(identity):
    """固定版本的引擎 ID 为 UUID 加后缀，如 UUID_0 或 UUID-99626d23。"""
    match = re.match(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}",
                     str(identity), re.IGNORECASE)
    if match is None:
        raise ValueError("missing gateway UUID in engine request id")
    return match.group().replace("-", "").lower()
