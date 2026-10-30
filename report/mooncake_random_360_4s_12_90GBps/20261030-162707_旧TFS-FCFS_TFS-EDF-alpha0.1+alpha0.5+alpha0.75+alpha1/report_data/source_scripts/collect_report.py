"""Export existing completed runs. This script does not start or alter a run."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
W = next(p for p in HERE.parents if (p/'workspace.json').is_file())
DEFAULT = Path('/home/zed/.local/share/state-aware-network/audits/mooncake90-alpha-20261029-r1')
OUT = W/'note/tfs_dv/Mooncake360-90GB计分阈值对照-20261029'
MODES = ('old-tfs', 'a01', 'a05', 'a075', 'a1')
read = lambda p: json.loads(p.read_text())
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT)
    parser.add_argument('--output', type=Path, default=OUT)
    args = parser.parse_args()
    a = args.source
    args.output.mkdir(parents=True,exist_ok=True)
    d = args.output/'report_data'
    receipt=read(a/'matrix_receipt.json')
    assert receipt['status']=='passed' and receipt['formal_requests']==1800
    physical={r['case']:r['physical_case'] for r in receipt['cases']}
    d.mkdir(exist_ok=True)
    write = lambda p, v: p.write_text(json.dumps(v, ensure_ascii=False, indent=2)+'\n')
    inp = read(a/'input_check.json')
    trace = a/'package/results/input360/trace.jsonl'
    assert sha(trace) == inp['effective_trace_sha256']
    rows = [json.loads(s) for s in trace.read_text().splitlines()]
    metadata = []
    for row in rows:
        row = dict(row)
        tokens = row.pop('prompt_token_ids')
        row['prompt_tokens'] = len(tokens)
        row['prompt_token_ids_sha256'] = hashlib.sha256(json.dumps(tokens).encode()).hexdigest()
        metadata.append(row)
    write(d/'input_metadata.json', metadata)
    write(d/'input_check.json', inp)
    shutil.copy2(a/'plan.json', d/'plan.json')
    shutil.copy2(a/'matrix_receipt.json', d/'matrix_receipt.json')
    if (a/'attempts.json').is_file():
        shutil.copy2(a/'attempts.json',d/'attempts.json')
    source_hashes = {}
    for name, expected in inp['runtime_sources'].items():
        assert sha(a/'package'/name) == expected, name
        source_hashes[name] = expected
    implementations = []
    audits = {}
    for mode in MODES:
        case = a/'package/results'/physical[mode]
        run = case/'run'
        out = d/'raw'/mode
        out.mkdir(parents=True, exist_ok=True)
        # The runner writes pretty-spaced JSONL. Compare every parsed field;
        # retain the byte hash of each actual replay file separately.
        assert [json.loads(s) for s in (case/'measured_trace.jsonl').read_text().splitlines()] == rows
        implementation = read(run/'implementation.json')
        implementations.append(implementation)
        driver = read(case/'driver.json')
        assert driver['status'] == 'passed' and not driver['alive'] and not driver['guards']
        for name in ('admission.final.json', 'config.json', 'check.json', 'warmup.check.json',
                     'warmup.history.check.json', 'gateway.gc.control.json', 'formula_check.json',
                     'layer_check.json', 'routing.build.json', 'routing.check.json',
                     'storage.gc.formal.begin.json', 'storage.gc.end.json', 'implementation.json',
                     'edf_priority.check.json', 'read_admission.check.json', 'routing.score.check.json', 'admission.rpc.json'):
            if (run/name).is_file():
                shutil.copy2(run/name, out/name)
        online=read(run/'admission.runtime.final.json')
        assert online['execution_feedback']=='disabled'
        assert all(not r['load_attempts'] and not r['submitted_read_bytes'] for r in online['records'].values())
        assert read(run/'admission.final.json')['load_observation_source']=='offline_worker_logs'
        write(out/'online_feedback_check.json',dict(status='pass',execution_feedback_rpcs=0,
            online_source_sha256=sha(run/'admission.runtime.final.json'),online_execution_fields_empty=True))
        from collections import Counter
        controls=[]
        for p in run.glob('worker*.read_admission.jsonl'):
            controls.extend(json.loads(line) for line in p.open() if 'read_acquire_response' in line)
        responses=[r for r in controls if r['event']=='read_acquire_response']
        # This control summary includes warmup; formal central counts are calculated separately.
        write(out/'admission_controls.json',dict(scope='warmup and formal',calls=len(responses),
            denied=sum(not r['granted'] for r in responses),
            mean_wall_ms=sum(r['duration_ns']/1e6 for r in responses)/len(responses) if responses else 0.0))
        shutil.copy2(run/'measured/report.json', out/'client_report.json')
        shutil.copy2(case/'driver.json', out/'driver.json')
        storage = read(run/'storage.final.json')
        write(out/'storage.projection.json', {k: storage[k] for k in
              ('hit_bindings', 'layer_loads', 'layer_read_history', 't0_monotonic_s')})
        for f in run.glob('worker*.steps.jsonl'):
            shutil.copy2(f, out/f.name)
        origins = []
        for ready in (run/'fine_trace').glob('ready-*.json'):
            info = read(ready)
            if info['role'] == 'gateway':
                log = run/'fine_trace'/('trace-'+str(info['pid'])+'.jsonl')
                origins += [json.loads(line)['origin_s'] for line in log.open()
                            if json.loads(line)['event'] == 'gateway_origin']
        assert len(origins) == 1
        write(out/'clock.json', {'gateway_origin_monotonic_s': origins[0], 'scale': 1,
                                'source': 'fine_trace gateway_origin event; no clock estimation'})
        audits[mode] = {str(f.relative_to(case)): {'sha256': sha(f), 'bytes': f.stat().st_size}
                       for f in [run/'admission.final.json', run/'storage.final.json',
                                 run/'measured/report.json', case/'measured_trace.jsonl', *run.glob('worker*.steps.jsonl')]}
        print(mode, 'exported', flush=True)
    assert all(x == implementations[0] for x in implementations)
    assert all(sha(a/'package'/name) == value for name, value in source_hashes.items())
    import statistics, math
    rpc = {}
    for mode in MODES:
        run=a/'package/results'/physical[mode]/'run'
        state=read(run/'admission.final.json');bindings=read(run/'storage.final.json')['hit_bindings']
        formal={rid for rid in state['records'] if rid.startswith('conversation:')}
        calls=[];by_worker={str(w):dict(calls=0,elapsed_s=0.) for w in range(12)}
        for path in run.glob('worker*.read_admission.jsonl'):
            worker=str(int(path.name.split('.')[0][6:]))
            for line in path.open():
                event=json.loads(line)
                if event['event']!='read_acquire_response':continue
                rid=bindings[event['request_id'].replace('-','')[:32]]
                if rid not in formal:continue
                calls.append(event);by_worker[worker]['calls']+=1;by_worker[worker]['elapsed_s']+=event['duration_ns']/1e9
        ms=sorted(e['duration_ns']/1e6 for e in calls)
        rpc[mode]=dict(calls=len(calls),denied=sum(not e['granted'] for e in calls),
            mean_ms=statistics.mean(ms) if ms else 0.,p95_ms=ms[math.ceil(.95*len(ms))-1] if ms else 0.,
            total_instance_s=sum(ms)/1000,by_worker=by_worker)
    write(d/'formal_rpc.json',rpc)
    cfg=read(d/'raw/a01/config.json')
    for mode,alpha in [('a01',.1),('a05',.5),('a075',.75),('a1',1.)]:
        candidate=read(d/'raw'/mode/'config.json');expected=json.loads(json.dumps(cfg))
        expected['routing_accounting']['waiting_factor']=alpha
        assert candidate==expected,mode
        assert candidate['admission']['idle_bypass'] is False
        assert read(d/'raw'/mode/'routing.score.check.json')['factor']==alpha
    write(d/'provenance.json',dict(source_commit=inp['source_commit'],native_sha256=inp['native_sha256'],
        same_runtime_fingerprints=True,same_input=True,trace_sha256=inp['effective_trace_sha256'],
        runtime_sources=source_hashes,source_audits=audits,source_directory=str(a),plan=read(a/'plan.json'),
        execution_feedback='disabled',reused=['old-tfs','a01'],new=['a05','a075','a1'],
        only_alpha_differs_between_new_tfs=True))
    reproduce=args.output/'reproduce';reproduce.mkdir(exist_ok=True)
    for name in ('runner.py','control_runner.py','base_runner.py','controlled_gc_gateway_20261027.py','tfs_warmup_gateway_20261027.py','runtime-change.patch','plan.json'):
        shutil.copy2(a/name,reproduce/name)
    print('All five cases exported; three new, two reused.',flush=True)


if __name__=='__main__':main()
