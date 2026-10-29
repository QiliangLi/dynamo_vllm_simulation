# -*- coding: utf-8 -*-
"""GLM5 + D950PR 逐层 prefill 计算时间（us）与 KV 加载量（GB）估算——任意每卡 batch 版。

独立脚本：不依赖本仓库其他模块。系数抄录自外部仿真器（sim_code_v2.py / models.py /
npus.py / storage_scheduling_sim.py），函数内行号注释指向该外部代码。

用途
----
估算同一张 NPU 上 bs 个请求并行 prefill 时**一层**的计算时间，以及这批请求需要加载的
逐层 KV 量。bs=1 即单请求情形（与 prefill_time_single.py 同模型）。

语义约定（继承外部 TestConfig）
------------------------------
- bs：每张 NPU 同时 prefill 的请求数；dp = 32 // bs，ep = dp * tp（外部 base.py:72 约束
  ep == tp*dp）。全局并发恒为 bs*dp（bs 整除 32 时为 32），bs 越大占用的 NPU 越少（不整除
  时有卡闲置，CLI 告警）。MoE 路由项按全局并发的 EP 分摊建模，因此"单请求时间"是系统
  满载时该请求穿过一层的时延，不是空集群单请求时延。
- input_length → full_seq_len（上下文全长，含本次新 token）；
  u_tokens → new_query_len（本次 prefill 的新 token 数，即缓存未命中的部分）。
- 异构 batch 用 per_batch()：计算按 padding 上界（批内 max 长度 × bs），KV 按各请求
  精确求和（IO 无 padding 浪费）。

对抄录版（prefill_time_anybs_sim.py，GBK 编码，无法运行）的修正
------------------------------------------------------------
F1  注意力路径加入 new_query_len。原版 indexer_qb_linear / indexer_qk_score / gather_kv /
    fused_mla_attention 只按 bs 个"单个 query token"建模，是 decode 语义；prefill 时每个
    新 token 都要经 lightning indexer 对上下文打分、各选 top-2048、再做稀疏 MLA，计算量
    与新 token 数成正比。来源：DeepSeek-V3.2 技术报告（arXiv:2512.02556）、vLLM 博文
    （vllm.ai/blog/2025-09-29-deepseek-v3-2）、FlashMLA 仓库（indices 形状 [s_q, h_kv,
    topk]，逐 query token）。ut=1 时退化为原版 decode 情形。
F2  注意力 KV 的 HBM 读取按"每层每请求的上下文至多读一遍"建模：
    entries = bs * min(ut*topk, full_seq_len)。依据：各 query 的 top-k 并集不会超过上下文；
    FlashMLA 稀疏 kernel prefill 实测 640 TFLOPS（H800）/410 TFLOPS（昇腾950，95% 峰值），
    说明 prefill 注意力是算力瓶颈而非逐 query 重复读 KV 的带宽瓶颈。kv_hbm_reuse=False
    给出无复用上界 bs*ut*topk。kernel 内精确复用因子待核实。
F3  fused MLA 的 QK 计算补 qk_rope_head_dim（后被 F11 的 absorb 口径覆盖）。
F4  dense 层（前 3 层）去掉 MoE dispatch/combine：无专家路由就没有 all-to-all 通信。
F5  字节单位统一十进制 GB（1e9），与 FLOPS 的 1e12、NPU 带宽 GB/s 及本仓库 sim/config.py
    约定一致。原版按 1024^3 折算字节却按十进制 GB/s 除，访存项低估约 7%（新版在访存受限项
    上高 7.4%，计算受限项不变），故数值与外部 sim 原输出不再逐位可比。
F6  打分扫描长度取因果中点 full_seq_len - new_query_len/2：第 i 个新 token 只能看到
    il-ut+i 个前缀 token（vLLM 博文："Due to causality, every query token only attends to
    the tokens before it"，ke = range(n-q, n)）。原版一律用全长，对首轮长 prefill（ut≈il）
    会把 indexer 二次项高估到 2 倍。
F7  bs 超过 MODEL.max_batch_size（8）时告警（原版静默接受任意 bs）。
F8  修正用法文件名；默认 trace 路径指向本仓库 mooncake_trace/derived/，缺失时报错提示。
F9  indexer 打分写回按每 (query, key) 对一个标量计：head 归约在 kernel 内完成。依据
    DeepGEMM README（github.com/deepseek-ai/DeepGEMM，mqa_logits）：
    out_ij = relu(q[i,:,:] @ kv_j) * weights[i,:] 后 .sum() 得标量，输出形状
    [seq_len, seq_len_kv]。原版按逐 head FP16 物化（index_n_heads 倍），decode 下可忽略，
    prefill 下会把长上下文的主导项高估 16 倍。标量 dtype 按 FP32（DeepGEMM 惯例，待核实；
    若为 FP16 该项再减半）。
F10 补上 MLA 线性投影（原版 DSA 层时间只含 indexer(2)+gather+fused_mla+MoE，漏掉了每个新
    token 都要过的 GEMM）：q_a(dim→q_lora)、q_b(q_lora→h·(nope+rope))、nope 侧吸收 W_UK
    (逐 head nope→kv_lora)、kv_a(dim→kv_lora+rope，生成本 token 的 KV 缓存)、注意力输出
    W_UV(逐 head kv_lora→v)、o(h·v→dim)、indexer K 投影(dim→index_head_dim)。合计每 token
    每层约 332 MFLOP，占线性层 FLOPs 的 1/3；GEMM 走 FP8 与 MoE/MLP 一致，权重每层读一遍。
    来源：DeepSeek-V3 技术报告 MLA 定义（arXiv:2412.19437 §2.1.1）；第一性原理复算见
    prefill_time/validate_prefill_time.py。
F11 注意力核心改 absorb（MQA）口径：Q/K 在潜空间 d_qk = kv_lora+rope = 576，V 为
    d_v = kv_lora = 512。来源：FlashMLA 稀疏 prefill API flash_mla_sparse_fwd 的
    q[s_q,h_q,d_qk]、kv[s_kv,h_kv,d_qk]（github.com/deepseek-ai/FlashMLA）。原版按 MHA 口径
    （192/256）算 FLOPs 却读 576 维潜 KV，不自洽，核心 FLOPs 少算 2.1 倍。

保留的外部 kernel 模型假设（未改，已核实或待核实）
- 索引 K 缓存每 token 128 维 FP8（与 vLLM "key cache of 128 per token" 一致），每请求
  每层从 HBM 读一遍；打分 Q 读一遍；top-k 在物化的 (q, n) logits 上做（vLLM 博文：
  "the logits tensor is materialized before running a row-wise topk"）。
- gather_kv 与 fused_mla 各读一遍 KV（原版 decode 情形亦如此双重计），按 128 条一 tile、
  每 tile 受 pipeline_floor 下限约束，加一次 kernel_launch。
- 稀疏 KV 存储拉取：HBM 命中 0.8，未命中部分按 kv_precision(FP16) 从 storage_bw 拉取，
  与逐层 KV 加载量（getPrefillKVLoadGB，全上下文口径）并存：前者是注意力逐层按需取，
  后者是前缀命中时的整段加载量，二者语义不同。
- q_lora_rank 等 MoE/MLP 公式、nvlink dispatch/combine、pipeline_floor 与 kernel_launch
  数值均照抄外部仿真器。

单位：时间 us；字节 GB（十进制 1e9）；带宽 GB/s；算力 TFLOPS（1e12）。

用法
----
  python prefill_time_batch.py [trace.jsonl] [bs]
    输入 jsonl 需含 input_length、u_tokens 字段
    （默认 ../mooncake_trace/derived/conversation_trace.jsonl.hit.jsonl）
    输出每行 per_layer_us、per_layer_kv_gb
  代码调用：
    from prefill_time_batch import per_request, per_batch
    us, kv = per_request(input_length, u_tokens, bs=4)
    us, kv = per_batch([(il1, ut1), (il2, ut2)], bs=2)
"""
import sys, os, json, math, warnings

