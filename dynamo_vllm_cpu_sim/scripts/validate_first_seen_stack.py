"""全量核对派生指纹，真实多进程核对冷/部分/尾块全命中请求的 h/u。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"scripts"))
from sim.mooncake import derive_file, sha256_file
from prepare_mooncake import prepare
from run_tfs_stack import run_one, write


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trace-dir",required=True,type=Path)
    p.add_argument("--output",required=True,type=Path)
    p.add_argument("--python",required=True,type=Path)
    p.add_argument("--cases",nargs="+",choices=["immediate","tfs_first_token"],
                   default=["immediate","tfs_first_token"])
    a = p.parse_args()
    out = a.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    reference = a.trace_dir/"derived/derive_report.json"
    references = {r["file"]:r for r in json.loads(reference.read_text())["files"]}
    reports = []
    for name in ("conversation_trace.jsonl","toolagent_trace.jsonl","synthetic_trace.jsonl"):
        _,report = derive_file(a.trace_dir/name,out/"derived"/(name+".hit.jsonl"))
        derive_file(a.trace_dir/name,out/"derived"/(name+".hit.jsonl"),check_only=True)
        expected = references[name]
        for field in ("input_sha256","derived_sha256","n_rows","timestamps","max_same_ts_group",
                      "full_hit_rows","token_hit_ratio","request_hit_ratio"):
            assert report[field]==expected[field],(name,field,report[field],expected[field])
        reports.append(dict(file=name,**report))
    write(out/"derive_check.json",dict(status="pass",reference_sha256=sha256_file(reference),files=reports))
    rows,cfg,_ = prepare(a.trace_dir/"conversation_trace.jsonl",out/"prepared",
                         lines=[1,2,14,17,262,323],time_scale=.001)
    assert any(r["u_tokens"]==1 for r in rows)
    assert rows[0]["hit_tokens"]==rows[1]["hit_tokens"]==0
    results = {}
    for case in a.cases:
        mode = "immediate" if case=="immediate" else "tfs"
        release = "first_token"
        results[case] = run_one(cfg,out/"prepared/trace.jsonl",out/case,a.python,mode,release,2)
        write(out/"summary.json",results)
    assert len({r["actual_submitted_read_bytes"] for r in results.values()})==1
    print("first_seen full-file parity and multi-process h/u checks passed",flush=True)


if __name__ == "__main__":
    main()
