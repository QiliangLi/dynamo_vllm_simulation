"""Check the declared shared runtime against its frozen hashes and another checkout."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def digest(path):
    # Git text checkouts may use CRLF on Windows. Normalize only line endings.
    return hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()


def verify(manifest, package=ROOT, source=None):
    files=manifest['files_sha256']
    if not files:raise ValueError('empty shared runtime manifest')
    for name, expected in files.items():
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('invalid runtime path: '+name)
        for root in (package, source) if source is not None else (package,):
            path=Path(root)/relative
            if not path.is_file() or digest(path)!=expected:
                raise ValueError('shared runtime differs: '+str(path))
    return dict(status='passed',files_checked=len(files),
                source_compared=source is not None,source_commit=manifest['research_commit'],
                comparison='SHA256 after CRLF-to-LF normalization')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,help='other repository root; no network or writes')
    args=parser.parse_args()
    manifest=json.loads((ROOT/'RUNTIME_SYNC.json').read_text(encoding='utf-8'))
    print(json.dumps(verify(manifest,ROOT,None if args.source is None else args.source/'dynamo_vllm_cpu_sim'),ensure_ascii=False,indent=2))