# ---- 精度常量（外部 base.py）----
FP8 = 1
FP16 = 2
FP32 = 4
GB = 1e9   # 十进制 GB（F5）

# ---- 模型：ModelArgsGLM5（外部 models.py:1586）----
MODEL = {
    "model_name": "GLM5",
    "n_layers": 78, "n_dense_layers": 3,
    "dim": 6144, "inter_dim": 12288,
    "n_routed_experts": 256, "n_shared_experts": 1, "n_activated_experts": 8,
    "moe_inter_dim": 2048, "moe_compress_ratio": 1.0,
    "n_heads": 64,
    "q_lora_rank": 2048, "kv_lora_rank": 512,
    "qk_nope_head_dim": 192, "qk_rope_head_dim": 64, "v_head_dim": 256,
    "index_n_heads": 32, "index_head_dim": 128, "index_topk": 2048,
    "index_share_interval": 1,
    "kv_precision": FP16, "weight_precision": FP16, "moe_precision": FP16,
    "max_batch_size": 8, "max_seq_len": 4096 * 32, "vocab_size": 154880,
}

# ---- NPU：D950PR（外部 npus.py:63）----
NPU = {
    "fp16_flops": 512, "fp8_flops": 1024,
    "mem": 128, "mem_bw": 1600,
    "nvlink_bw": 2000, "storage_bw": 350, "pcie_bw": 100,
    "prefill_discount_cal": 0.8, "prefill_discount_mem": 0.8,
    "decode_discount_cal": 0.8, "decode_discount_mem": 0.8,
    "base_delay": 30, "kernel_launch": 7, "iops_limit": 1e12,
    "npu_core_num": 32,
    "pipeline_floor_mla": 0.35, "pipeline_floor_moe": 0.55,
}

