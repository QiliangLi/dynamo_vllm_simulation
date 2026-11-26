"""Build and archive reports of the four-worker bandwidth-ledger comparison.

Run `build` after the three simulations finish, inspect the seven PNGs, then
run `archive`. Both operations use frozen run logs and never start a service.
"""
import argparse
import base64
from collections import Counter, defaultdict
import csv
from datetime import datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import struct
import subprocess
import sys
from zoneinfo import ZoneInfo


REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / 'skills/report-organizer/scripts'
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(REPO / 'dynamo_vllm_cpu_sim'))

BENCHMARKS = {
    '408': 'synthetic_abchalfb_408_8s_4_25GBps',
    '540': 'synthetic_abclonghit_540_12s_4_25GBps',
    'mooncake': 'mooncake_head875_360_12s_4_25GBps',
    'agentx-narrow': 'agentx_narrow_358_12s_4_25GBps',
    'agentx-flex': 'agentx_flex_358_12s_4_25GBps',
}
POLICIES = {
    'light': ('默认全轻读', '#888888'),
    'tfs': ('普通 TFS', '#55A868'),
    'bandwidth': ('带宽账本 TFS', '#9974A5'),
}


def read(path):
    return json.loads(path.read_bytes())


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def info(path):
    return dict(sha256=digest(path), bytes=path.stat().st_size)


def percentile(values, q):
    return sorted(values)[math.ceil(q * len(values)) - 1]


