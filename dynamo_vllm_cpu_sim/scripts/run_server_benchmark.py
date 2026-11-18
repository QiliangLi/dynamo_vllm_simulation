"""Screen server benchmark catalogs on the three-worker CPU process stack."""
import argparse
from collections import deque
import json
import math
from pathlib import Path
import random
import sys

from run_tfs_stack import ROOT, run_one, write


def make_rows(catalog, tokenizer, block_size, time_scale, limit=None):
    if not math.isfinite(time_scale) or time_scale <= 0:
        raise ValueError('Arrival scale must be positive')
    vocabulary = tokenizer.get_vocab()
    pool = []
    for word, token in sorted(vocabulary.items()):
        if word[:1] in ('Ġ', '▁') and word[1:].isascii() and word[1:].isalpha():
            text = tokenizer.decode([token])
            if text.startswith(' ') and tokenizer.encode(text) == [token]:
                pool.append(token)
    if not pool:
        raise ValueError('No stable leading-space tokens in simulation vocabulary')
    rng = random.Random(20261117)
    rows = []
    for i, request in enumerate(catalog['requests'][:limit]):
        h, u = request['hit_tokens'], request['miss_tokens']
        assert h % block_size == 0 and h + u < catalog['max_model_len']
        # Keep different requests' first blocks distinct, including cold requests.
        for _ in range(30):
            ids = rng.choices(pool, k=h + u)
            if tokenizer.encode(tokenizer.decode(ids)) == ids:
                break
        else:
            raise ValueError(f'Cannot synthesize an exact token roundtrip for {h=}, {u=}')
        rows.append(dict(id=f'q{i:05d}', arrival_s=request['arrival_offset_ms'] * time_scale / 1000,
                         class_id=request['request_class'], prompt_token_ids=ids,
                         remote_prefix_tokens=h, output_tokens=1))
    return rows


def periodic_abc(catalog):
    """Five 3-second cycles with 36 arrivals in the first second and 36 in the next two."""
    queues = {label: deque(r for r in catalog['requests'] if r['request_class'] == label)
              for label in 'ABC'}
    if any(len(q) != 120 for q in queues.values()) or len(catalog['requests']) != 360:
        raise ValueError('The periodic schedule needs exactly 120 requests per ABC class')
    requests = []
    for cycle in range(5):
        for start_ms, interval_ms in ((0, 1000 / 36), (1000, 2000 / 36)):
            for i in range(36):
                row = queues['ABC'[i % 3]].popleft()
                requests.append(dict(row, arrival_offset_ms=3000 * cycle + start_ms + i * interval_ms))
    return dict(catalog, requests=requests)


def summarize_run(folder, rows, arrival_pattern, arrival_scale):
    report = json.loads((folder/'report.json').read_text())
    identities = json.loads((folder/'request_ids.json').read_text())
    groups = {'all': list(report['requests'].values())}
    for row in rows:
        request = report['requests'][identities[row['id']]]
        groups.setdefault(row['class_id'], []).append(request)
        if arrival_pattern == 'periodic-abc':
            phase = 'burst' if row['arrival_s'] / arrival_scale % 3 < 1 - 1e-9 else 'recovery'
            groups.setdefault(phase, []).append(request)
    result = {}
    for group, requests in groups.items():
        ttft = sorted(r['ttft_s'] for r in requests if r.get('ok') and r.get('ttft_s') is not None)
        result[group] = dict(requests=len(requests), succeeded=sum(r['ok'] for r in requests),
            visible_ttft=len(ttft), mean_ttft_ms=1000 * sum(ttft) / len(ttft) if ttft else None,
            p95_ttft_ms=1000 * ttft[math.ceil(0.95 * len(ttft)) - 1] if ttft else None)
    storage = json.loads((folder/'storage.final.json').read_text())
    return dict(groups=result, elapsed_model_s=report['model_elapsed_s'],
        read_bytes=storage['bytes_submitted'], per_disk_peak_Bps=storage['disk_peak_Bps'],
        peak_pending_layer_reads=storage['peak_pending_requests'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--server-dynamo', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('default', 'tfs', 'both'), default='both')
    parser.add_argument('--arrival-scale', type=float, default=1.0)
    parser.add_argument('--arrival-pattern', choices=('original', 'periodic-abc'), default='original')
    parser.add_argument('--wall-time-scale', type=float, default=1.0)
    parser.add_argument('--num-blocks', type=int, help='Usable KV blocks per worker, when measured on the server')
    parser.add_argument('--limit', type=int, help='Limit the run to the first N requests for startup checks')
    parser.add_argument("--alpha", type=float, default=0.1)
    parser.add_argument("--dev", action="store_true", help="Unlimited TFS heavy-read permits")
    args = parser.parse_args()
    if not math.isfinite(args.alpha) or not 0 <= args.alpha <= 1:
        parser.error("--alpha must be finite and in [0,1]")
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    from transformers import AutoTokenizer
    cfg = json.loads((ROOT/'configs/minimax_server_3w.json').read_text())
    if args.num_blocks is not None:
        cfg['num_blocks'] = args.num_blocks
    cfg['wall_time_scale'] = args.wall_time_scale
    cfg['routing_accounting']['waiting_factor'] = args.alpha
    cfg['admission']['dev_mode'] = args.dev
    catalog = json.loads(args.catalog.read_text())
    if args.arrival_pattern == 'periodic-abc':
        catalog = periodic_abc(catalog)
    tokenizer = AutoTokenizer.from_pretrained(ROOT/'configs/model', local_files_only=True)
    tokenizer.model_max_length = cfg['max_model_len']
    rows = make_rows(catalog, tokenizer, cfg['block_size'], args.arrival_scale, args.limit)
    if len({tuple(row['prompt_token_ids'][:cfg['block_size']]) for row in rows}) != len(rows):
        raise ValueError('Generated requests share a first block')
    out = args.output.resolve()
    out.mkdir(parents=True)
    trace = out/'trace.jsonl'
    with trace.open('w') as stream:
        for row in rows:
            stream.write(json.dumps(row)+'\n')
    write(out/'input.json', dict(catalog=str(args.catalog.resolve()), requests=len(rows),
                                 arrival_pattern=args.arrival_pattern,
                                 arrival_scale=args.arrival_scale, wall_time_scale=args.wall_time_scale))
    modes = ('default', 'tfs') if args.mode == 'both' else (args.mode,)
    summaries = {}
    for mode in modes:
        run_one(cfg, trace, out/mode, Path(sys.executable),
                'immediate' if mode == 'default' else 'tfs', 'first_token',
                workers=cfg['workers'], server_dynamo=args.server_dynamo)
        summaries[mode] = summarize_run(out/mode, rows, args.arrival_pattern, args.arrival_scale)
        write(out/'comparison.json', summaries)


if __name__ == '__main__':
    main()