# ---- storage_sim 固定配置（外部 storage_scheduling_sim.py:64-108）----
NUM_NPU = 32
CONF = {
    "instance_type": "prefill", "mla_type": 2, "sparse_mode": "DSA",
    "dual_pipe": False,
    "dp": NUM_NPU, "ep": NUM_NPU, "tp": 1, "routed_rep_num": 1, "shared_rep_num": 1,
    "total_bs": NUM_NPU, "bs": 1,            # bs = total_bs / dp（每卡 batch）
    "output_len": 1, "moe_compress_ratio": 1.0,
    "sparse_HBM_hit_ratio": 0.8, "per_layer_miss_rates": None,
    "kv_hbm_reuse": True,                    # F2：注意力 KV 按上下文并集读一遍
}

# prefill 折扣后的有效算力/带宽（GPU_perf getters）
F_FP16 = NPU["fp16_flops"] * NPU["prefill_discount_cal"] * 1e12   # FLOPS/s
F_FP8 = NPU["fp8_flops"] * NPU["prefill_discount_cal"] * 1e12
B_MEM = NPU["mem_bw"] * NPU["prefill_discount_mem"]               # GB/s


def _roof(cal, mem_bytes, fp=FP16, fp16_cal=False):
    """Roofline：t(us) = max(cal/FLOPS, mem_bytes*fp/GB/BW)。（外部 get_time/get_time_fp16）"""
    F = F_FP16 if fp16_cal else F_FP8
    return max(cal / F * 1e6, mem_bytes * fp / GB / B_MEM * 1e6)


def _nq(c):
    """本卡新 query token 总数（F1：prefill 的注意力/索引工作量都按它缩放）。"""
    return c["bs"] * c["new_query_len"]


def _score_len(c):
    """indexer 打分扫描长度，因果中点（F6）。"""
    return max(1.0, c["full_seq_len"] - c["new_query_len"] / 2.0)


def _kv_entries(m, c):
    """本层本卡注意力需从 HBM 读的 KV 条目数（F2）。"""
    per_req = c["new_query_len"] * m["index_topk"]
    if c.get("kv_hbm_reuse", True):
        per_req = min(per_req, c["full_seq_len"])
    return c["bs"] * per_req


def _t_indexer_qb_linear(m, c, g):
    # 外部 sim_code_v2.py:551（FP8 权重/激活）；F1：cal 与激活读写随新 token 数缩放
    nq = _nq(c); tp = c["tp"]
    cal = 2 * nq * m["q_lora_rank"] * m["index_n_heads"] * m["index_head_dim"] / tp
    mem_r = nq * (m["q_lora_rank"] + m["index_n_heads"] * m["index_head_dim"]) / tp
    mem_w = m["q_lora_rank"] * m["index_n_heads"] * m["index_head_dim"] / tp
    return _roof(cal, mem_r + mem_w, fp=FP8)


