"""独立手算工作表：不导入 sim、生产计时模块或冻结的来源算子。"""
import argparse
import json
import math
from pathlib import Path


def manual_legacy(shapes, hit=1.0, overhead=0.0):
    """固定 GLM5/D950PR 常量，直接按 FLOPs/字节和层数复算，时间单位 us。"""
    bs = len(shapes)
    il = max(h + u for h, u in shapes)
    u = max(u for _, u in shapes)
    nq = bs * u
    dp = max(1, 32 // bs)
    sl = max(1.0, il - u / 2)
    entries = bs * min(u * 2048, il)
    f8, f16, bw = 819.2e12, 409.6e12, 1280e9
    worksheet = []

    def roof(name, flops, byte_count, f=f8):
        cal, mem = flops / f * 1e6, byte_count / bw * 1e6
        result = max(cal, mem)
        worksheet.append(dict(name=name, flops=flops, bytes=byte_count,
                              compute_us=cal, memory_us=mem, result_us=result))
        return result

    q = roof("indexer_q", 2*nq*2048*32*128,
             nq*(2048+32*128) + 2048*32*128)
    score_flops = 2*nq*32*128*sl
    score_bytes = nq*32*128 + bs*sl*128 + nq*sl*4
    tiles = max(1, math.ceil(sl/128))
    score = max(score_flops/f8*1e6, score_bytes/bw*1e6, tiles*0.35) + 7
    worksheet.append(dict(name="indexer_score", flops=score_flops, bytes=score_bytes,
                          compute_us=score_flops/f8*1e6, memory_us=score_bytes/bw*1e6,
                          pipeline_floor_us=tiles*0.35, launch_us=7, result_us=score))
    gather = max(entries/1e12*1e6, entries*576/bw*1e6, 16*0.35) + 7
    attn_flops = 2*nq*64*2048*(576+512)
    attn_bytes = nq*64*576*2 + entries*576 + nq*64*512*2
    attention = max(attn_flops/f16*1e6, attn_bytes/bw*1e6, 16*0.55) + 7
    worksheet.append(dict(name="sparse_attention", flops=attn_flops, bytes=attn_bytes,
                          compute_us=attn_flops/f16*1e6, memory_us=attn_bytes/bw*1e6,
                          pipeline_floor_us=16*0.55, launch_us=7, result_us=attention))
    projection = sum(roof(f"projection_{i}", 2*nq*din*dout,
                          nq*(din+dout)+din*dout)
                     for i,(din,dout) in enumerate([
                         (6144,2048),(2048,64*256),(64*192,512),
                         (6144,576),(64*512,256),(64*256,6144),(6144,128)]))
    d, inter, mi = 6144, 12288, 2048
    mlp = sum(roof(f"dense_mlp_{i}", 2*nq*d*inter, mr+mw)
              for i,(mr,mw) in enumerate([
                  (nq*d+d*inter,nq*inter),(nq*inter+d*inter,nq*d),
                  (nq*d+d*inter,nq*inter)]))
    routed_tokens = max(bs*dp*u*8/256, 1)
    shared_tokens = bs*u
    local_experts = math.ceil(min(256, bs*dp*8)/dp)

    def experts(tokens, label):
        return sum(roof(f"{label}_{i}", 2*tokens*d*mi, mr+mw)
                   for i,(mr,mw) in enumerate([
                       (tokens*d+d*mi,tokens*mi),(tokens*mi+d*mi,tokens*d),
                       (tokens*d+d*mi,tokens*mi)]))

    moe = experts(shared_tokens, "shared") + local_experts*experts(routed_tokens, "routed")
    dispatch = 30 + local_experts*routed_tokens*6144/2000e9*1e6
    combine = 30 + local_experts*routed_tokens*6144*2/2000e9*1e6
    storage = entries*576*2*(1-hit)/350e9*1e6
    parts = dict(indexer_q_us=q, indexer_score_us=score, gather_us=gather,
                 sparse_attention_us=attention, mla_projection_us=projection,
                 sparse_storage_us=storage, dense_mlp_us=mlp, moe_us=moe,
                 dispatch_us=dispatch, combine_us=combine)
    common = q+score+gather+attention+projection+storage
    dense = common+mlp
    moe_layer = common+moe+dispatch+combine
    total_s = (3*dense+75*moe_layer)*1e-6+overhead
    return dict(duration_s=total_s, dense_layer_us=dense, moe_layer_us=moe_layer,
                components=parts, worksheet=worksheet,
                inputs=dict(bs=bs, il=il, u=u, nq=nq, dp=dp, ep=dp,
                            score_len=sl, kv_entries=entries,
                            routed_tokens=routed_tokens, local_experts=local_experts))


def manual(shapes, hit=1.0, overhead=0.0, *, tp=1, dp=8, ep=8, reuse=True):
    """独立复算实际组批v2；固定常数，不导入生产公式。"""
    n = sum(u for h, u in shapes)
    scans = [max(1., h + u / 2) for h, u in shapes]
    pairs = math.fsum(u * s for (h, u), s in zip(shapes, scans))
    entries = sum(min(u * 2048, h + u) if reuse else u * 2048 for h, u in shapes)
    f8, f16, bw = 819.2e12, 409.6e12, 1280e9
    worksheet = []

    def roof(name, flops, byte_count):
        value = max(flops / f8, byte_count / bw) * 1e6
        worksheet.append(dict(name=name, flops=flops, bytes=byte_count, result_us=value))
        return value

    q = roof('indexer_q', 2*n*2048*32*128/tp, n*(2048+32*128)/tp+2048*32*128/tp)
    score = max(2*pairs*32*128/tp/f8*1e6,
                (n*32*128/tp+math.fsum(scans)*128+pairs*4)/bw*1e6,
                max(math.ceil(s/128) for s in scans)*.35)+7
    gather = max(entries/1e12*1e6, entries*576/bw*1e6, 16*.35)+7
    attention = max(2*n*64*2048*(576+512)/tp/f16*1e6,
                    (n*64*(576+512)/tp*2+entries*576)/bw*1e6,16*.55)+7
    matrices = [(6144,2048),(2048,64*256/tp),(64*192/tp,512),
                (6144,576),(64*512/tp,256),(64*256/tp,6144),(6144,128)]
    projection = sum(roof('projection_'+str(i),2*n*a*b,n*(a+b)+a*b)
                     for i,(a,b) in enumerate(matrices))
    d, inter, mi = 6144, 12288, 2048
    mlp = sum(roof('dense_'+str(i),2*n*d*inter/tp,byte_count)
              for i,byte_count in enumerate((n*d+d*inter/tp+n*inter/tp,
                  n*inter+d*inter/tp+n*d/tp,n*inter/tp+d*inter/tp+n*d/tp)))
    total_routes = dp*n*8
    active_probability = -math.expm1(dp*n*math.log1p(-8/256))
    per_rank = []
    for rank in range(ep):
        owned = 256//ep + (rank < 256%ep)
        routes = total_routes/256*owned
        # 每专家工作量与权重读取分别计算；每个专家有3个GEMM。
        routed = 3*roof('routed_'+str(rank),2*routes*d*mi,
                       routes*(d+mi)+owned*active_probability*d*mi)
        shared = 3*roof('shared_'+str(rank),2*(n/tp)*d*mi,(n/tp)*(d+mi)+d*mi)
        dispatch = 30+routes*d/2000e9*1e6
        combine = 30+routes*d*2/2000e9*1e6
        per_rank.append((routed+shared,dispatch,combine))
    moe, dispatch, combine = max(per_rank,key=sum)
    storage = entries*576*2*(1-hit)/350e9*1e6
    parts = dict(indexer_q_us=q,indexer_score_us=score,gather_us=gather,
        sparse_attention_us=attention,mla_projection_us=projection,dense_mlp_us=mlp,
        moe_us=moe,dispatch_us=dispatch,combine_us=combine,sparse_storage_us=storage)
    common = math.fsum((q,score,gather,attention,projection,storage))
    dense, moe_layer = common+mlp, common+moe+dispatch+combine
    return dict(duration_s=math.fsum([dense]*3+[moe_layer]*75)*1e-6+overhead,
        dense_layer_us=dense,moe_layer_us=moe_layer,components=parts,worksheet=worksheet,
        inputs=dict(nq=n,indexer_pairs=pairs,kv_entries=entries,tp=tp,dp=dp,ep=ep,
                    group_routes=total_routes))


def manual_timing(timing):
    """按事件的公式版本审计；无版本字段的历史事件保留旧语义。"""
    if 'context_length_limit' in timing:
        maximum=max(h+u for h,u in timing['shapes'])
        limit=timing['context_length_limit']
        assert type(limit) is int and maximum<=limit<=300481
        assert timing['formula_context_extrapolation'] is (maximum>131072)
    version = timing.get('batch_time_model')
    args = (timing['shapes'],timing['sparse_HBM_hit_ratio'],timing['engine_overhead_s'])
    if version is None:
        return manual_legacy(*args)
    if version not in ('glm5_actual_batch_work_v2', 'glm5_direct_sum_v1'):
        raise ValueError('unsupported recorded batch_time_model: '+str(version))
    if version == 'glm5_direct_sum_v1':
        return manual_direct_sum(*args, tp=timing['reference_tp'], dp=timing['reference_dp'],
            ep=timing['reference_ep'], reuse=timing['kv_hbm_reuse'],
            fraction=timing['batch_composition']['launch_saving_fraction'])
    return manual(*args,tp=timing['reference_tp'],dp=timing['reference_dp'],
                  ep=timing['reference_ep'],reuse=timing['kv_hbm_reuse'])


def manual_direct_sum(shapes, hit=1.0, overhead=0.0, *, tp=1, dp=8, ep=8,
                      reuse=True, fraction=1.0):
    """独立直接组合审计；单请求算术仍使用本文件固定常数。"""
    singles = [manual([shape], hit, 0.0, tp=tp, dp=dp, ep=ep, reuse=reuse)
               for shape in shapes]
    saved_component = max(0, len(singles) - 1) * 7 * fraction
    components = {name: math.fsum(single['components'][name] for single in singles)
                  for name in singles[0]['components']}
    for name in ('indexer_score_us', 'gather_us', 'sparse_attention_us'):
        components[name] -= saved_component
    dense = math.fsum(single['dense_layer_us'] for single in singles) - 3 * saved_component
    moe = math.fsum(single['moe_layer_us'] for single in singles) - 3 * saved_component
    return dict(duration_s=math.fsum([dense] * 3 + [moe] * 75) * 1e-6 + overhead,
        dense_layer_us=dense, moe_layer_us=moe, components=components,
        worksheet=[dict(singleton=i, **row) for i, single in enumerate(singles)
                   for row in single['worksheet']],
        inputs=dict(singleton_inputs=[single['inputs'] for single in singles],
                    launch_saving_fraction=fraction, saved_launch_us_per_layer=3 * saved_component))


def check(paths):
    results = []
    for path in paths:
        for line_number,line in enumerate(Path(path).read_text().splitlines(), 1):
            row = json.loads(line)
            timing = row.get("timing") or {}
            if timing.get("mode") != "glm5_d950pr":
                continue
            got = manual_timing(timing)
            error = abs(got["duration_s"] - timing["duration_s"])
            for name, value in got["components"].items():
                if not math.isclose(value, timing["components"][name], rel_tol=1e-12, abs_tol=1e-9):
                    raise AssertionError((path, line_number, name, value, timing["components"][name]))
            if error > 1e-12:
                raise AssertionError((path, line_number, error))
            scheduled = row.get("num_scheduled_tokens", {})
            if sum(scheduled.values()) != sum(u for _,u in timing["shapes"]):
                raise AssertionError("timing shape tokens differ from real scheduled tokens")
            results.append(dict(path=str(path), line=line_number, step=row.get("step",row.get("step_id")),
                                program_s=timing["duration_s"], manual_s=got["duration_s"],
                                error_s=error, bs=timing["bs"], shapes=timing["shapes"],
                                sleep_elapsed_s=row.get("sleep_elapsed_s"), **got))
    if not results:
        raise AssertionError("no executed glm5_d950pr steps found")
    return dict(status="pass", steps=len(results),
                max_abs_error_s=max(r["error_s"] for r in results), rows=results)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    result = check(args.logs)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:v for k,v in result.items() if k!="rows"}))


if __name__ == "__main__":
    main()
