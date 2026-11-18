"""MiniMax TP=2, batch=1, total16 compute estimate; excludes OD and queueing."""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

VERSION = "prefill50k_total16_v2"
MAX_MODEL_LEN = 98000
CHUNK_TOKENS = 50000
MODEL_FILE = Path(__file__).with_name("model.json")
POLICY = dict(max_model_len=MAX_MODEL_LEN, chunk_tokens=CHUNK_TOKENS,
              outside_measured_envelope="ridge_extrapolation",
              high_h_floor=61440, minimum_ms=1.0)


def measured_envelope(h, u):
    """Envelope of the 240 available points, not a claim of every shape measured."""
    if h <= 47104:
        return u <= 50000
    if h <= 50048:
        return u <= 40960
    return h <= 61440 and u <= 11441


def features(h, u):
    x, y = h / 61440, u / 50000
    z = math.log1p(u / 128)
    return (1., y, y*y, x*y, x*x*y, x*y*y, z, x*z, x,
            max(0., u-1024) / 50000)


@dataclass(frozen=True)
class PrefillEstimate:
    compute_ms: float
    source: str

    @property
    def compute_s(self):
        return self.compute_ms / 1000


class PrefillTimeModel:
    """Reuse the frozen 189-point ridge fit; explicitly label extrapolation."""

    def __init__(self, path=MODEL_FILE):
        data = Path(path).read_bytes()
        self.model_sha256 = hashlib.sha256(data).hexdigest()
        model = json.loads(data)
        if (model.get("version") != VERSION or model.get("runtime_policy") != POLICY
                or model.get("fit", {}).get("method") != "ridge"):
            raise ValueError("Expected prefill50k_total16_v2 with its frozen range policy")
        self.coefficients = tuple(model["fit"]["coefficients"])
        if len(self.coefficients) != 10 or any(
                type(v) not in (int, float) or not math.isfinite(v)
                for v in self.coefficients):
            raise ValueError("Expected 10 finite ridge coefficients")
        self._cached = lru_cache(maxsize=4096)(self._estimate)

    def estimate(self, hit_tokens, miss_tokens):
        if (type(hit_tokens) is not int or type(miss_tokens) is not int
                or hit_tokens < 0 or miss_tokens < 1
                or hit_tokens + miss_tokens >= MAX_MODEL_LEN):
            raise ValueError("Require H>=0, U>=1 and H+U+1<=98000")
        return self._cached(hit_tokens, miss_tokens)

    def _ridge(self, h, u):
        # Preserve the v1 arithmetic for exact comparison on the 240 points.
        return max(1.0, sum(a*b for a, b in zip(self.coefficients, features(h, u))))

    def _estimate(self, hit, miss):
        durations = []
        extrapolated = False
        for start in range(0, miss, CHUNK_TOKENS):
            h, u = hit + start, min(CHUNK_TOKENS, miss-start)
            extrapolated |= not measured_envelope(h, u)
            duration = self._ridge(h, u)
            if h > 61440:
                # The short-U regression can decrease with H. Keep the tail
                # at least as large as the H=61440 boundary estimate.
                duration = max(duration, self._ridge(61440, u))
            durations.append(duration)
        source = VERSION + ("_extrapolated" if extrapolated else "")
        return PrefillEstimate(math.fsum(durations), source)