def log_period(run):
    """Use the frontend's real UTC log timestamps for the experiment date."""
    pattern = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z')
    begin = end = None
    with (run / 'frontend.log').open() as stream:
        for line in stream:
            match = pattern.search(line)
            if match:
                end = datetime.fromisoformat(match.group().replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Shanghai'))
                if begin is None:
                    begin = end
    assert begin is not None and end is not None
    return dict(frontend_start=begin.isoformat(), frontend_stop=end.isoformat())


def reconstruct_bandwidth(storage, cfg, origin, span, starts, ends, costs):
    """Redistribute recorded GET submissions on the same shared six-disk model.

    Demand is the sum of each executing request's actual bytes/model compute,
    from its first read (or compute start) through last layer compute. Actual
    allocation is the 50 ms average of the replayed cumulative transfer bytes.
    """
    import numpy as np
    from sim.grouped_storage import GroupedStorage

    assert cfg['wall_time_scale'] == 1 and cfg['workers'] == 4
    sc = cfg['storage']
    assert sc['disks'] == 6 and sc['shared_link_Bps'] == 25e9
    assert sc['bandwidth_policy'] == 'max_min_fair'
    edges = np.r_[np.arange(0, span, .05), span]
    widths = np.diff(edges)
    shift = origin - storage['t0_monotonic_s']
    store = GroupedStorage(sc, cfg['block_size'])
    loads = storage['layer_loads']
    history = sorted(storage['layer_read_history'], key=lambda row: row['submitted_s'])
    recorded = {(row['rid'], row['layer']): row['completed_s'] for row in history}
    actions = [(row['submitted_s'], index, row) for index, row in enumerate(history)]
    actions += [(shift + float(t), -1, None) for t in edges]
    cumulative = []
    checked = {}
    for t, _, row in sorted(actions):
        while store.now < t:
            target = min(t, store.next_time())
            for key in store.advance(target):
                checked[key] = target
        if row is None:
            cumulative.append(store.bytes_transferred)
        else:
            rid = row['rid']
            load = loads[rid]
            counts = {int(d): n for d, n in load['counts'].items()}
            store.submit_counted(t, (rid, row['layer']), counts,
                                 row['bytes'] / sum(counts.values()),
                                 load['demand'], row['read_latency_s'])
    assert checked.keys() == recorded.keys()
    err = max(abs(checked[k] - t) for k, t in recorded.items())
    assert err < 1e-6, err
    actual = np.diff(cumulative) / widths
    assert max(actual) <= 25e9 + 1e5 and min(actual) >= -1e-6
    assert abs(sum(np.diff(cumulative)) - storage['bytes_transferred']) < 1
    demand_volume = np.zeros_like(widths)
    first = {row['rid']: row['submitted_s'] + storage['t0_monotonic_s']
             for row in history if row['layer'] == 0}
    for rid, load in loads.items():
        key = rid.replace('-', '').lower()[:32]
        left = min(first[rid], starts[key]) - origin
        right = ends[key] - origin
        assert -1e-6 <= left < right <= span + 1e-6
        demand_volume += load['total_bytes'] / costs[key] * np.maximum(
            0, np.minimum(edges[1:], right) - np.maximum(edges[:-1], left))
    demand = demand_volume / widths
    intervals = [dict(start_s=float(a), end_s=float(b),
                      actual_Bps=min(25e9, max(0., float(v))), demand_Bps=float(d))
                 for a, b, v, d in zip(edges, edges[1:], actual, demand)]
    audit = dict(max_replay_error_us=err * 1e6, layer_reads=len(history),
                 read_bytes=storage['bytes_transferred'], demand_peak_GBps=float(max(demand)) / 1e9,
                 over_s=float(widths[demand > 25e9].sum()))
    band = dict(capacity_Bps=25e9, read_bytes=storage['bytes_transferred'],
                intervals=intervals, source='逐层 GET 提交事件在原六盘共享链路模型重放，逐次完成核对；50ms 积分分桶。需求按执行中请求的实际读取字节/名义计算时间求和。')
    return band, audit


def adapt_case(case, run, rows):
    mode = case['summary']['case']
    cfg = case['config']
    client = read(run / 'report.json')['requests']
    ledger = read(run / 'admission.final.json')
    storage = read(run / 'storage.final.json')
    gate = read(run / 'read_admission.check.json')
    check = read(run / 'check.json')
    assert check['status'] == gate['status'] == read(run / 'layer_check.json')['status'] == 'pass'
    mapping = read(run / 'request_ids.json')
    reverse = {v: k for k, v in mapping.items()}
    assert set(reverse) == set(client) == {r['id'] for r in case['requests']}
    assert mapping.keys() == rows.keys()
    origin = ledger['origin_monotonic_s'] + min(ledger['records'][rid]['profile']['arrival_s'] for rid in client)
    starts, ends, prev_ends = {}, {}, {}
    costs, waits = defaultdict(float), defaultdict(Counter)
    for path in run.glob('worker*.steps.jsonl'):
        with path.open() as stream:
            for event in map(json.loads, stream):
                kind = event.get('event')
                if kind in ('layer_compute_start', 'layer_compute_complete'):
                    rid, = event['req_ids']
                    key = rid.replace('-', '').lower()[:32]
                    t = event['occurred_mono']
                    layer = event['layer']
                    if kind == 'layer_compute_start' and layer == 0:
                        starts.setdefault(key, t)
                    elif kind == 'layer_compute_complete':
                        prev_ends.setdefault((key, layer), t - storage['t0_monotonic_s'])
                        if layer == 15:
                            ends[key] = t
                elif kind is None and 'timing' in event and event.get('req_ids'):
                    rid, = event['req_ids']
                    key = rid.replace('-', '').lower()[:32]
                    costs[key] += event['timing']['duration_s']
    assert starts.keys() == ends.keys() == costs.keys() == client.keys()
    for io in storage['layer_read_history']:
        rid = io['rid'].replace('-', '').lower()[:32]
        layer = io['layer']
        w = waits[rid]
        w['bytes'] += io['bytes']
        w['layers'] += 1
        if layer:
            w['later_wait_s'] += max(0., io['completed_s'] - prev_ends[rid, layer - 1])
        else:
            w['first_wait_s'] += io['completed_s'] - io['submitted_s']
    details = []
    times = []
    classification = Counter()
    for request in case['requests']:
        rid = request['id']
        source_id = reverse[rid]
        row = rows[source_id]
        profile = ledger['records'][rid]['profile']
        hint = ledger['storage_hints'][rid]
        w = waits[rid]
        assert abs(profile['deadline_s'] - profile['arrival_s'] -
                   cfg['request_profile']['slo_multiplier'] * profile['reference_service_s']) < 1e-7
        assert w['bytes'] == int(row['read_bytes']) and w['layers'] == (16 if w['bytes'] else 0)
        assert hint['estimated_remote_read_bytes'] == int(row['read_bytes'])
        request['cohort'] = row['class_id']
        request['read_bytes'] = int(row['read_bytes'])
        ttft = client[rid]['ttft_s']
        times.append(ttft)
        heavy = bool(hint['is_heavy_read'])
        classification[row['class_id'], heavy] += 1
        details.append(dict(mode=mode, id=source_id, worker=request['worker'], cohort=row['class_id'],
                            H=int(row['hit_tokens']), U=int(row['new_tokens']), pressure_Bps=profile['pressure_Bps'],
                            ledger_pressure_Bps=hint.get('pressure_Bps'),
                            online_heavy=int(heavy), nominal_compute_s=costs[rid],
                            first_wait_s=w['first_wait_s'], later_wait_s=w['later_wait_s'],
                            client_ttft_s=ttft, read_bytes=w['bytes']))
    assert sum(r['read_bytes'] for r in details) == storage['bytes_transferred']
    band, audit = reconstruct_bandwidth(storage, cfg, origin, case['summary']['span_s'], starts, ends, costs)
    case['bandwidth'] = band
    case['bandwidth_unavailable'] = None
    case['layers'] = []  # Exact compute/IO intervals remain in the portable dataset.
    for name in ('request_ids.json', 'read_admission.check.json', 'check.json', 'layer_check.json', 'frontend.log'):
        case['sources'][str(run / name)] = info(run / name)
    denied_ids = set()
    selected = Counter()
    for path in run.glob('worker*.read_admission.jsonl'):
        with path.open() as stream:
            for event in map(json.loads, stream):
                if event['event'] == 'read_acquire_response' and event.get('granted') is False and event.get('reason') == 'full':
                    denied_ids.add(event['request_id'].replace('-', '').lower()[:32])
                elif event['event'] == 'read_skipped_full':
                    denied_ids.add(event['request_id'].replace('-', '').lower()[:32])
                elif event['event'] == 'bandwidth_light_selected':
                    selected[event['source'], event['fitted']] += 1
        case['sources'][str(path)] = info(path)
    summary = dict(mode=mode, client_finish_s=max(r['client_finish_s'] for r in case['requests']),
                   mean_ttft_s=statistics.mean(times), p95_ttft_s=percentile(times, .95),
                   first_wait_s=sum(w['first_wait_s'] for w in waits.values()),
                   later_wait_s=sum(w['later_wait_s'] for w in waits.values()),
                   heavy=sum(classification[k, True] for k in {r['class_id'] for r in rows.values()}),
                   grants=gate['grants'], denials=gate['denied'], skipped=gate['skipped_heavy_requests'],
                   acquire_rpc_mean_ms=gate['acquire_rpc_mean_wall_ms'],
                   acquire_rpc_p95_ms=gate['acquire_rpc_p95_wall_ms'],
                   denied_or_skipped_requests=len(denied_ids),
                   **audit)
    if cfg['admission'].get('bandwidth_permits'):
        summary.update(peak_held_GBps=gate['peak_held_Bps']/1e9,
                       oversized_alone_grants=gate['oversized_alone_grants'],
                       final_held_GBps=gate['final_held_Bps']/1e9,
                       light_selected_current_denial_fit=selected['current_denial', True],
                       light_selected_current_denial_fallback=selected['current_denial', False],
                       light_selected_previous_denial_fit=selected['previous_denial', True],
                       light_selected_previous_denial_fallback=selected['previous_denial', False])
    elif mode == 'tfs':
        caps = Counter(event['cap'] for event in ledger['events'] if event['event'] == 'threshold_updated')
        summary['count_cap_updates'] = json.dumps(dict(caps), separators=(',', ':'))
    original = read(run.parent / 'summary.json')
    for key, field in [('client_finish_s', 'makespan_s'), ('mean_ttft_s', 'mean_ttft_s'), ('p95_ttft_s', 'p95_ttft_s')]:
        assert abs(summary[key] - original[field]) < 1e-7, (mode, key)
    summary.update({f'worker{i}_finish_s': original['worker_finish_s'][i] for i in range(4)})
    summary['mean_worker_finish_s'] = original['mean_worker_finish_s']
    assert summary['heavy'] == original['heavy']
    for key in sorted({r['class_id'] for r in rows.values()}):
        summary[f'{key}_heavy'] = classification[key, True]
        summary[f'{key}_total'] = classification[key, True] + classification[key, False]
    return summary, details


def metric_table(summaries):
    from plotting.report import table
    return table(['指标', *[POLICIES[s['mode']][0] for s in summaries]], [
        [label, *[f"{s[field]:.3f}" for s in summaries]] for field, label in (
            ('client_finish_s', '客户端整批完成（s）'), ('mean_worker_finish_s', '四 worker 完成时刻均值（s）'),
            ('mean_ttft_s', '客户端平均 TTFT（s）'), ('p95_ttft_s', '客户端 P95 TTFT（s）'),
            ('slo_pct', 'SLO 满足率（%）'), ('first_wait_s', '首层累计 KV 等待（s）'),
            ('later_wait_s', '后 15 层累计 KV 等待（s）'), ('total_wait_s', '累计 KV 等待合计（s）'))])


def build(audit, case_name, stage):
    from plotting.dataset import native_dataset, validate_dataset
    from plotting.report import table, metrics
    benchmark = REPO / 'benchmark' / BENCHMARKS[case_name]
    manifest = read(benchmark / 'manifest.json')
    with (benchmark / 'requests.csv').open(encoding='utf-8-sig') as stream:
        rows = {r['id']: r for r in csv.DictReader(stream)}
    stage.mkdir(parents=True, exist_ok=False)
    spec = dict(schema_version=1, title=f"{case_name}｜四实例 25 GB/s，默认、普通 TFS 与带宽账本", cases=[])
    for mode, (label, color) in POLICIES.items():
        run = audit / f'{case_name}-{mode}' / 'run'
        assert digest(run.parent / 'input/trace.jsonl') == manifest['original_trace_sha256']
        spec['cases'].append(dict(id=mode, label=label, color=color, path=str(run),
                                  run_metadata=dict(repeat=1, ordering='FCFS', alpha=.1, **log_period(run))))
    write(stage / 'cases.json', spec)
    data = native_dataset(manifest=stage / 'cases.json')
    summaries, all_details = [], []
    for item, case in zip(spec['cases'], data['cases']):
        summary, details = adapt_case(case, Path(item['path']), rows)
        summaries.append(summary)
        all_details.extend(details)
    validate_dataset(data)
    result = metrics(data, 50)
    for s, derived in zip(summaries, result['summary']):
        s['slo_pct'] = derived['slo_pct']
        assert abs(s['mean_worker_finish_s'] - derived['mean_worker_finish_s']) < 1e-7
        s['total_wait_s'] = s['first_wait_s'] + s['later_wait_s']
    data['auxiliary_tables'] = dict(client_comparison=summaries, request_wait_details=all_details)
    data['execution_boundary'] = '本轮各方案新运行一次 CPU 进程栈仿真；本报告离线读取冻结日志。'
    dates = sorted({item['run_metadata'][key][:10] for item in spec['cases'] for key in ('frontend_start', 'frontend_stop')})
    data['metadata'] = {'实验日期': '、'.join(dates), '固定负载': manifest['case'],
                        '请求数 / 到达窗口': f"{manifest['requests']} / {manifest['arrival_window_s']} s",
                        '输入 SHA256': manifest['original_trace_sha256'],
                        '读量': f"{manifest['read_bytes']/1e9:.3f} GB",
                        '资源': '四实例、每实例 BS=1、MiniMax 16 层、6 盘共享 25 GB/s，FCFS，α=0.1，关闭强制准入',
                        '带宽': '50ms 积分分桶；实际分配由逐层提交事件在共享六盘模型离线重放；理论需求按执行请求求和',
                        '客户端与网关': '第 0 节使用客户端 TTFT 和完成时间；标准 CDF/主表采用 central 画像到首 token；SLO 使用冻结 deadline。',
                        'SLO 定义': '逐请求 deadline=到达时刻+4×原画像 reference_service_s；保留每条期限和按原公式得到的名义计算。'}
    base, old, new = summaries
    diff = lambda s, k: (new[k] / s[k] - 1) * 100 if s[k] else None
    comparison = '\n'.join(f"- 相比{POLICIES[s['mode']][0]}，新 TFS 的整批完成时间变化 {diff(s, 'client_finish_s'):+.1f}%，平均 TTFT 变化 {diff(s, 'mean_ttft_s'):+.1f}%，四实例平均完成时刻变化 {diff(s, 'mean_worker_finish_s'):+.1f}%。" for s in (base, old))
    classes = sorted({r['class_id'] for r in rows.values()})
    classification = table(['策略', *[f'{c} 判重/总数' for c in classes], '许可获批', '拒绝次数', '曾被拒绝或跳过的请求数'], [
        [POLICIES[s['mode']][0], *[f"{s[f'{c}_heavy']}/{s[f'{c}_total']}" for c in classes],
         s['grants'], s['denials'], s['denied_or_skipped_requests']] for s in summaries])
    data['narrative'] = {
        '0': comparison + '\n\n' + metric_table(summaries) + '\n上表的完成时间和 TTFT 为客户端口径；后续标准主表与 CDF 为 central 画像口径。',
        '6': '默认强制全轻读，实际 KV 读取保持一致。普通 TFS 冻结进入时的 P75 轻重标记，以重读条数计许可。新方案增加均摊门槛 6.25 GB/s，以原模型需求判重，再将画像带宽四舍五入到整数 B/s，用于 RPC、账本与本地选择。被拒后用剩余带宽和均摊门槛选轻读。\n\n' + classification + '\n'
             + f"新 TFS 的累计 KV 等待为 {new['total_wait_s']:.3f} s，普通 TFS 为 {old['total_wait_s']:.3f} s，默认策略为 {base['total_wait_s']:.3f} s。首层等待取提交至存储完成；逐层等待取上一层计算完成至下一层读取完成的正差。跨 worker 求和，不能直接从整批完成时间扣除。时间线红色为在途读取减名义计算的区间，口径与依赖等待不同。",
        '7': '本次每种策略仅执行一次。独立路由和 CPU 调度时序有波动。模型计算占比和模拟分配带宽不等于真机 NPU 与 OD 测量；无法据此确认 AIV/HBM 竞争。轻读没有全局预算预留，过载仍可能出现。',
        '8': '逐请求 H/U、实际归属、理论需求和 KV 等待详见[请求明细](report_data/request_wait_details.csv)。客户端主指标及逐次读取重放最大误差详见[客户端对照](report_data/client_comparison.csv)。',
        '10': (f"对比普通 TFS，带宽账本的 50ms 理论需求峰值从 {old['demand_peak_GBps']:.2f} 变为 {new['demand_peak_GBps']:.2f} GB/s。"
               f"客户端平均 TTFT 变化 {diff(old, 'mean_ttft_s'):+.1f}%，P95 变化 {diff(old, 'p95_ttft_s'):+.1f}%，"
               f"SLO 满足率变化 {new['slo_pct']-old['slo_pct']:+.1f} 个百分点。"
               '这些指标从不同角度描述一次回放，结合时间线看排队长尾。'),
    }
    data['narrative']['6'] += (f"\n\n新方案账本峰值为 {new['peak_held_GBps']:.3f} GB/s；自身需求大于 25 GB/s 且单独获批 {new['oversized_alone_grants']} 次；最后账本归零。"
        f"本次拒绝后选择轻读：符合上限 {new['light_selected_current_denial_fit']} 次，选择最小需求兜底 {new['light_selected_current_denial_fallback']} 次。"
        f"沿用上次拒绝上限：符合上限 {new['light_selected_previous_denial_fit']} 次，兜底 {new['light_selected_previous_denial_fallback']} 次。以上为选队事件计数，同一请求可能多次被选。")
    data['narrative']['6'] += (f"\n\n普通 TFS 的额度更新次数按额度分布为 `{old['count_cap_updates']}`。"
        f"普通 TFS 的申请 RPC 平均/P95 为 {old['acquire_rpc_mean_ms']:.3f}/{old['acquire_rpc_p95_ms']:.3f} ms，"
        f"新方案为 {new['acquire_rpc_mean_ms']:.3f}/{new['acquire_rpc_p95_ms']:.3f} ms。拒绝次数包含重复申请。")
    if case_name == '540':
        data['narrative']['10'] += (f"\n\n普通 TFS 判重 {old['B_heavy']} 条 B，申请拒绝 {old['denials']} 次。"
            f"带宽账本判重 {new['B_heavy']} 条 B，批准 {new['oversized_alone_grants']} 次超 25 GB/s 的单独读取。"
            f"后 15 层累计等待从 {old['later_wait_s']:.3f} s 降为 {new['later_wait_s']:.3f} s。")
    elif case_name.startswith('agentx') or case_name == 'mooncake':
        data['narrative']['10'] += (f"\n\n带宽账本标重 {new['A_heavy']}/{new['A_total']} 条 A。"
            '其余 A 按轻读放行，仍会与重读共享链路。图 4 的峰值同账本峰值采用不同口径。')
    for mode, suffix, reason in (
            ('tfs', 'failed-suspend', '跨宿主休眠，客户端回放失败'),
            ('bandwidth', 'failed-metadata', 'Dynamo Python 端严格字段校验拒绝新增带宽画像字段'),
            ('bandwidth', 'failed-rpc', '小数带宽经 RPC 往返发生数值差异，准入验收未通过'),
            ('bandwidth', 'superseded-float', '小数带宽版本曾通过，随后统一按整数 B/s 最终版本重跑')):
        failed = audit / f'{case_name}-{mode}-{suffix}'
        if not failed.is_dir():
            continue
        report = read(failed / 'run/report.json')
        data['narrative']['7'] += (f"\n\n{POLICIES[mode][0]}有一轮未计入报告。{reason}。客户端成功 {report['n_ok']}/{report['n_total']} 条。"
            f"该轮日志保留在本机 `{failed.name}`，报告使用最终版本的完整验收轮。")
        data['auxiliary_tables'].setdefault('excluded_attempts', []).append(dict(mode=mode, run=failed.name,
            n_total=report['n_total'], n_ok=report['n_ok'], reason=reason, **log_period(failed/'run'),
            run_log_sha256=digest(failed/'run.log'), client_report_sha256=digest(failed/'run/report.json')))
    if data['auxiliary_tables'].get('excluded_attempts'):
        data['narrative']['8'] += '\n\n[排除的尝试及原始日志指纹](report_data/excluded_attempts.csv)。'
    write(stage / 'dataset.json', data)
    write(stage / 'summary.json', summaries)
    subprocess.run([sys.executable, str(SKILL / 'generate_report.py'), '--dataset', str(stage / 'dataset.json'),
                    '--output', str(stage / 'report'), '--bin-ms', '50'], check=True)


def archive(case_name, stage, tag):
    """Archive reviewed output and add a self-contained offline HTML entry."""
    from markdown_it import MarkdownIt
    source = stage / 'report'
    receipt = read(source / 'report_data/validation.json')
    assert receipt['status'] == 'passed' and receipt['visual_reviewed'] and receipt['figure_count'] == 7
    benchmark = BENCHMARKS[case_name]
    dest = REPO / 'report' / benchmark / tag
    dest.mkdir(parents=True, exist_ok=False)
    mapping, source_files = {}, {}
    for path in sorted(source.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        rel = path.relative_to(source).as_posix()
        raw = path.read_bytes()
        source_files[rel] = info(path)
        target = 'report_data/source_validation.json' if rel == 'report_data/validation.json' else rel
        if len(raw) > 512 * 1024 and path.suffix in ('.csv', '.json'):
            raw = gzip.compress(raw, compresslevel=6, mtime=0)
            target += '.gz'
        mapping[rel] = target
        out = dest / target
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
    report = dest / 'report.md'
    md = report.read_text(encoding='utf-8')
    md = re.sub(r'\]\(([^)]+)\)', lambda m: '](' + mapping.get(m.group(1), m.group(1)) + ')', md)
    md = md.replace('在报告目录执行：', '从仓库根目录执行（自动解压冻结数据）：')
    md = md.replace('python reproduce/generate_report.py --dataset report_data/dataset.json --output ../reproduced-report',
                    f'python scripts/manage_reports.py redraw "{benchmark}/{tag}" --output <新的输出目录>')
    report.write_text(md, encoding='utf-8')
    body = MarkdownIt('commonmark', {'html': True}).enable('table').render(md)
    def embed(match):
        path = dest / match.group(1)
        return 'src="data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode() + '"'
    body = re.sub(r'src="(figure/[^\"]+\.png)"', embed, body)
    assert body.count('src="data:image/png;base64,') == 7
    html = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{case_name}｜四实例带宽账本实验</title>'
            '<style>body{max-width:1240px;margin:32px auto;padding:0 20px;font:16px/1.65 system-ui,sans-serif;color:#202124}'
            'img{max-width:100%;height:auto}table{border-collapse:collapse;display:block;overflow-x:auto}td,th{border:1px solid #ddd;padding:6px 10px}'
            'th{background:#f5f5f5}code,a{overflow-wrap:anywhere}h2{margin-top:2em}</style><body>' + body + '</body></html>')
    (dest / 'report.html').write_text(html, encoding='utf-8')
    (dest / '.gitattributes').write_text('* -text -whitespace\n', encoding='utf-8')
    (dest / 'reproduce/README.md').write_text(f'# 复现\n\n从仓库根目录运行：\n\n```bash\npython scripts/manage_reports.py redraw "{benchmark}/{tag}" --output <新目录>\n```\n\n完整重跑使用 `bash scripts/run_4w25.sh {case_name} light|tfs|bandwidth NEW_OUTPUT_DIR`，每个策略分别启动。原始逐层事件保留本机；标准派生数据支持离线重绘。\n', encoding='utf-8')
    figures = []
    for path in dest.glob('figure/*.png'):
        width, height = struct.unpack('>II', path.read_bytes()[16:24])
        figures.append(dict(path=path.relative_to(dest).as_posix(), source_sha256=digest(path), width=width, height=height))
    created = datetime.strptime(tag.split('_', 1)[0], '%Y%m%d-%H%M%S').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    dates = read(stage / 'dataset.json')['metadata']['实验日期'].split('、')
    manifest = dict(report_id=dest.relative_to(REPO / 'report').as_posix(), benchmark_id=benchmark,
                    study_id='bandwidth-ledger-20261125', created_at=created.isoformat(), experiment_dates=dates,
                    schemes=[dict(id=k, label=v[0]) for k, v in POLICIES.items()], compatibility='matched_input_and_model',
                    benchmark_input_sha256=read(REPO / 'benchmark' / benchmark / 'manifest.json')['original_trace_sha256'],
                    simulations_run=0, performance_recomputed=False, source_execution=dict(formal_runs=3, repeats_per_policy=1),
                    source_files=source_files, figures=figures, source_visual_reviewed=True,
                    standard_dataset=mapping['report_data/dataset.json'], report_bin_ms=receipt['bin_ms'])
    manifest['files'] = {p.relative_to(dest).as_posix(): info(p) for p in dest.rglob('*') if p.is_file()}
    write(dest / 'manifest.json', manifest)
    readme = dest.parent / 'README.md'
    with readme.open('a', encoding='utf-8') as stream:
        stream.write(f'\n[带宽账本对照（离线 HTML）]({tag}/report.html) · [Markdown]({tag}/report.md)\n')
    print(dest)


def overview(stage_root, tag):
    from plotting.report import table
    folders = [REPO / 'report' / name / tag for name in BENCHMARKS.values()]
    rows = []
    dates = set()
    exclusions = []
    for case_name, folder in zip(BENCHMARKS, folders):
        data = read(stage_root / case_name / 'summary.json')
        dates.update(read(folder/'manifest.json')['experiment_dates'])
        dataset = read(stage_root/case_name/'dataset.json')
        for attempt in dataset.get('auxiliary_tables', {}).get('excluded_attempts', []):
            exclusions.append([case_name, POLICIES[attempt['mode']][0], f"{attempt['n_ok']}/{attempt['n_total']}", attempt['reason']])
        for s in data:
            rows.append([case_name, POLICIES[s['mode']][0], *[f"{s[key]:.3f}" for key in (
                'client_finish_s', 'mean_worker_finish_s', 'mean_ttft_s', 'p95_ttft_s', 'slo_pct',
                'first_wait_s', 'later_wait_s', 'total_wait_s')]])
    text = ('# 四实例 25 GB/s 带宽账本研究总览\n\n' + '、'.join(sorted(dates)) + '完成五个固定负载各三种策略。每种策略保留一次成功运行。'
            '客户端指标包含实际入口开销，SLO 采用逐请求冻结画像期限。KV 等待按四实例累加。详见各报告的计时边界。\n\n')
    text += table(['负载', '策略', '完成 s', '四实例完成均值 s', '平均 TTFT s', 'P95 s', 'SLO %', '首层等待 s', '后15层等待 s', '总等待 s'], rows)
    text += '\n## 相对普通 TFS\n\n'
    changes = []
    for case_name in BENCHMARKS:
        _, old, new = read(stage_root / case_name / 'summary.json')
        changes.append([case_name, *[f"{(new[key]/old[key]-1)*100:+.1f}%" if old[key] else '不适用' for key in (
            'client_finish_s', 'mean_worker_finish_s', 'mean_ttft_s', 'p95_ttft_s', 'total_wait_s')]])
    text += table(['负载', '完成', '四实例完成均值', '平均 TTFT', 'P95', '累计 KV 等待'], changes)
    text += '\n## 完整报告\n\n'
    for case_name, folder in zip(BENCHMARKS, folders):
        prefix = '../../' + folder.relative_to(REPO / 'report').as_posix()
        text += f'- {case_name}： [Markdown]({prefix}/report.md) · [离线 HTML]({prefix}/report.html)\n'
    text += '\n每个工况每种策略只有一次有效独立运行，CPU 路由、调度和 RPC 开销会波动。各负载分别比较，不把不同请求数量或读量加总为单一性能指标。\n'
    if exclusions:
        text += '\n## 保留的失败与旧版本记录\n\n'
        text += table(['负载', '策略', '客户端成功', '排除原因'], exclusions)
        text += '\n本地保留原始日志。表中运行未计入本次性能结果。\n'
    dest = folders[0] / 'overview.md'
    dest.write_text(text, encoding='utf-8')
    manifest = read(folders[0] / 'manifest.json')
    manifest['overview'] = 'overview.md'
    manifest['files']['overview.md'] = info(dest)
    write(folders[0] / 'manifest.json', manifest)
    print(dest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    builder = sub.add_parser('build')
    builder.add_argument('audit', type=Path)
    builder.add_argument('case', choices=BENCHMARKS)
    builder.add_argument('stage', type=Path)
    publisher = sub.add_parser('archive')
    publisher.add_argument('case', choices=BENCHMARKS)
    publisher.add_argument('stage', type=Path)
    publisher.add_argument('tag')
    combined = sub.add_parser('overview')
    combined.add_argument('stage_root', type=Path)
    combined.add_argument('tag')
    args = parser.parse_args()
    if args.action == 'build':
        build(args.audit.resolve(), args.case, args.stage.resolve())
    elif args.action == 'archive':
        archive(args.case, args.stage.resolve(), args.tag)
    else:
        overview(args.stage_root.resolve(), args.tag)