def _t_indexer_qk_score(m, c, g):
    # 外部 sim_code_v2.py:598；F1/F6/F9：打分计算、Q 读、打分写回均随新 token 数缩放，
    # 扫描长度取因果中点；索引 K 每请求读一遍（不随 token 数重复读）；
    # 打分写回每 (q,k) 对一个标量（head 归约在 kernel 内完成）
    nq = _nq(c); tp = c["tp"]
    sl = _score_len(c)
    qk_cal = 2 * nq * m["index_n_heads"] * m["index_head_dim"] / tp * sl
    q_read = nq * m["index_n_heads"] * m["index_head_dim"] / tp          # FP8，只读一遍
    k_read = c["bs"] * sl * m["index_head_dim"]                          # FP8，索引 K 每请求一遍
    score_w = nq * sl                                                    # FP32，每对一个 logit
    s2 = 128
    n_iter = max(1, math.ceil(sl / s2))
    cal_per_iter = qk_cal / F_FP8 * 1e6 / n_iter
    mem_per_iter = (q_read * FP8 + k_read * FP8 + score_w * FP32) / GB / B_MEM * 1e6 / n_iter
    t_per_iter = max(cal_per_iter, mem_per_iter, g["pipeline_floor_mla"])
    return n_iter * t_per_iter + g["kernel_launch"]


def _t_gather_kv(m, c, g):
    # 外部 sim_code_v2.py:1215；F2：按并集条目数，而非逐请求固定 topk
    io_count = _kv_entries(m, c)
    kv_dim = m["kv_lora_rank"] + m["qk_rope_head_dim"]
    data_gb = io_count * kv_dim * FP8 / GB
    iops_time = io_count / g["iops_limit"] * 1e6
    bw_time = data_gb / B_MEM * 1e6
    s2 = 128
    n_iter = max(1, math.ceil(m["index_topk"] / s2))
    pipe = g.get("pipeline_floor_gather", g["pipeline_floor_mla"])
    t_per_iter = max(iops_time / n_iter, bw_time / n_iter, pipe)
    return n_iter * t_per_iter + g["kernel_launch"]


def _t_fused_mla_attention(m, c, g):
    # 外部 sim_code_v2.py:1262；F1/F2/F11：计算与 Q 读写随新 token 数缩放、KV 按并集条目读、
    # absorb 口径——Q/K 潜空间 d_qk = kv_lora+rope，V 为 d_v = kv_lora
    nq = _nq(c); tp = c["tp"]
    seq_len = m["index_topk"]
    d_qk = m["kv_lora_rank"] + m["qk_rope_head_dim"]
    d_v = m["kv_lora_rank"]
    sfa_cal = 2 * nq * m["n_heads"] * seq_len * (d_qk + d_v) / tp
    mem_q = nq * m["n_heads"] * d_qk / tp * FP16
    mem_kv = _kv_entries(m, c) * d_qk * FP8
    mem_w = nq * m["n_heads"] * d_v / tp * FP16
    pipe = g["pipeline_floor_moe"]
    s2 = 128
    n_iter = max(1, math.ceil(seq_len / s2))
    cal_per_iter = sfa_cal / F_FP16 * 1e6 / n_iter
    mem_per_iter = (mem_q + mem_kv + mem_w) / GB / B_MEM * 1e6 / n_iter
    t_per_iter = max(cal_per_iter, mem_per_iter, pipe)
    return n_iter * t_per_iter + g["kernel_launch"]


def _t_mla_proj(m, c, g):
    # F10：MLA 线性投影（absorb 模式），原版 DSA 层未计。每个 GEMM 单独 roofline，
    # 激活按 FP8 读入+写出，权重 FP8 每层读一遍；逐 head 的吸收矩阵按 (h·in, out) 计
    # FLOPs/字节与分块对角形式相同
    nq = _nq(c); tp = c["tp"]; h = m["n_heads"]
    gemms = (
        (m["dim"], m["q_lora_rank"]),                                               # q_a
        (m["q_lora_rank"], h * (m["qk_nope_head_dim"] + m["qk_rope_head_dim"]) / tp),  # q_b
        (h * m["qk_nope_head_dim"] / tp, m["kv_lora_rank"]),                      # W_UK 吸收
        (m["dim"], m["kv_lora_rank"] + m["qk_rope_head_dim"]),                      # kv_a
        (h * m["kv_lora_rank"] / tp, m["v_head_dim"]),                              # W_UV
        (h * m["v_head_dim"] / tp, m["dim"]),                                       # o
        (m["dim"], m["index_head_dim"]),                                            # indexer K
    )
    t = 0.0
    for din, dout in gemms:
        cal = 2 * nq * din * dout
        mem = nq * (din + dout) + din * dout
        t += _roof(cal, mem, fp=FP8)
    return t


