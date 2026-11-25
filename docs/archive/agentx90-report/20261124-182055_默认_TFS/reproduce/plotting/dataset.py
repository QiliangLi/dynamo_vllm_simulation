"""Versioned report data adapter. No experiment/runtime imports."""
import math
from collections import defaultdict
from pathlib import Path

from .timeline import (discover, load_case, read_json, digest, require,
                       length, subtract)


def native_dataset(report=None, manifest=None):
    spec = discover(report, manifest)
    extra = read_json(manifest) if manifest else {}
    result = dict(schema_version=1, title=spec['title'],
                  metadata=extra.get('metadata', {}), cases=[])
    for item in spec['cases']:
        case = load_case(item)
        root = Path(item['path'])
        cfg = read_json(root/'config.json')
        status = read_json(root/'admission.final.json')
        client_path = next((p for p in (root/'client_report.json', root/'measured/report.json') if p.is_file()), None)
        client = read_json(client_path)['requests'] if client_path else {}
        records = status['records']
        formal = {r['id'] for r in case['requests']}
        first = min(records[r]['profile']['arrival_s'] for r in formal)
        requests = []
        for row in case['requests']:
            rid = row['id']; record = records[rid]; profile = record['profile']; c = client.get(rid, {})
            require('deadline_s' in profile, f'{rid} 缺少逐请求期限，不能统计 SLO。')
            request = dict(id=rid, worker=row['worker'], cohort=row['historical_class'] or '未分类',
                arrival_s=profile['arrival_s']-first, completion_s=record['first_token_s']-first,
                deadline_s=profile['deadline_s']-first, compute_classes=row['compute_classes'])
            for field, value in (
                ('gateway_wait_ms', None if record.get('submitted_s') is None else (record['submitted_s']-profile['arrival_s'])*1000),
                ('arrival_lateness_ms', None if not {'actual_arrival_s','planned_arrival_s'} <= c.keys() else (c['actual_arrival_s']-c['planned_arrival_s'])*1000),
                ('read_bytes', record.get('submitted_read_bytes'))):
                if value is not None: request[field] = value
            requests.append(request)
        storage_path = root/('storage.projection.json' if (root/'storage.projection.json').is_file() else 'storage.final.json')
        storage = read_json(storage_path)
        clock=read_json(root/'clock.json') if (root/'clock.json').is_file() else {}
        origin=clock.get('gateway_origin_monotonic_s',status.get('origin_monotonic_s'))
        bandwidth, reason = reconstruct_pool(storage, cfg, formal, first, origin)
        case.update(requests=requests, bandwidth=bandwidth, bandwidth_unavailable=reason,
                    config=cfg, run_metadata=item.get('run_metadata', {}), color=item.get('color'))
        result['cases'].append(case)
    return result


def reconstruct_pool(storage, cfg, formal, first, origin):
    """Infer rate only for the frozen single-pool model whose occupancy proves it."""
    sc = cfg['storage']
    if not (sc.get('disks') == 1 and sc.get('bandwidth_policy') == 'max_min_fair'
            and sc.get('read_latency_s') == 0):
        return None, '当前日志适配器仅重建单盘、零启动延迟的 max_min_fair 共享池；其他拓扑需导出真实速率区间。'
    cap = min(sc['disk_Bps'], sc.get('shared_link_Bps') or sc['disk_Bps'])
    scale = cfg.get('wall_time_scale', 1)
    shift = (storage['t0_monotonic_s']-origin)*scale-first
    bindings = storage['hit_bindings']
    def sid(native):
        return native if native in formal else bindings.get(native.replace('-', '').lower()[:32])
    changes = defaultdict(list)
    demands = {rid: load['demand']*sum(load['counts'].values()) for rid, load in storage.get('layer_loads', {}).items()}
    total = 0
    for i, read in enumerate(storage['layer_read_history']):
        if sid(read['rid']) not in formal: continue
        if read['rid'] not in demands:
            return None, '缺少逐层申请带宽，不能重建需求曲线。'
        changes[read['submitted_s']].append((i, read['rid']))
        changes[read['completed_s']].append((i, None))
        total += read['bytes']
    active = {}; previous = None; intervals = []
    for time, events in sorted(changes.items()):
        if previous is not None and time > previous:
            demand = math.fsum(demands[rid] for rid in active.values())
            # DV weighted_waterfill distributes surplus even when summed requested
            # rates fit. A request's demand is not a hard throughput ceiling.
            # The single zero-latency disk therefore stays work-conserving.
            intervals.append(dict(start_s=previous+shift, end_s=time+shift,
                                  raw_start_s=previous, raw_end_s=time,
                                  actual_Bps=cap if active else 0., demand_Bps=demand))
        for index, rid in events:
            if rid is None: del active[index]
            else: active[index] = rid
        previous = time
    require(not active, '带宽日志存在未完成读取。')
    return dict(capacity_Bps=cap, read_bytes=total, intervals=intervals, timestamp_shift_s=shift,
                source='DV grouped max_min_fair单池活动区间推导；空余带宽继续分配，申请率不是硬上限；实际速率积分另与读取字节核对'), None


