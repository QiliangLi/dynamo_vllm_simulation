"""GLM5/D950PR 正式组批估时：按实际工作量聚合，保留冻结算子常数。

per_batch 接收 (input_length, u_tokens)，返回 (平均每层 us, 每层前缀 KV GB)。
input_length 是本轮结束时的上下文长度 h+u，不含未来尚未调度的 prompt。
estimate_batch 使用相同输入，另返回逐算子、逐层及完整 78 层时间。

4090 实验支持逐请求聚合工作量；其 Qwen 时间系数没有移植到本模块。
GLM5 算子与硬件常数沿用冻结 reference。DP/TP/EP 固定传入。
所有时间为公式预测，不是 GLM5 实测。默认不含外部 KV 读取、排队或 RPC。

v2：MoE 与 dispatch/combine 共用守恒的 token—专家计数，权重读取单独按
均匀独立路由的活跃专家期望值估计。假设各 DP 副本新 token 数相同，
不包含真实专家倾斜、kernel 内补齐、本地/跨卡通信区分或去重发送。
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
import importlib.util
import json
import math
from pathlib import Path


REFERENCE = Path(__file__).with_name("glm5_d950pr_reference.py")
REFERENCE_SHA256 = "ea76abe23c0a0c05b3633760358b70f876f4b20b9a8e18c92d10eac1a09c0b59"
VERSION = "glm5_actual_batch_work_v2"


@lru_cache(maxsize=1)
def _reference():
    """读取冻结公式；不修改它的函数、配置或模块全局变量。"""
    if hashlib.sha256(REFERENCE.read_bytes()).hexdigest() != REFERENCE_SHA256:
        raise ValueError("GLM5 reference changed; review formulas before updating the fingerprint")
    spec = importlib.util.spec_from_file_location("_glm5_batch_frozen_reference", REFERENCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate(requests, tp, dp, ep, sparse_HBM_hit_ratio, kv_hbm_reuse):
    for name, value in (("tp", tp), ("dp", dp), ("ep", ep)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if ep != dp * tp:
        raise ValueError("the retained reference requires ep == dp * tp")
    ref = _reference()
    if ep > ref.MODEL["n_routed_experts"]:
        raise ValueError("ep exceeds the number of routed experts")
    if any(ref.MODEL[key] % tp for key in ("n_heads", "index_n_heads")):
        raise ValueError("tp must divide both attention and indexer head counts")
    if (type(sparse_HBM_hit_ratio) not in (int, float)
            or not math.isfinite(sparse_HBM_hit_ratio)
            or not 0 <= sparse_HBM_hit_ratio <= 1):
        raise ValueError("sparse_HBM_hit_ratio must be finite and in [0, 1]")
    if type(kv_hbm_reuse) is not bool:
        raise ValueError("kv_hbm_reuse must be bool")
    rows = list(requests)
    if len(rows) > ref.MODEL["max_batch_size"]:
        raise ValueError("this candidate retains the reference limit of 8 requests")
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError("each request must be (input_length, u_tokens)")
        length, new = row
        if (type(length) is not int or type(new) is not int
                or not 0 <= new <= length <= ref.MODEL["max_seq_len"]):
            raise ValueError("require integer 0 <= u_tokens <= input_length <= max_seq_len")
    return tuple((length, new) for length, new in rows)


def _moe_estimate(new_tokens, *, tp, dp, ep):
    """按每 token top-k 统计路由；返回本层各 EP rank 的期望工作和时间。

    N_group=DP*N：沿用每个 DP 副本负载相同的假设，TP 不重复产生输入。
    每 token 均匀选择 K 个不同专家，跨 token 独立：
    P(某专家活跃)=1-(1-K/E)**N_group。
    FLOPs/激活字节用期望路由次数，权重字节用活跃专家期望数；两者分开。
    该近似没有模拟随机路由的耗时分布。尤其小批次不能当作实机定标。
    """
    ref = _reference()
    m, g = ref.MODEL, ref.NPU
    experts, top_k = m["n_routed_experts"], m["n_activated_experts"]
    group_tokens = new_tokens * dp
    group_routes = group_tokens * top_k
    # 每专家平均负载允许小于 1；不能用 max(...,1) 增加逻辑计算量。
    routes_per_expert = group_routes / experts
    active_probability = (-math.expm1(group_tokens * math.log1p(-top_k / experts))
                          if group_tokens else 0.0)
    group_active = experts * active_probability
    base, remainder = divmod(experts, ep)
    shared_routes = new_tokens * m["n_shared_experts"] / tp
    dim, intermediate = m["dim"], m["moe_inter_dim"]
    weight_elements = dim * intermediate
    compress = ref.CONF["moe_compress_ratio"]
    ranks = []
    for rank in range(ep):
        # 不整除时实际分配余数，避免每卡都 ceil 而凭空多出专家。
        owned = base + (rank < remainder)
        routes = owned * routes_per_expert
        active_experts = owned * active_probability
        routed_flops = 6 * routes * weight_elements
        routed_activation_bytes = 3 * routes * (dim + intermediate) * ref.FP8
        routed_weight_bytes = 3 * active_experts * weight_elements * ref.FP8
        shared_flops = 6 * shared_routes * weight_elements
        shared_activation_bytes = 3 * shared_routes * (dim + intermediate) * ref.FP8
        shared_weight_bytes = (3 * m["n_shared_experts"] * weight_elements
                               * compress * ref.FP8 if new_tokens else 0.0)
        # 三个 GEMM 的 FLOPs/总字节在冻结公式中相同，先各自 roofline 后求和。
        routed_us = 3 * ref._roof(
            routed_flops / 3, (routed_activation_bytes + routed_weight_bytes) / 3,
            fp=1)
        shared_us = 3 * ref._roof(
            shared_flops / 3, (shared_activation_bytes + shared_weight_bytes) / 3,
            fp=1)
        # 保留原逻辑向量传递、固定带宽及每批启动延迟。不是物理网卡字节。
        dispatch_bytes = routes * dim * ref.FP8
        combine_bytes = routes * dim * ref.FP16
        dispatch_us = (g["base_delay"] + dispatch_bytes / ref.GB / g["nvlink_bw"] * 1e6
                       if group_tokens else 0.0)
        combine_us = (g["base_delay"] + combine_bytes / ref.GB / g["nvlink_bw"] * 1e6
                      if group_tokens else 0.0)
        ranks.append(dict(
            rank=rank, owned_experts=owned, expected_active_experts=active_experts,
            routed_token_expert_work=routes, shared_token_expert_work=shared_routes,
            routed_flops=routed_flops, shared_flops=shared_flops,
            routed_activation_bytes=routed_activation_bytes,
            routed_weight_bytes=routed_weight_bytes,
            shared_activation_bytes=shared_activation_bytes,
            shared_weight_bytes=shared_weight_bytes,
            dispatch_logical_bytes=dispatch_bytes, combine_logical_bytes=combine_bytes,
            routed_us=routed_us, shared_us=shared_us,
            moe_us=routed_us + shared_us, dispatch_us=dispatch_us, combine_us=combine_us))
    # 同负载假设下 attention 相同；MoE 同步阶段取估计最慢 rank。
    critical = max(ranks, key=lambda r: r["moe_us"] + r["dispatch_us"] + r["combine_us"])
    return dict(
        routing_model="uniform_independent_topk_expected_work",
        peer_load_assumption="each DP replica has the same new-token count as this batch",
        group_new_tokens=group_tokens, top_k=top_k, num_experts=experts,
        group_routed_token_expert_work=group_routes,
        group_shared_token_expert_work=group_tokens * m["n_shared_experts"],
        group_expected_active_experts=group_active,
        expected_tokens_per_expert=routes_per_expert,
        expected_tokens_per_active_expert=(group_routes / group_active if group_active else 0.0),
        rank_selection="maximum modeled MoE plus communication time",
        critical_rank=critical["rank"], ranks=ranks,
        communication_model="reference full logical route payload; no locality or deduplication")


def estimate_batch(requests, *, dp: int, ep: int, tp: int = 1,
                   sparse_HBM_hit_ratio: float = 1.0,
                   kv_hbm_reuse: bool = True) -> dict:
    """返回本轮完整批次的模型时间与明细；部署参数不随请求数变化。

    每个输入为 (h+u, u)。分块 prefill 必须使用当前块结束时的 h+u。
    u=0 的行只贡献前缀字节，不启动算子，也不计入活跃 batch。
    前缀字节只表示 h 对应的 KV 大小，不是本轮实际外部读取需求。
    h 可含本地已计算的 token；真实读取仍由 KV 连接器决定，不能每轮重读 h。
    若只测 GPU/专家通信时间，保持 sparse_HBM_hit_ratio=1。
    小于 1 时，额外计入原公式的逐层稀疏取数，按原固定 storage_bw，
    不代表 120 GB/s 共享存储服务或其竞争。decode 的效率未定标。
    MoE 假设各 DP 副本本轮新 token 数相同，取估计最慢 EP rank；
    活跃专家是均匀独立路由的期望值，不是 Mooncake 提供的真实路由。
    """
    rows = _validate(requests, tp, dp, ep, sparse_HBM_hit_ratio, kv_hbm_reuse)
    ref = _reference()
    m, g = ref.MODEL, ref.NPU
    active = [(length - new, new) for length, new in rows if new > 0]
    bs = len(active)
    n = sum(new for _, new in active)
    scans = [max(1.0, history + new / 2) for history, new in active]
    pairs = math.fsum(new * scan for (_, new), scan in zip(active, scans))
    scan_tokens = math.fsum(scans)
    kv_entries = sum(min(new * m["index_topk"], history + new)
                     if kv_hbm_reuse else new * m["index_topk"]
                     for history, new in active)
    # exact_causal_pairs 只用于解释与 Qwen 指标的关系；GLM5 继续用原因果中点。
    exact_pairs = sum(new * history + new * (new + 1) // 2 for history, new in active)
    layers, dense_layers = m["n_layers"], m["n_dense_layers"]
    prefix_tokens = sum(length - new for length, new in rows)
    prefix_bytes = prefix_tokens * m["kv_precision"] * (
        (m["kv_lora_rank"] + m["qk_rope_head_dim"]) * layers
        + m["index_head_dim"] * math.ceil(layers / m["index_share_interval"]))
    components = dict.fromkeys(("indexer_q_us", "indexer_score_us", "gather_us",
                               "sparse_attention_us", "mla_projection_us",
                               "sparse_storage_us", "dense_mlp_us", "moe_us",
                               "dispatch_us", "combine_us"), 0.0)
    moe_work = _moe_estimate(n, tp=tp, dp=dp, ep=ep)
    critical = moe_work["ranks"][moe_work["critical_rank"]]
    for name in ("moe_us", "dispatch_us", "combine_us"):
        components[name] = critical[name]
    if active:
        c = dict(ref.CONF, bs=bs, dp=dp, ep=ep, tp=tp, total_bs=bs * dp,
                 sparse_HBM_hit_ratio=sparse_HBM_hit_ratio, kv_hbm_reuse=kv_hbm_reuse)
        # 这些被复用的线性函数只通过 bs*new_query_len 消费新 token。
        # N/bs 是代数转换，绝不拿它代替各请求的注意力扫描长度。
        c["new_query_len"] = n / bs
        for name, function in (
            ("indexer_q_us", ref._t_indexer_qb_linear),
            ("mla_projection_us", ref._t_mla_proj),
            ("dense_mlp_us", ref._t_mlp),
        ):
            components[name] = function(m, c, g)

        # 混合长度 indexer：分别合并计算、Q/K 读取和打分写回。
        score_flops = 2 * pairs * m["index_n_heads"] * m["index_head_dim"] / tp
        score_bytes = (n * m["index_n_heads"] * m["index_head_dim"] / tp * ref.FP8
                       + scan_tokens * m["index_head_dim"] * ref.FP8
                       + pairs * ref.FP32)
        # 混合长度下流水线下限取最长扫描。这是保留原模型的候选近似。
        score_floor = max(math.ceil(scan / 128) for scan in scans) * g["pipeline_floor_mla"]
        components["indexer_score_us"] = max(
            score_flops / ref.F_FP8 * 1e6,
            score_bytes / ref.GB / ref.B_MEM * 1e6,
            score_floor) + g["kernel_launch"]

        d_qk = m["kv_lora_rank"] + m["qk_rope_head_dim"]
        d_v = m["kv_lora_rank"]
        iterations = max(1, math.ceil(m["index_topk"] / 128))
        components["gather_us"] = max(
            kv_entries / g["iops_limit"] * 1e6,
            kv_entries * d_qk * ref.FP8 / ref.GB / ref.B_MEM * 1e6,
            iterations * g.get("pipeline_floor_gather", g["pipeline_floor_mla"])
        ) + g["kernel_launch"]

        # 保留原公式固定 top-k 的计算口径，即使上下文短于 top-k 也不裁剪。
        attention_flops = 2 * n * m["n_heads"] * m["index_topk"] * (d_qk + d_v) / tp
        attention_bytes = (n * m["n_heads"] * (d_qk + d_v) / tp * ref.FP16
                           + kv_entries * d_qk * ref.FP8)
        components["sparse_attention_us"] = max(
            attention_flops / ref.F_FP16 * 1e6,
            attention_bytes / ref.GB / ref.B_MEM * 1e6,
            iterations * g["pipeline_floor_moe"]
        ) + g["kernel_launch"]
        components["sparse_storage_us"] = (
            kv_entries * d_qk * m["kv_precision"] * (1 - sparse_HBM_hit_ratio)
            / ref.GB / g["storage_bw"] * 1e6)

    common_us = math.fsum(components[name] for name in (
        "indexer_q_us", "indexer_score_us", "gather_us", "sparse_attention_us",
        "mla_projection_us", "sparse_storage_us"))
    dense_us = common_us + components["dense_mlp_us"]
    moe_us = common_us + components["moe_us"] + components["dispatch_us"] + components["combine_us"]
    layer_us = [dense_us] * dense_layers + [moe_us] * (layers - dense_layers)
    total_us = math.fsum(layer_us)
    if not math.isfinite(total_us) or (n > 0 and total_us <= 0):
        raise ValueError("nonpositive or nonfinite modeled time")
    return {
        "model": VERSION,
        "reference_sha256": REFERENCE_SHA256,
        "requests": [list(row) for row in rows],
        "shapes_h_u": [list(row) for row in active],
        "batch_size": bs,
        "deployment": {"tp": tp, "dp": dp, "ep": ep},
        "work": {"new_tokens": n, "indexer_pairs": pairs,
                 "indexer_scan_tokens": scan_tokens, "exact_causal_pairs": exact_pairs,
                 "sparse_attention_pairs": n * m["index_topk"],
                 "kv_entries_per_read": kv_entries, "prefix_tokens": prefix_tokens},
        "sparse_HBM_hit_ratio": sparse_HBM_hit_ratio,
        "kv_hbm_reuse": kv_hbm_reuse,
        "components_us": components,
        "moe_work": moe_work,
        "dense_layer_us": dense_us, "moe_layer_us": moe_us,
        "dense_layers": dense_layers, "moe_layers": layers - dense_layers,
        "layer_durations_us": layer_us,
        "total_us": total_us, "total_ms": total_us / 1000, "duration_s": total_us / 1e6,
        "per_layer_us": total_us / layers,
        "prefix_kv_bytes": prefix_bytes,
        "per_layer_prefix_kv_gb": prefix_bytes / layers / ref.GB,
        "assumptions": {
            "moe": "conserved top-k routes, equal DP peer tokens, uniform independent routing expectations, fixed expert placement",
            "moe_time": "roofline at mean work, not expected real latency; no expert imbalance or kernel tile padding",
            "expert_communication": "conserved logical route bytes with original bandwidth/startup; includes local routes, no transport deduplication",
            "indexer_pipeline_floor": "longest request scan; not calibrated on GLM5",
            "short_sparse_attention": "retain fixed top-k work even for short contexts",
            "scope": "78 modeled layers, conserved logical expert communication at reference bandwidth/startup, optional sparse fetch; no queue, RPC, engine overhead or external prefix load",
            "validation": "structural adaptation; no GLM5/D950PR timing validation",
        },
    }


def per_batch(requests, *, dp: int, ep: int, tp: int = 1,
              sparse_HBM_hit_ratio: float = 1.0,
              kv_hbm_reuse: bool = True) -> tuple[float, float]:
    """与原 per_batch 相同的返回单位：(平均每层 us，每层前缀 KV GB)。

    批次大小由请求列表决定；不支持原 bs 覆盖。DP/EP 必须明确提供。
    KV 返回值是前缀容量口径；不可直接当作每轮需要重新读取的字节量。
    逐层流水执行应使用 estimate_batch 的 layer_durations_us，避免平均掉
    前 3 个 Dense 层与后 75 个 MoE 层的差别。
    """
    result = estimate_batch(requests, dp=dp, ep=ep, tp=tp,
                            sparse_HBM_hit_ratio=sparse_HBM_hit_ratio,
                            kv_hbm_reuse=kv_hbm_reuse)
    return result["per_layer_us"], result["per_layer_prefix_kv_gb"]


if __name__ == "__main__":
    example = [(4096, 4096), (512, 512), (512, 512), (512, 512)]
    # DP8/EP8 仅为复现既有四请求例子的固定布局，不推断 12 实例部署。
    print(json.dumps(estimate_batch(example, dp=8, ep=8), ensure_ascii=False, indent=2))
