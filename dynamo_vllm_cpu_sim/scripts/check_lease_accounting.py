"""tfs-lease 臂的租约账本独立审计：放行上界、释放时机、事件完整性与消息计数。

输入：run 目录（config.json / admission.final.json / admission.runtime.final.json）。
只做离线核对，不重跑、不推断账房未记录的事实。
"""
import argparse
from collections import Counter
import json
from pathlib import Path


def check(folder, status=None, config=None):
    folder = Path(folder)
    config = config or json.loads((folder/'config.json').read_text())
    if status is None:
        status = json.loads((folder/'admission.final.json').read_text())
    assert config['admission']['classification'] == 'bandwidth_line'
    assert config['admission']['release_on'] == 'lease_events'
    lease_cfg = config['admission']['lease']
    disk_Bps = config['storage']['disk_Bps']
    exception_Bps = disk_Bps * lease_cfg['line_factor_exception']
    line_Bps = disk_Bps * lease_cfg['line_factor_normal']
    assert status.get('execution_feedback') == 'lease_events'
    assert status.get('load_observation_source') == 'offline_worker_logs'
    lease = status['lease']
    permits = status['permits']
    events = status['events']

    # 预热期租约不受线限制；正式审计从 warmup_finished 之后回放。
    boundaries = [e['t'] for e in events if e['event'] == 'warmup_finished']
    formal_since = max(boundaries) if boundaries else float('-inf')
    permits = {token: p for token, p in permits.items() if p['granted_s'] >= formal_since}
    events = [e for e in events if e['t'] >= formal_since]

    # 1) 终态：无开放租约，许可状态收敛。
    open_entries = len(lease['open'])
    assert open_entries == 0, lease['open']
    states = Counter(p['state'] for p in permits.values())
    assert set(states) <= {'released', 'rolled_back'}, states
    reasons = Counter(p.get('release_reason') for p in permits.values() if p['state'] == 'released')
    assert 'first_token' not in reasons, reasons
    for token, p in permits.items():
        if p['state'] == 'released':
            assert p.get('release_reason') in ('lease_completed', 'request_finished',
                                               'lease_expired', 'no_external_load'), (token, p)
    first_token_releases = sum(1 for r in status['records'].values()
                               if r.get('lease_release_reason') == 'first_token')

    # 2) 事件完整性：每条进入 submitted 的许可都有读开始；lease_completed 有对应减账。
    started = {e['id'] for e in events if e['event'] == 'lease_started'}
    completed = {e['id'] for e in events if e['event'] == 'lease_completed'}
    for token, p in permits.items():
        if p['state'] in ('rolled_back',):
            continue
        assert p['id'] in started, f"permit {token} submitted without lease_started"
        if p.get('release_reason') == 'lease_completed':
            assert p['id'] in completed, f"permit {token} released by completion without event"

    # 3) 放行上界回放：非护栏路径的账面峰值不得越例外带；护栏只允许单条在场。
    timeline = []
    guardrail = set()
    for e in events:
        if e['event'] == 'admission_reserved' and e.get('reason') == 'lease':
            timeline.append((e['t'], 1, e['id'], e.get('lease_path'), e.get('lease_sum_after')))
            if e.get('lease_path') == 'guardrail_empty':
                guardrail.add(e['id'])
        elif e['event'] == 'admission_released':
            # 基类（request_finished 兜底）不带 permit_id，按 rid 对账。
            timeline.append((e['t'], -1, e['id'], None, None))
    timeline.sort(key=lambda row: (row[0], row[1]))
    running, peak, peak_open, violations = 0.0, 0.0, 0, []
    current = {}
    for t, sign, rid, path, sum_after in timeline:
        if sign == 1:
            share = next(p['share_Bps'] for p in permits.values() if p['id'] == rid)
            current[rid] = share
            running += share
        else:
            running -= current.pop(rid, 0.0)
        peak = max(peak, running)
        peak_open = max(peak_open, len(current))
        if running > exception_Bps * (1 + 1e-6) and not (len(current) == 1
                                                         and next(iter(current)) in guardrail):
            violations.append((t, running, len(current)))
    assert not violations, violations[:5]
    for e in events:
        if (e['event'] == 'admission_reserved' and e.get('reason') == 'lease'
                and e.get('lease_path') != 'guardrail_empty'):
            assert e.get('lease_sum_after') is None or e['lease_sum_after'] <= exception_Bps * (1 + 1e-6)

    # 4) 消息计数：重读 acquire RTT(2) + lease_started(1) + lease_completed(1) = 4。
    grants = sum(1 for e in events if e['event'] == 'admission_reserved' and e.get('reason') == 'lease')
    started_n = len(started)
    completed_n = len(completed)
    rolled = states.get('rolled_back', 0)

    # 5) 获准到读开始的虚占窗口（保守侧观测）。
    gaps = [e['grant_to_start_s'] for e in events
            if e['event'] == 'lease_started' and e.get('grant_to_start_s') is not None]

    return dict(status='passed', permits=len(permits), open_entries=open_entries,
                permit_states=dict(states), release_reasons=dict(reasons),
                heavy_peak_Bps=peak, heavy_peak_open_entries=peak_open,
                exception_Bps=exception_Bps, line_Bps=line_Bps,
                grants=grants, lease_started=started_n, lease_completed=completed_n,
                rolled_back=rolled, first_token_releases=first_token_releases,
                messages_per_heavy=(2 * grants + started_n + completed_n) / max(1, grants),
                grant_to_start_max_s=max(gaps, default=0.0),
                grant_to_start_mean_s=(sum(gaps) / len(gaps)) if gaps else 0.0,
                counters=lease['counters'], delta_s=lease['delta_s'],
                light_rate_final_Bps=lease['light_rate_Bps'],
                reconcile_events=lease['counters'].get('reconciles', 0),
                ttl_expired=lease['counters'].get('ttl_expired', 0))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('folder', type=Path)
    a = p.parse_args()
    print(json.dumps(check(a.folder), indent=2, ensure_ascii=False))
