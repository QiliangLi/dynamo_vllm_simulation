"""Replace the report timeline using the shared, read-only plotting script."""
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys


def refresh(root,redraw=True):
    root=Path(root).resolve()
    workspace=next((p for p in root.parents if (p/'workspace.json').is_file()),None)
    script=(workspace/'tooling/redraw_timeline.py') if workspace else (root/'reproduce/redraw_timeline.py')
    if not script.is_file():
        raise FileNotFoundError(f'找不到固定重绘脚本：{script}')
    output=root/'online_timeline'
    if redraw:
        process=subprocess.run([sys.executable,str(script),str(root),
            '--title','Mooncake360｜90 GB/s｜直接组合五方案｜在线轻重请求'],
            check=True,capture_output=True,text=True,encoding='utf-8')
        print(process.stdout.strip(),flush=True)
    else:
        validation=json.loads((output/'validation.json').read_text(encoding='utf-8'))
        assert validation['status']=='passed'
        for files in validation['source_files'].values():
            for path,entry in files.items():
                assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==entry['sha256']
        for name,entry in validation['output_files'].items():
            assert hashlib.sha256((output/name).read_bytes()).hexdigest()==entry['sha256']
    with (root/'report_data/run_summary.csv').open(encoding='utf-8-sig',newline='') as stream:
        original={r['mode']:r for r in csv.DictReader(stream)}
    with (output/'summary.csv').open(encoding='utf-8-sig',newline='') as stream:
        redrawn={r['case']:r for r in csv.DictReader(stream)}
    with (root/'report_data/device_utilization.csv').open(encoding='utf-8-sig',newline='') as stream:
        devices=list(csv.DictReader(stream))
    assert set(original)==set(redrawn)
    for key,row in redrawn.items():
        old=original[key]
        assert math.isclose(float(row['span_s'])*1000,float(old['completion_ms']),abs_tol=1e-7)
        assert math.isclose(float(row['model_s']),float(old['model_compute_s']),abs_tol=1e-7)
        selected=[r for r in devices if r['mode']==key]
        assert math.isclose(float(row['red_s']),sum(float(r['io_without_model_compute_s']) for r in selected),abs_tol=1e-7)
        assert math.isclose(float(row['gray_s']),sum(float(r['other_s']) for r in selected),abs_tol=1e-7)
    image=root/'figure/02_full-sort-ms.png'
    shutil.copy2(output/'timeline.png',image)
    receipt=dict(status='passed',simulations_run=0,cases=len(redrawn),requests=sum(int(r['requests']) for r in redrawn.values()),
        source_script=str(script),script_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
        image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        timeline_validation_sha256=hashlib.sha256((output/'validation.json').read_bytes()).hexdigest(),
        published_time_accounts_equal=True)
    (root/'report_data/timeline_replacement.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return receipt


if __name__=='__main__':
    print(json.dumps(refresh(Path(__file__).resolve().parent),ensure_ascii=False))
