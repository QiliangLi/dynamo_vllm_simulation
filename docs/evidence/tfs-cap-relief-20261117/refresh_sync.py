"""刷新 RUNTIME_SYNC.json：lql_dev cap 策略扩展的三个共享文件。

- files_sha256 更新为 lql_dev 当前 SHA；
- source_exceptions 记录研究版（research_commit 755f051）旧 SHA 与理由，
  保持 --source 对比诚实；
- 新增 tests/test_admission_cap_policy.py 为 lql_dev 独有，不进并集
  （合回研究版时随下次并集刷新进入）。
用法：python refresh_sync.py <dynamo_vllm_cpu_sim 绝对路径>
"""
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest_path = root / 'RUNTIME_SYNC.json'
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
CHANGED = {
    'sim/admission.py':
        'lql_dev cap 数值来源策略扩展（cap_policy legacy/measured/fixed 与实测样本采集）；研究版未含，默认 legacy 行为逐值一致',
    'sim/request_profile.py':
        'lql_dev admission 新键默认值与校验（cap_policy/cap_fixed_int/cap_measured_min_samples/overdraft_*）；研究版未含，缺省行为不变',
    'sim/stack/read_admission.py':
        'lql_dev acquire 判满弹性超发旁路（overdraft_wait_s/overdraft_max_extra，默认关闭）；研究版未含',
    'scripts/check_read_admission.py':
        'lql_dev 验收脚本接受 overdraft 获准并核对超发上限与 read_permit_overdraft 记录；研究版未含该 reason',
}


def digest(p):
    return hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


for name, reason in CHANGED.items():
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
    rule['reason'] = reason
    print(f'updated: {name}\n  delivery {new[:16]}… research {old[:16]}…')

manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('manifest written')
