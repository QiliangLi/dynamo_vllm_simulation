"""预扫完整 Mooncake 文件，再导出所选行供多进程 DV 执行。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sim.mooncake import derive_file, export_stack_rows, sha256_file


def prepare(source, output, lines=None, time_scale=1.0, config=None):
    from transformers import AutoTokenizer
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    derived, report = derive_file(source, output/(Path(source).name+".hit.jsonl"))
    tok = AutoTokenizer.from_pretrained(str(ROOT/"configs/model"))
    safe = [tok.encode(w)[0] for w in (" hello", " world", " test", " data", " time", " request")]
    rows = export_stack_rows(derived, Path(source).name, safe, lines, time_scale)
    for row in rows:
        if tok.encode(tok.decode(row["prompt_token_ids"])) != row["prompt_token_ids"]:
            raise ValueError("synthetic prompt does not roundtrip")
    cfg = json.loads(Path(config or ROOT/"configs/mooncake_first_seen.json").read_text())
    cfg.update(cache_model="first_seen", block_size=1,
               max_model_len=max(cfg["max_model_len"],max(len(r["prompt_token_ids"]) for r in rows)+1),
               enable_prefix_caching=False)
    cfg["num_blocks"] = max(cfg["num_blocks"],cfg["max_model_len"]*cfg["max_num_seqs"]+1)
    from sim.request_profile import normalize_config
    cfg = normalize_config(cfg)
    (output/"trace.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    (output/"config.json").write_text(json.dumps(cfg,indent=2)+"\n")
    report.update(source=str(Path(source).resolve()),exported_rows=len(rows),
        source_lines=[r["mooncake_source"]["line"] for r in rows],time_scale=time_scale,
        trace_sha256=sha256_file(output/"trace.jsonl"),
        semantics="Full-file pre-scan before selection; frozen per-request hits; synthetic text; output=1; block_size=1.")
    (output/"derive_report.json").write_text(json.dumps(report,indent=2)+"\n")
    return rows,cfg,report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source",required=True,type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--lines",type=int,nargs="+",help="原文件的 1-based 行号；省略时导出全部")
    p.add_argument("--time-scale",type=float,default=1)
    p.add_argument("--config",type=Path)
    p.add_argument("--check-only",type=Path,help="只核对该派生文件与完整原文件一致")
    a = p.parse_args()
    if a.check_only:
        _,report = derive_file(a.source,a.check_only,check_only=True)
    else:
        if a.output is None:
            p.error("--output is required when exporting")
        _,_,report = prepare(a.source,a.output,a.lines,a.time_scale,a.config)
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