def _t_mlp(m, c, g):
    # 外部 mlp_time → mlp_1of3/2of3/3of3（GLM5 走 FP8 计算）；bs*nql 已含新 token 数
    bs = c["bs"]; tp = c["tp"]; nql = c["new_query_len"]
    # 1of3: dim→inter_dim
    cal = 2 * bs * nql * m["dim"] * m["inter_dim"] / tp
    mem_r = bs * nql * m["dim"] + m["dim"] * m["inter_dim"] / tp
    mem_w = bs * nql * m["inter_dim"] / tp
    t = _roof(cal, mem_r + mem_w, fp=FP8)
    # 2of3: inter_dim→inter_dim
    cal = 2 * bs * nql * m["dim"] * m["inter_dim"] / tp
    mem_r = bs * nql * m["inter_dim"] + m["dim"] * m["inter_dim"] / tp
    mem_w = bs * nql * m["dim"] / tp
    t += _roof(cal, mem_r + mem_w, fp=FP8)
    # 3of3: inter_dim→dim
    cal = 2 * bs * nql * m["inter_dim"] * m["dim"] / tp
    mem_r = bs * nql * m["inter_dim"] / tp + m["dim"] * m["inter_dim"] / tp
    mem_w = bs * nql * m["dim"] / tp
    t += _roof(cal, mem_r + mem_w, fp=FP8)
    return t * c["moe_compress_ratio"]


def _routed_moe_bs(m, c):
    # 外部 sim_code_v2.py:28
    return max(c["bs"] * c["dp"] * c["new_query_len"] * m["n_activated_experts"]
               / c["routed_rep_num"] / m["n_routed_experts"], 1)


def _shared_moe_bs(m, c):
    # 外部 sim_code_v2.py:17
    return c["bs"] * c["dp"] * c["new_query_len"] * m["n_shared_experts"] / c["ep"] / c["shared_rep_num"]


def _t_moe(m, c, g):
    # 外部 moe_time：shared 1of3/2of3/3of3（op_num=1）+ routed 1of3/2of3/3of3（op_num=n_route）
    bs_r = _routed_moe_bs(m, c); bs_s = _shared_moe_bs(m, c)
    dim = m["dim"]; mi = m["moe_inter_dim"]
    n_route = math.ceil(min(m["n_routed_experts"], c["bs"] * c["dp"] * m["n_activated_experts"]) / c["ep"])
    # shared 1of3
    cal = 2 * bs_s * dim * mi; mem_r = bs_s * dim + dim * mi * c["moe_compress_ratio"]; mem_w = bs_s * mi
    t = _roof(cal, mem_r + mem_w, fp=FP8)
    # shared 2of3
    cal = 2 * bs_s * dim * mi; mem_r = bs_s * mi + dim * mi * c["moe_compress_ratio"]; mem_w = bs_s * dim
    t += _roof(cal, mem_r + mem_w, fp=FP8)
    # shared 3of3
    cal = 2 * bs_s * dim * mi; mem_r = bs_s * dim + dim * mi * c["moe_compress_ratio"]; mem_w = bs_s * mi
    t += _roof(cal, mem_r + mem_w, fp=FP8)
    # routed 1of3/2of3/3of3（op_num = n_route）
    for (mr, mw) in [(bs_r * dim + dim * mi, bs_r * mi),
                     (bs_r * mi + dim * mi, bs_r * dim),
                     (bs_r * dim + dim * mi, bs_r * mi)]:
        cal = 2 * bs_r * dim * mi
        t += _roof(cal, mr + mw, fp=FP8) * n_route
    return t


