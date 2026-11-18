"""Add a launch-time switch to server-owned scripts. Never start/stop services."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime

ROOT = Path('/home/lql_agent/auto_deploy_vllm/drt_dev')
NAMES = [f'{i}.run_vllm_single_minimax_m2.5.bash' for i in range(4)] + ['4.run_dynamo.bash', '5.run_tfs_central.bash']
MARK = '# tfs-launch-switch-20261116-v1'
PARSER = '''
__launch_tfs_mode=on
while (($#)); do
    case "$1" in
        --tfs)
            if (($# < 2)); then echo '--tfs requires on or off' >&2; exit 2; fi
            __launch_tfs_mode="$2"; shift 2 ;;
        --tfs=*) __launch_tfs_mode="${1#*=}"; shift ;;
        -h|--help) echo "Usage: bash ${BASH_SOURCE[0]} [--tfs on|off] (default: on)"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$__launch_tfs_mode" in on|off) ;; *) echo '--tfs requires on or off' >&2; exit 2 ;; esac
'''
DISABLE = '''
if [[ "$__launch_tfs_mode" == off ]]; then
    for __launch_key in $(compgen -e); do
        case "$__launch_key" in
            DYN_TFS_*|DYN_TIER_PROFILE_*|DYN_WAITING_SCORE_*|SIM_STACK_*|TFS_NATIVE_INGRESS)
                unset "$__launch_key" ;;
        esac
    done
'''


def patch_script(text, name):
    if MARK in text:
        raise ValueError(f'Already patched: {name}')
    if 'native-ingress-20261116' not in text:
        raise ValueError(f'Expected current native-ingress server launcher: {name}')
    anchors = list(re.finditer(r"(?m)^setsid bash -c '\s*$", text))
    if len(anchors) != 1:
        raise ValueError(f'Expected one launcher: {name}')
    if re.search(r'(?m)^\s*(?:getopts|while\s+.*\$#)', text):
        raise ValueError(f'Existing option parser needs review: {name}')
    index = int(name[0])
    head = MARK + PARSER
    if index == 5:
        head += '''if [[ "$__launch_tfs_mode" == off ]]; then
    echo '[LAUNCH] TFS=off: central not started; existing processes are not stopped'
    exit 0
fi
'''
        override = 'echo "[LAUNCH] central TFS=on"\n'
    else:
        override = DISABLE
        if index < 4:
            if not all(x in text for x in ('DYN_TFS_CONFIG', 'VLLM_PLUGINS', 'tfs12', 'OdKvStorageConnector')):
                raise ValueError(f'Missing expected TFS/OD settings: {name}')
            override += '''    __launch_plugins=()
    IFS=',' read -r -a __launch_all_plugins <<< "${VLLM_PLUGINS:-ascend}"
    for __launch_plugin in "${__launch_all_plugins[@]}"; do
        if [[ -n "$__launch_plugin" && "$__launch_plugin" != tfs12 ]]; then
            __launch_plugins+=("$__launch_plugin")
        fi
    done
    export VLLM_PLUGINS="$(IFS=,; echo "${__launch_plugins[*]}")"
'''
        override += 'else\n'
        if index < 4:
            override += '''    if [[ -z "${DYN_TFS_CONFIG:-}" || ! -f "$DYN_TFS_CONFIG" || ",$VLLM_PLUGINS," != *,tfs12,* ]]; then
        echo 'TFS=on requires existing DYN_TFS_CONFIG and tfs12 plugin' >&2; exit 2
    fi
'''
        else:
            override += '''    if [[ "${DYN_TFS_NATIVE_INGRESS:-}" != 1 || -z "${DYN_TFS_INGRESS_URL:-}" || -z "${DYN_TIER_PROFILE_URL:-}" || -z "${DYN_TIER_PROFILE_TOKEN:-}" || -z "${DYN_WAITING_SCORE_FACTOR:-}" || -z "${DYN_WAITING_SCORE_READY:-}" ]]; then
        echo 'TFS=on requires complete native ingress and waiting-score settings' >&2; exit 2
    fi
'''
        override += 'fi\necho "[LAUNCH] TFS=$__launch_tfs_mode namespace=${DYN_NAMESPACE:-default}"\n'
    pos = anchors[0].start()
    result = text[:pos] + '\n' + MARK + '\n' + override + '\n' + text[pos:]
    insert = result.index('\n') + 1 if result.startswith('#!') else 0
    return result[:insert] + head + '\n' + result[insert:]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix='.tfs-switch-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(root, apply=False):
    changes = []
    for name in NAMES:
        path = root / name
        old = path.read_bytes()
        new = patch_script(old.decode('utf-8'), name).encode('utf-8')
        subprocess.run(['bash', '-n'], input=new, check=True)
        changes.append((path, old, new, path.stat().st_mode & 0o777))
        print('CHECK_OK=' + name)
    if not apply:
        print('PREFLIGHT_PASS; no files changed')
        return
    backup_root = root / 'temp'
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='tfs_switch_' + datetime.now().strftime('%Y%m%d_') , dir=backup_root))
    os.chmod(backup, 0o700)
    records = []
    for path, old, new, mode in changes:
        (backup / path.name).write_bytes(old)
        records.append(dict(path=str(path.resolve()), name=path.name, before=sha(old), after=sha(new), mode=mode))
    (backup / 'manifest.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
    print('BACKUP=' + str(backup), flush=True)
    written = []
    try:
        for path, old, new, mode in changes:
            if path.read_bytes() != old:
                raise RuntimeError('Script changed during installation: ' + str(path))
            atomic(path, new, mode)
            written.append((path, old, new, mode))
    except BaseException:
        for path, old, new, mode in reversed(written):
            if path.read_bytes() == new:
                atomic(path, old, mode)
        raise
    print('APPLY_PASS; launch arguments added; services not restarted')


def rollback(root, backup):
    records = json.loads((backup / 'manifest.json').read_text())
    if {r['name'] for r in records} != set(NAMES) or len(records) != len(NAMES):
        raise ValueError('Unexpected backup manifest')
    for r in records:
        path = root / r['name']
        if str(path.resolve()) != r['path'] or sha(path.read_bytes()) != r['after']:
            raise ValueError('Current script changed after installation: ' + r['name'])
        if sha((backup / r['name']).read_bytes()) != r['before']:
            raise ValueError('Backup checksum mismatch')
    for r in records:
        atomic(root / r['name'], (backup / r['name']).read_bytes(), r['mode'])
    print('ROLLBACK_PASS; services not restarted')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=ROOT)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--check', action='store_true')
    g.add_argument('--apply', action='store_true')
    g.add_argument('--rollback', type=Path, metavar='BACKUP')
    a = p.parse_args()
    if a.rollback:
        rollback(a.root.resolve(), a.rollback.resolve())
    else:
        install(a.root.resolve(), a.apply)


if __name__ == '__main__':
    main()
