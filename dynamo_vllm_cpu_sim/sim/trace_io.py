"""Read unchanged JSONL values with bounded sharing of immutable integers.

Each JSON array is still an independent Python list. This saves repeated token
integer objects, not model work or trace records. The cache belongs to one read.
"""
import json
from pathlib import Path


def load_trace(path, *, cache_limit=65536):
    if cache_limit < 0:
        raise ValueError('cache_limit must be nonnegative')
    cache = {}

    def parse_int(text):
        value = cache.get(text)
        if value is None:
            value = int(text)
            if len(cache) < cache_limit:
                cache[text] = value
        return value

    rows = []
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line, parse_int=parse_int))
    return rows