def validate_dataset(data):
    require(data.get('schema_version') == 1, 'dataset.schema_version 必须为1。')
    require(data.get('title') and data.get('cases'), '缺少标题或方案。')
    identifiers = [c['summary']['case'] for c in data['cases']]
    require(len(set(identifiers)) == len(identifiers), '方案ID重复。')
    def finite(*values):
        require(all(type(x) in (int,float) and math.isfinite(x) for x in values), '数值必须有限。')
    for case in data['cases']:
        summary = case['summary']; rows = case['requests']; span = summary['span_s']; workers = summary['workers']
        finite(span); require(span > 0 and type(workers) is int and workers > 0, '窗口和实例数必须为正。')
        require(summary.get('label') and rows and len({r['id'] for r in rows}) == len(rows), '方案名称为空或请求ID重复。')
        for r in rows:
            finite(r['arrival_s'], r['completion_s'], r['deadline_s'])
            require(0 <= r['arrival_s'] <= r['completion_s'] <= span+1e-6 and r['deadline_s'] >= r['arrival_s'], '请求时间超出窗口或期限早于到达。')
            require(type(r['worker']) is int and 0 <= r['worker'] < workers and isinstance(r['cohort'],str), '请求实例或分类无效。')
            for name in ('gateway_wait_ms','arrival_lateness_ms','read_bytes'):
                if name in r: finite(r[name])
            if 'read_bytes' in r: require(r['read_bytes'] >= 0, '读取字节不能为负。')
        require(abs(min(r['arrival_s'] for r in rows)) < 1e-6 and abs(max(r['completion_s'] for r in rows)-span) < 1e-6, '窗口必须从首条正式请求到达到末条首token。')
        require((case.get('comp') is None) == (case.get('io') is None), '时间线需要同时提供计算和IO区间；无IO用空列表。')
        accounts = []
        if case.get('comp') is not None:
            for row in case['comp']+case['io']:
                finite(row['start'], row['end'])
                require(type(row['worker']) is int and 0 <= row['worker'] < workers and -1e-6 <= row['start'] <= row['end'] <= span+1e-6, '执行区间无效。')
            require(all(r['state'] in ('heavy','light','mixed') for r in case['comp']), '计算状态只能是heavy/light/mixed，禁止用历史A/B替代。')
            require(all(r['state'] == 'io' for r in case['io']), 'IO区间状态无效。')
            require(summary.get('rule') in ('arrival','dynamic') and type(summary.get('classification_reference_only')) is bool, '时间线缺少分类规则及参考标记。')
            for worker in range(workers):
                cc = [(r['start'],r['end']) for r in case['comp'] if r['worker']==worker]
                model = length(cc)
                require(abs(model-math.fsum(b-a for a,b in cc)) < 1e-7, '实例计算区间重叠。')
                red = length(subtract([(r['start'],r['end']) for r in case['io'] if r['worker']==worker],cc))
                gray = span-model-red
                require(gray >= -1e-7, '时间账本为负。')
                accounts.append(dict(case=summary['case'],worker=worker,model_s=model,red_s=red,gray_s=max(0.,gray)))
        case['accounts'] = accounts
        summary['requests'] = len(rows)
        for key in ('model_s','red_s','gray_s'):
            summary[key] = math.fsum(a[key] for a in accounts) if accounts else None
        band = case.get('bandwidth')
        if band is not None:
            cap = band['capacity_Bps']; expected = band['read_bytes']; finite(cap,expected)
            require(cap > 0 and expected >= 0, '带宽容量或字节无效。')
            previous = -1e-6; roundoff_terms = []
            for e in band['intervals']:
                a,b,rate,demand = (e[k] for k in ('start_s','end_s','actual_Bps','demand_Bps'))
                finite(a,b,rate,demand)
                require(previous <= a < b <= span+1e-6 and rate >= 0 and demand >= 0 and rate <= cap+1e-6, '带宽区间重叠、越界或超过容量。')
                previous = b
                if 'raw_start_s' in e or 'raw_end_s' in e:
                    raw_a,raw_b,shift=e['raw_start_s'],e['raw_end_s'],band['timestamp_shift_s']
                    finite(raw_a,raw_b,shift)
                    require(raw_a < raw_b and a == raw_a+shift and b == raw_b+shift,
                            '带宽原始时间戳与平移后的端点不一致。')
                    # The event clock can round a completion upward and clamp
                    # the remaining read bytes. Integrating cap over those float
                    # endpoints therefore has a distinct, measurable ULP budget.
                    roundoff_terms.append(rate*sum(math.ulp(x) for x in (raw_a,raw_b,a,b)))
            integral = math.fsum((e['end_s']-e['start_s'])*e['actual_Bps'] for e in band['intervals'])
            roundoff_bound = math.fsum(roundoff_terms)
            tolerance = max(1e-6, expected*1e-11,roundoff_bound)
            require(abs(integral-expected) <= tolerance, f"{summary['case']} 带宽积分与读取字节不符。")
            if all('read_bytes' in r for r in rows):
                require(abs(sum(r['read_bytes'] for r in rows)-expected) <= tolerance, '请求字节和带宽字节不符。')
            band['integral_error_bytes'] = integral-expected
            band['integral_tolerance_bytes'] = tolerance
            band['timestamp_roundoff_bound_bytes'] = roundoff_bound
    return data