def _t_moe_dispatch(m, c, g):
    # 外部 sim_code_v2.py:1964
    size_gb = FP8 * _routed_moe_bs(m, c) * m["dim"] / GB
    n_route = math.ceil(min(m["n_routed_experts"], c["bs"] * c["dp"] * m["n_activated_experts"]) / c["ep"])
    return g["base_delay"] + n_route * size_gb / g["nvlink_bw"] * 1e6


def _t_moe_combine(m, c, g):
    # 外部 sim_code_v2.py:1981
    size_gb = FP16 * _routed_moe_bs(m, c) * m["dim"] / GB
    n_route = math.ceil(min(m["n_routed_experts"], c["bs"] * c["dp"] * m["n_activated_experts"]) / c["ep"])
    return g["base_delay"] + n_route * size_gb / g["nvlink_bw"] * 1e6


def _t_sparse_kv_load(m, c, g):
    # 外部 sim_code_v2.py:1997 + getSparseFetchKVCacheSizeOneLayer（DSA）+ miss_rate；
    # F2：拉取条目数随并集口径缩放
    fetch = (m["kv_lora_rank"] + m["qk_rope_head_dim"]) * _kv_entries(m, c) * m["kv_precision"]
    miss_rate = 1 - c["sparse_HBM_hit_ratio"]
    load_gb = fetch / GB * miss_rate
    return load_gb / g["storage_bw"] * 1e6


def _t_dense_layer(m, c, g):
    """dense 层 DSA：indexer(2) + gather_kv + fused_mla_attention + mla_proj + mlp。

    F4：dense 层无专家路由，不含 MoE dispatch/combine 通信，只有稀疏 KV 拉取。
    """
    cal = (_t_indexer_qb_linear(m, c, g) + _t_indexer_qk_score(m, c, g)
           + _t_gather_kv(m, c, g) + _t_fused_mla_attention(m, c, g) + _t_mla_proj(m, c, g)
           + _t_mlp(m, c, g))
    comm = max(0, _t_sparse_kv_load(m, c, g))
    return cal + comm


def _t_moe_layer(m, c, g):
    """MoE 层 DSA：indexer(2) + gather_kv + fused_mla_attention + mla_proj + moe(shared+routed) + 通信。"""
    cal = (_t_indexer_qb_linear(m, c, g) + _t_indexer_qk_score(m, c, g)
           + _t_gather_kv(m, c, g) + _t_fused_mla_attention(m, c, g) + _t_mla_proj(m, c, g)
           + _t_moe(m, c, g))
    comm = max(0, _t_moe_dispatch(m, c, g)) + max(0, _t_moe_combine(m, c, g)) + max(0, _t_sparse_kv_load(m, c, g))
    return cal + comm


