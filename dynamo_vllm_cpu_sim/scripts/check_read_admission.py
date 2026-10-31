"""Audit scheduler permits against gateway facts and independent worker logs."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sim.stack.admission_service import UUID_RE


def check_permit_ledger(events, permits, lease_arm=False):
    """Allow same-worker overlap, but account for every grant and release.

    lease 臂的 held_* 字段是 B/s 份额和，不是条数；只核对发放/释放配对与终态清零。
    """
    active = set()
    max_forced_per_worker = 0
    for event in events:
        if event['event'] == 'admission_reserved':
            token = event['permit_id']
            assert token not in active
            if not lease_arm:
                assert event['held_before'] == len(active)
            active.add(token)
            if not lease_arm:
                assert event['held_after'] == len(active)
            worker_counts = {}
            for token in active:
                permit = permits[token]
                if permit.get('forced'):
                    worker_counts[permit['worker']] = worker_counts.get(permit['worker'], 0) + 1
            max_forced_per_worker = max(max_forced_per_worker, max(worker_counts.values(), default=0))
        elif event['event'] == 'admission_released':
            own = [token for token in active if permits[token]['id'] == event['id']]
            assert len(own) == 1, 'release must close exactly one active request permit'
            if not lease_arm:
                assert event['held_before'] == len(active)
            active.remove(own[0])
            if not lease_arm:
                assert event['held_after'] == len(active)
    assert not active, 'unreleased permits remain'
    return max_forced_per_worker


def check(folder, status=None):
    folder = Path(folder)
    status = status or json.loads((folder/'admission.final.json').read_text())
    config = json.loads((folder/'config.json').read_text())
    lease_arm = config['admission'].get('classification') == 'bandwidth_line'
    if status.get('execution_feedback') in ('disabled', 'lease_events'):
        from sim.stack.load_observation import with_local_observations
        status = with_local_observations(status, sorted(folder.glob('worker*.loads.jsonl')))
        assert not any(e['event']=='feedback_received' and e.get('engine_id') for e in status['events'])
    events, records, hints, permits = (status[k] for k in ('events','records','storage_hints','permits'))
    assert status['status'] == 'ready' and not status['invalid_reasons']
    rpc = status.get('rpc')
    if rpc:
        configured = json.loads((folder/'config.json').read_text())['admission']['rpc_transport']
        assert rpc['transport'] == configured
        assert json.loads((folder/'admission.rpc.json').read_text()) == rpc
    aliases = {uuid.UUID(e['engine_uuid']).hex:e['id'] for e in events if e['event']=='request_correlated'}
    received, responses, skipped, forced = {}, [], [], {}
    forced_rounds = set()
    for path in sorted(folder.glob('worker*.read_admission.jsonl')):
        full_rounds = set()
        for line in path.read_text().splitlines():
            row = json.loads(line)
            match = UUID_RE.search(row['request_id'])
            assert match, row
            rid = aliases[uuid.UUID(match.group()).hex]
            if row['event'] == 'storage_hint_received':
                assert rid not in received and row['hint'] == hints[rid]
                received[rid] = row
            elif row['event'] == 'read_acquire_response':
                assert hints[rid]['is_heavy_read'] and row['round'] not in full_rounds
                assert row['ok'] is True
                if rpc:
                    assert row['rpc_transport'] == rpc['transport']
                    if rpc['transport'] == 'dynamo':
                        assert row['rpc_request_plane'] == rpc['request_plane'] == 'tcp'
                        assert row['rpc_codec'] == rpc['codec'] == 'msgpack'
                        assert row['rpc_instance_id'] == rpc['instance_id']
                        assert row['rpc_ledger_epoch'] == rpc['ledger_epoch']
                        assert row['rpc_server_pid'] == rpc['server_pid'] != row['rpc_client_pid']
                        assert row['rpc_bridge'] == 'thread'
                if not row['granted']:
                    assert row['reason'] in ('full', 'worker_loading_full')
                    full_rounds.add(row['round'])
                else:
                    assert permits[row['permit_id']]['id'] == rid
                responses.append(row)
            elif row['event'] == 'read_skipped_full':
                assert hints[rid]['is_heavy_read']
                skipped.append(row)
            elif row['event'] == 'idle_bypass_committed':
                assert (path.name, row['round']) not in forced_rounds, 'multiple forces in one scheduler round'
                forced_rounds.add((path.name, row['round']))
                assert rid not in forced and hints[rid]['is_heavy_read']
                assert row['admission_waited'] is False
                assert row['allocation_completed_mono'] <= row['decision_mono'] <= row['monotonic_ns']/1e9
                p = permits[row['permit_id']]
                assert p.get('forced') and p['id'] == rid
                forced[rid] = row
    assert set(received) == set(records) == set(hints)
    for rid, rec in records.items():
        assert rec['state'] == 'FINISHED' and not rec['lease_open']
        forwarded = next(e for e in events if e['event']=='storage_hint_forwarded' and e['id']==rid)
        assert forwarded['hint'] == hints[rid]
        dispatched = next(e for e in events if e['event']=='request_submitted' and e['id']==rid)
        assert dispatched['reserved'] is False
        own = [p for p in permits.values() if p['id']==rid]
        if hints[rid]['is_heavy_read'] and rec['load_attempts']:
            completed = [p for p in own if p['state']=='released']
            assert len(completed) == 1
            p = completed[0]
            assert p['worker'] == rec['worker_id']
            assert forwarded['t'] <= p['granted_s'] <= rec['load_submit_s']
            if lease_arm:
                # lease 臂：末层读毕即释放，先于首 token。
                assert rec['load_done_s'] <= p['released_s'] <= rec['first_token_s']
                assert p['release_reason'] in ('lease_completed', 'request_finished',
                                               'lease_expired')
            else:
                assert rec['load_done_s'] <= rec['first_token_s'] <= p['released_s']
                assert p['release_reason'] == 'first_token'
        elif not hints[rid]['is_heavy_read']:
            assert not own
    for p in permits.values():
        assert p['state'] in ('released', 'rolled_back'), p
    grants = [e for e in events if e['event']=='admission_reserved']
    assert len(grants) == len(permits)
    for e in grants:
        assert e['placement']=='scheduler' and e['heavy']
        if not lease_arm:
            assert e['held_after']==e['held_before']+1
        if e['reason'] == 'idle_bypass':
            assert configured_idle(folder) and e['id'] in forced
            assert e['proof'] == forced[e['id']]['proof']
            assert e['proof']['running_before'] == e['proof']['scheduled_tokens_before'] == 0
            assert e['proof']['allocated'] and e['proof']['nonheavy_candidates'] == 0
            assert not any(r['request_id'] == forced[e['id']]['request_id']
                           and r['round'] == forced[e['id']]['round'] for r in responses)
        else:
            assert e['reason'] in ('normal', 'lease')
            assert e['cap'] is None or e['held_after']<=e['cap']
            if not lease_arm:
                assert e['held_after']==e['held_before']+1
    # Sweep every permit independently. Same-worker forced leases may overlap;
    # every outstanding lease must still count toward normal admission held.
    max_forced_per_worker = check_permit_ledger(events, permits, lease_arm=lease_arm)
    assert not status.get('pending_engine_events')
    durations = [r['duration_ns']/1e6 for r in responses]
    return dict(status='pass', requests=len(records), hints_received=len(received),
                rpc_transport=rpc['transport'] if rpc else 'legacy_unix',
                heavy_hints=sum(h['is_heavy_read'] for h in hints.values()),
                grants=len(grants), denied=sum(not r['granted'] for r in responses),
                skipped_heavy_requests=len(skipped), leaked_permits=0,
                rolled_back=sum(p['state']=='rolled_back' for p in permits.values()),
                forced_grants=len(forced), forced_over_cap=sum(e['reason']=='idle_bypass' and e.get('over_cap',False) for e in grants),
                max_unreleased_forced_per_worker=max_forced_per_worker,
                acquire_rpc_mean_wall_ms=statistics.mean(durations) if durations else 0,
                acquire_rpc_p50_wall_ms=statistics.median(durations) if durations else 0,
                acquire_rpc_p95_wall_ms=sorted(durations)[int(.95*(len(durations)-1))] if durations else 0,
                acquire_rpc_max_wall_ms=max(durations, default=0))


def configured_idle(folder):
    return json.loads((folder/'config.json').read_text())['admission'].get('idle_bypass',False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    print(json.dumps(check(parser.parse_args().folder), indent=2))
