"""Build an isolated, fingerprinted Dynamo scorer overlay; never replaces a wheel.

Requires the pinned Rust toolchain and normal Dynamo native build dependencies.
The output directory must be new. Only the frontend uses output/python on
PYTHONPATH; workers and the existing environment keep their installed wheel.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
PATCH=ROOT/'patched/weighted_routing'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True,type=Path,help='unmodified pinned Dynamo source')
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--jobs',type=int,default=4)
    p.add_argument('--release',action='store_true',help='optimized native build for controlled performance experiments')
    p.add_argument('--tier-admission',action='store_true',help='resolve one shared-storage profile before native scoring')
    p.add_argument('--target-cache',type=Path,help='reuse Cargo artifacts without replacing any installed wheel')
    args=p.parse_args();out=args.output.resolve();original=args.source.resolve()
    if out.is_relative_to(original):p.error('build output must lie outside the source tree')
    out.mkdir(parents=True,exist_ok=False)
    if args.jobs<1:p.error('--jobs must be positive')
    source=out/'dynamo'
    shutil.copytree(original,source,ignore=shutil.ignore_patterns('.git','target','__pycache__'))
    spec=importlib.util.spec_from_file_location('weighted_patch',PATCH/'apply_patch.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    patched=module.apply(source)
    tier_recipe = None
    if args.tier_admission:
        tier = ROOT/'patched/tier_admission'
        spec = importlib.util.spec_from_file_location('tier_patch', tier/'apply_patch.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        patched.update(module.apply(source))
        tier_recipe = {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted(tier.iterdir()) if p.is_file()}
    target = args.target_cache.resolve() if args.target_cache else out/'target'
    env=dict(os.environ,CARGO_BUILD_JOBS=str(args.jobs),CARGO_TARGET_DIR=str(target),
             CARGO_PROFILE_DEV_DEBUG='0',PYO3_PYTHON=sys.executable)
    with (out/'build.log').open('w') as log:
        command=['cargo','build','--locked','--manifest-path',str(source/'lib/bindings/python/Cargo.toml'),
                 '--features','select-service']+(['--release'] if args.release else [])
        if sys.platform == 'darwin':
            # PyO3 extension symbols resolve against the importing interpreter.
            # Apply these arguments to the extension only, not all dependencies.
            command[1]='rustc'
            command+=['--lib','--','-C','link-arg=-undefined','-C','link-arg=dynamic_lookup']
        subprocess.run(command,cwd=source/'lib/bindings/python',env=env,
                       stdout=log,stderr=subprocess.STDOUT,check=True)
    dest=out/'python/dynamo';dest.mkdir(parents=True)
    native=target/('release' if args.release else 'debug')/('lib_core.dylib' if sys.platform=='darwin' else 'lib_core.so')
    shutil.copy2(native,dest/'_core.abi3.so')
    receipt=dict(base_commit='b83b1d9304ebfc624709ac46db32b1b6f1ff1615',patch=patched,
        overlay_recipe_sha256=hashlib.sha256((PATCH/'apply_patch.py').read_bytes()).hexdigest(),
        native_sha256=hashlib.sha256(native.read_bytes()).hexdigest(),
        cargo_lock_sha256=hashlib.sha256((source/'lib/bindings/python/Cargo.lock').read_bytes()).hexdigest(),
        python_path=str(dest.parent),profile='release' if args.release else 'dev; debug=0',
        toolchain=subprocess.check_output(['rustc','--version'],text=True).strip())
    if tier_recipe is not None:
        receipt['tier_admission_recipe'] = tier_recipe
    receipt['target_cache'] = str(target)
    (out/'build.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))

if __name__=='__main__':main()