def per_request(input_length, u_tokens, bs=1):
    """返回（每层计算时间 us，每层 KV 加载量 GB）。

    bs 个相同（input_length, u_tokens）请求在同一张 NPU 上并行 prefill 的一层时间。
    系数全部可溯源：MODEL/NPU/CONF 字典 + 上方算子公式；两个 trace 字段映射到
    full_seq_len / new_query_len（外部 storage_scheduling_sim.py:101-102）。
    bs 语义镜像外部 TestConfig.bs（每卡 batch），dp/ep 随之收缩，见模块 docstring。

    - bs=1（默认）：单请求单卡，dp=32（每卡 1 个请求）
    - bs=N：每卡 N 个请求，dp=32//N（要求 N 整除 32，否则有卡闲置）；计算时间随 N
      亚线性增长（权重读摊薄），KV 量随 N 线性增长
    """
    if bs < 1:
        raise ValueError(f"bs={bs} 必须 >= 1")
    if bs > MODEL["max_batch_size"]:
        warnings.warn(f"bs={bs} 超过 MODEL.max_batch_size={MODEL['max_batch_size']}（F7）")
    if u_tokens < 0 or u_tokens > input_length:
        raise ValueError(f"u_tokens={u_tokens} 需满足 0 <= u_tokens <= input_length={input_length}")
    c = dict(CONF)
    c["bs"] = bs
    c["dp"] = max(1, NUM_NPU // bs)
    c["ep"] = c["dp"] * c["tp"]   # 外部 base.py:72 约束 ep == tp*dp
    c["total_bs"] = bs * c["dp"]  # 全局并发 = 每卡 bs * dp
    c["full_seq_len"] = input_length
    c["new_query_len"] = u_tokens
    n_layers = MODEL["n_layers"]; n_dense = MODEL["n_dense_layers"]
    # ---- KV 加载量：外部 models.py:315 getPrefillKVLoadGB（非 V4 分支），F5 改十进制 GB ----
    cached = max(0, input_length - u_tokens)
    kv_mla = (MODEL["kv_lora_rank"] + MODEL["qk_rope_head_dim"]) * cached * c["bs"] * MODEL["kv_precision"]
    kv_idx = MODEL["index_head_dim"] * cached * c["bs"] * MODEL["kv_precision"]
    total_kv_gb = (kv_idx * math.ceil(n_layers / MODEL["index_share_interval"]) + kv_mla * n_layers) / GB
    per_layer_kv_gb = total_kv_gb / n_layers
    if u_tokens == 0:                 # 全命中、无新 token：没有 prefill 计算
        return 0.0, per_layer_kv_gb
    t_total = n_dense * _t_dense_layer(MODEL, c, NPU) + (n_layers - n_dense) * _t_moe_layer(MODEL, c, NPU)
    return t_total / n_layers, per_layer_kv_gb


def per_batch(requests, bs=None):
    """异构 batch 的（每层计算时间 us，每层 KV 加载量 GB）。

    padding 上界模型（LLM prefill 常规口径）：
    - full_seq_len = 批内 max(input_length)，new_query_len = 批内 max(u_tokens)，
      bs = len(requests)（可覆盖，须整除 32 才不留闲置卡）
    - 每层计算时间 = per_request(max_il, max_ut, bs)（短请求按长请求 padding）
    - KV 加载量 = 各请求精确值求和（IO 上无 padding 浪费）

    Args:
        requests: (input_length, u_tokens) 元组列表
        bs: 每卡 batch（默认 len(requests)）
    """
    n = len(requests)
    if n == 0:
        return 0.0, 0.0
    if bs is None:
        bs = n
    if bs < 1:
        raise ValueError(f"bs={bs} 必须 >= 1")
    max_il = max(r[0] for r in requests)
    max_ut = max(r[1] for r in requests)
    per_layer_us, _ = per_request(max_il, max_ut, bs=bs)
    per_layer_kv_gb = 0.0
    for il, ut in requests:
        _, kv = per_request(il, ut, bs=1)
        per_layer_kv_gb += kv
    return per_layer_us, per_layer_kv_gb


_DEFAULT_TRACE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "mooncake_trace", "derived",
                              "conversation_trace.jsonl.hit.jsonl")


def _check_bs(bs):
    if NUM_NPU % bs != 0:
        print(f"WARNING: bs={bs} 不整除 NUM_NPU={NUM_NPU}；dp={NUM_NPU // bs}（有卡闲置）",
              file=sys.stderr)
    if bs > MODEL["max_batch_size"]:
        print(f"WARNING: bs={bs} 超过 MODEL.max_batch_size={MODEL['max_batch_size']}（F7）",
              file=sys.stderr)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_TRACE
    bs = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    if bs < 1:
        print(f"ERROR: bs={bs} 必须 >= 1", file=sys.stderr)
        sys.exit(1)
    _check_bs(bs)
    warnings.filterwarnings("ignore", message=r"bs=\d+ 超过 MODEL\.max_batch_size")  # CLI 已打印过
    if not os.path.exists(path):
        print(f"ERROR: trace 文件不存在：{path}\n"
              f"  本仓库可用输入：mooncake_trace/derived/*.hit.jsonl（512 token 块）、"
              f"AgentX及trace转换分析/转换结果/agentx_mooncake_full.jsonl（64 token 块）",
              file=sys.stderr)
        sys.exit(1)
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                r = json.loads(line)
                us, kv = per_request(r['input_length'], r['u_tokens'], bs=bs)
                print(f"input_length={r['input_length']}\tu_tokens={r['u_tokens']}\tbs={bs}\t"
                      f"per_layer_us={us:.3f}\tper_layer_kv_gb={kv:.6e}")
    except BrokenPipeError:
        # 下游（如 head）提前关闭管道属正常情形；重定向 stdout 以免解释器退出时再报一次
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
