"""分模型计算时间接口；各实现保留原有数学与输出契约。"""
from abc import ABC, abstractmethod
from copy import deepcopy
import math

from . import glm5_d950pr_reference as ref
from .prefill_time_batch import VERSION, estimate_batch


class ComputeTimeModel(ABC):
    mode = None

    def __init__(self, compute):
        self.compute = deepcopy(compute)

    def estimate_step(self, shapes, decode_tokens=0):
        shapes = list(shapes)
        if decode_tokens < 0:
            raise ValueError('negative decode tokens')
        if any(not isinstance(h,int) or not isinstance(u,int) or h<0 or u<=0 for h,u in shapes):
            raise ValueError('shape requires integer h>=0 and u>0')
        if not shapes and not decode_tokens:
            return dict(mode=self.mode, duration_s=0.0, shapes=[])
        return self._estimate_step(shapes, decode_tokens)

    @abstractmethod
    def _estimate_step(self, shapes, decode_tokens):
        """返回 duration_s、shapes 以及该模型原有的估时明细。"""


class LinearComputeTimeModel(ComputeTimeModel):
    mode = 'linear'

    def _estimate_step(self, shapes, decode_tokens):
        c=self.compute
        duration=(c['step_overhead_s']+sum(u for h,u in shapes)*c['prefill_token_s']
                  +decode_tokens*c['decode_token_s'])
        return dict(mode=self.mode,duration_s=duration,
                    shapes=[list(s) for s in shapes],decode_tokens=decode_tokens)


class SAComputeTimeModel(ComputeTimeModel):
    mode = 'sa_e26f'

    def _estimate_step(self, shapes, decode_tokens):
        if decode_tokens:
            raise ValueError('SA stack is prefill-only; set max_tokens=1')
        from sim.sa_model import layer_compute
        p=self.compute['profile'];per_layer=layer_compute(shapes,p)
        n=sum(u for h,u in shapes)
        return dict(mode=self.mode,shapes=[list(s) for s in shapes],bs=len(shapes),
            duration_s=p['L']*per_layer,layer_compute_s=per_layer,layers=p['L'],
            real_prefill_tokens=n,padded_prefill_tokens=n,decode_tokens=0)


class GLM5ComputeTimeModel(ComputeTimeModel):
    mode = 'glm5_d950pr'

    def _estimate_step(self, shapes, decode_tokens):
        compute=self.compute
        mode=self.mode
        n=sum(u for h,u in shapes)
        if decode_tokens:
            raise ValueError("glm5_d950pr is prefill-only; set output_tokens/max_tokens=1")
        bs = len(shapes)
        if bs > ref.MODEL["max_batch_size"]:
            raise ValueError("glm5_d950pr supports at most 8 prefill requests per step")
        tp, dp = compute.get("tp", 1), compute.get("dp", 8)
        ep = compute.get("ep", tp * dp)
        batch = estimate_batch([(h + u, u) for h, u in shapes], tp=tp, dp=dp, ep=ep,
            sparse_HBM_hit_ratio=compute.get("sparse_HBM_hit_ratio", 1.0),
            kv_hbm_reuse=compute.get("kv_hbm_reuse", True),
            batch_time_model=compute.get("batch_time_model", VERSION),
            launch_saving_fraction=compute.get("launch_saving_fraction"))
        components = batch["components_us"]
        dense, moe = batch["dense_layer_us"], batch["moe_layer_us"]
        m = ref.MODEL
        layers, dense_layers = m["n_layers"], m["n_dense_layers"]
        model_s = batch["duration_s"]
        duration = model_s + compute.get("engine_overhead_s", 0.0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("nonpositive/nonfinite modeled execution time")
        # 与来源工具相同：命中前缀 MLA + indexer 的完整加载量，IO 不按 padding 放大。
        kv_gb = batch["per_layer_prefix_kv_gb"]
        return {"mode": mode, "duration_s": duration, "model_s": model_s,
                "batch_time_model": batch["model"], "work": batch["work"],
                **({"batch_composition": batch["batch_composition"]}
                   if "batch_composition" in batch else {}),
                "moe_work": batch["moe_work"],
                "layer_durations_us": batch["layer_durations_us"],
                "engine_overhead_s": compute.get("engine_overhead_s", 0.0),
                "shapes": [list(s) for s in shapes], "bs": bs,
                "reference_tp": tp, "reference_dp": dp, "reference_ep": ep,
                "reference_total_bs": bs * dp,
                "max_input_length": max(h + u for h, u in shapes),
                "max_u_tokens": max(u for _, u in shapes),
                "real_prefill_tokens": n, "padded_prefill_tokens": n,
                "sparse_HBM_hit_ratio": compute.get("sparse_HBM_hit_ratio", 1.0),
                "kv_hbm_reuse": compute.get("kv_hbm_reuse", True),
                "dense_layers": dense_layers, "moe_layers": layers - dense_layers,
                "dense_layer_us": dense, "moe_layer_us": moe,
                "per_layer_us": model_s * 1e6 / layers,
                "per_layer_prefix_kv_gb": kv_gb, "components": components}
