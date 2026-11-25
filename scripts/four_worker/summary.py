"""Check a completed four-worker run and print its client-side metrics."""
import csv
import json
import math
import statistics
import sys
from pathlib import Path


def summarize(run, benchmark, policy):
    def read(name):
        return json.loads((run / name).read_bytes())

    manifest = json.loads((benchmark / 'manifest.json').read_bytes())
    client = read('report.json')
    requests = client['requests']
    mapping = read('request_ids.json')
    gate = read('read_admission.check.json')
    check = read('check.json')
    hints = read('admission.final.json')['storage_hints']
    with (benchmark / 'requests.csv').open(encoding='utf-8-sig') as stream:
        rows = list(csv.DictReader(stream))
    assert check['status'] == gate['status'] == read('layer_check.json')['status'] == 'pass'
    assert len(requests) == client['n_ok'] == manifest['requests']
    assert set(requests) == {mapping[row['id']] for row in rows}
    assert check['actual_submitted_read_bytes'] == manifest['read_bytes']
    assert all(hints[mapping[row['id']]]['estimated_remote_read_bytes'] == int(row['read_bytes']) for row in rows)
    marked = {row['id'] for row in rows if hints[mapping[row['id']]]['is_heavy_read']}
    if policy == 'light':
        assert not marked and gate['grants'] == 0
    if policy == 'a_cap1':
        assert marked == {row['id'] for row in rows if row['class_id'] == 'A'}
        assert check['held_heavy_peak'] == 1
    first = min(r['sent_perf_s'] for r in requests.values())
    finishes = []
    seen = set()
    for worker in range(4):
        with (run / f'worker{worker}.steps.jsonl').open() as stream:
            ids = {rid.replace('-', '')[:32] for event in map(json.loads, stream)
                   if event.get('event') == 'layer_compute_start' for rid in event['req_ids']}
        assert ids and ids.isdisjoint(seen)
        seen.update(ids)
        finishes.append(max(requests[rid]['sent_perf_s'] - first + requests[rid]['e2e_s'] for rid in ids))
    assert seen == set(requests)
    ttfts = sorted(r['ttft_s'] for r in requests.values())
    return dict(benchmark=manifest['case'], policy=policy, ok=len(requests), heavy=len(marked),
                peak_held=check['held_heavy_peak'], makespan_s=max(finishes),
                worker_finish_s=finishes, mean_worker_finish_s=statistics.mean(finishes),
                mean_ttft_s=statistics.mean(ttfts), p95_ttft_s=ttfts[math.ceil(.95 * len(ttfts)) - 1])


if __name__ == '__main__':
    print(json.dumps(summarize(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]), indent=2))
