"""Run only the new V1 arm, reusing the frozen seed416 harness and fine probes."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
import run_seed416_fine
import run_sa_abc_stack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    source = ROOT/'results/seed416_fine_source_20261026'
    digest = hashlib.sha256((source/'trace.jsonl').read_bytes()).hexdigest()
    assert digest == '35e488b618a8744b420eb7a1876d4a8efeb4c7bb0d3f8def55754f9a4b918750'
    original_one, original_run = run_sa_abc_stack.run_one, run_sa_abc_stack.run

    def late(config, *a, **kw):
        config = deepcopy(config)
        config['admission'].update(placement='scheduler', classification='arrival',
                                   aging_enabled=False, retry_interval_wall_s=.002)
        return original_one(config, *a, **kw)

    def run(options):
        if args.smoke:
            options.workers = 2
        return original_run(options)

    run_sa_abc_stack.run_one, run_sa_abc_stack.run = late, run
    try:
        run_seed416_fine.native(args.name, 'tfs', args.smoke, True)
    finally:
        run_sa_abc_stack.run_one, run_sa_abc_stack.run = original_one, original_run
    out = ROOT/'results'/args.name
    audit = json.loads((out/'tfs/read_admission.check.json').read_text())
    result = json.loads((out/'tfs.result.json').read_text())
    assert result['status'] == audit['status'] == 'pass'
    assert audit['requests'] == (6 if args.smoke else 360)
    assert result['metrics']['check']['workers'] == list(range(2 if args.smoke else 12))
    (out/'v1_case.json').write_text(json.dumps(dict(status='pass', input_sha256=digest,
        placement='scheduler', classification='arrival', smoke=args.smoke,
        runtime_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        audit=audit), indent=2)+'\n')
    print(json.dumps(dict(status='pass', audit=audit)), flush=True)


if __name__ == '__main__':
    main()
