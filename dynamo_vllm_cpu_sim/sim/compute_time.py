"""按真实本轮形状估算执行时间。仅替换计时，不修改 Scheduler 或 KV 分配。"""

import math
from fractions import Fraction

def validate_compute(compute):
    mode = compute.get("mode", "linear")
    if mode not in ("linear", "glm5_d950pr", "sa_e26f"):
        raise ValueError(f"unknown compute.mode: {mode}")
    if mode == "linear":
        for key in ("step_overhead_s", "prefill_token_s", "decode_token_s"):
            value = compute.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"bad compute.{key}")
        if compute["prefill_token_s"] <= 0:
            raise ValueError("prefill_token_s must be positive")
    elif mode == "sa_e26f":
        p = compute["profile"]
        if (type(p["L"]) is not int or p["L"] <= 0
                or type(p["N_sat"]) is not int or p["N_sat"] <= 0
                or p.get("no_speedup",False)):
            raise ValueError("invalid/unsupported SA profile")
        for key in ("kappa_gb_per_token_layer","t_launch_s","a_s_per_token","b_s_per_pair","eta_floor"):
            if Fraction(p[key]) <= 0:
                raise ValueError("nonpositive SA profile coefficient")
        if Fraction(p["eta_floor"]) > 1:
            raise ValueError("SA eta_floor must not exceed 1")
    else:
        from sim.timing_models.prefill_time_batch import VERSION, _validate, validate_batch_options
        validate_batch_options(compute.get("batch_time_model", VERSION),
                               compute.get("launch_saving_fraction"))
        tp, dp = compute.get("tp", 1), compute.get("dp", 8)
        _validate([], tp, dp, compute.get("ep", tp * dp),
                  compute.get("sparse_HBM_hit_ratio", 1.0),
                  compute.get("kv_hbm_reuse", True), compute.get("context_length_limit"))
        for key, default in (("engine_overhead_s", 0.0), ("sparse_HBM_hit_ratio", 1.0)):
            value = compute.get(key, default)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"bad compute.{key}")
        if compute.get("sparse_HBM_hit_ratio", 1.0) > 1:
            raise ValueError("sparse_HBM_hit_ratio must be <= 1")
    return mode


def get_compute_time_model(compute):
    """选择 ABC、GLM 或线性实现；模型扩展通过此工厂接入。"""
    from sim.timing_models.compute_models import (LinearComputeTimeModel,
        SAComputeTimeModel, GLM5ComputeTimeModel)
    mode=validate_compute(compute)
    return {'linear':LinearComputeTimeModel,'sa_e26f':SAComputeTimeModel,
            'glm5_d950pr':GLM5ComputeTimeModel}[mode](compute)


def estimate_step(compute, shapes, decode_tokens=0):
    """稳定入口：按模型实现估算实际本步 (已计算 h, 新计算 u)。"""
    return get_compute_time_model(compute).estimate_step(shapes,decode_tokens)


def reference_compute_s(compute, prompt_tokens, cached_tokens, chunk_budget):
    """到达时的单请求估计；只使用已知长度与参考 chunk 预算。"""
    if not 0 <= cached_tokens < prompt_tokens or chunk_budget <= 0:
        raise ValueError("invalid reference prompt/cache/chunk budget")
    total = 0.0
    h = cached_tokens
    while h < prompt_tokens:
        u = min(chunk_budget, prompt_tokens - h)
        total += estimate_step(compute, [(h, u)])["duration_s"]
        h += u
    return total
