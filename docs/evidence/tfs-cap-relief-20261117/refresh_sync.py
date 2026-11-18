"""刷新 RUNTIME_SYNC.json（2026-11-18 合并 origin/main 后）。

对列出的 lql_dev 侧差异文件：
- files_sha256 更新为 lql_dev 当前（合并融合体）SHA；
- source_exceptions 记录研究版（research_commit 6c7ec19）SHA 与理由。
用法：python refresh_sync.py <dynamo_vllm_cpu_sim 绝对路径>
"""
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest_path = root / 'RUNTIME_SYNC.json'
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
REASON = ('lql_dev 侧改动（tfs-lease 臂与/或 cap_policy/overdraft 探索，'
          '及与 2026-11-18 服务器对齐 842b925 的合并融合）；研究版未含')


def digest(p):
    return hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


changed = [
    'scripts/run_server_benchmark.py',
    'patched/weighted_routing/pending_score.rs',
    'scripts/check_routing_accounting.py',
    'scripts/run_tfs_stack.py',
    'sim/stack/admission_service.py',
    'sim/stack/connector_rpc.py',
    'sim/stack/executor.py',
    'sim/stack/layerwise.py',
    'sim/stack/load_observation.py',
    'sim/stack/scheduler_admission.py',
    'sim/stack/storage_client.py',
    'sim/stack/storage_hint.py',
    'sim/stack/storage_service.py',
]
for name in changed:
    old = manifest['files_sha256'][name]
    new = digest(root / name)
    if old == new:
        print(f'unchanged: {name}')
        continue
    manifest['files_sha256'][name] = new
    rule = manifest.setdefault('source_exceptions', {}).setdefault(name, {})
    if 'sha256' in rule:
        rule['sha256_lqldev_base'] = rule['sha256']
    rule['sha256'] = old
    rule['reason'] = REASON
    print(f'updated: {name}\n  delivery {new[:16]}… research {old[:16]}…')

manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('manifest written')
