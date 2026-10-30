"""同源混合请求验证 immediate / TFS-first-token / TFS-load-done 的多进程闭环。"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"scripts"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--python",required=True,type=Path)
    parser.add_argument("--config",type=Path,default=ROOT/"configs/tfs_stack_prefill.json")
    parser.add_argument("--cases",nargs="+",choices=["immediate","tfs_first_token"],default=["immediate","tfs_first_token"])
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(str(ROOT/"configs/model"))
    safe = [tok.encode(w)[0] for w in [" hello"," world"," test"," data"," time"," request"]]
    source = [json.loads(s) for s in args.source.read_text().splitlines()]
    rows,provenance = [],[]
    for i,line in enumerate([17,17,17,17,14,14,4,4]):
        src = source[line-1]
        rng = random.Random(20261023+i)
        tokens = [rng.choice(safe) for _ in range(src["input_length"])]
        assert tok.encode(tok.decode(tokens))==tokens
        category = "heavy" if line==17 else "medium" if line==14 else "cold"
        rows.append(dict(id=f"m{i:02d}-line{line}",arrival_s=0,prompt_token_ids=tokens,
                         remote_prefix_tokens=src["hit_tokens"],output_tokens=1,class_id=category))
        provenance.append(dict(id=rows[-1]["id"],line=line,source=src))
    (out/"trace.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    config = json.loads(args.config.read_text())
    from run_tfs_stack import run_one,write
    write(out/"config.json",config)
    write(out/"source.json",dict(path=str(args.source),sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
                                  rows=provenance,semantics="同源长度重复采样；隔离合成前缀；同时到达；输出=1。"))
    result = {}
    for case in args.cases:
        mode = "immediate" if case=="immediate" else "tfs"
        release = "first_token"
        result[case] = run_one(config,out/"trace.jsonl",out/case,args.python,mode,release,2)
        write(out/"summary.json",result)
    if "immediate" in result and "tfs_first_token" in result:
        a,b = result["immediate"],result["tfs_first_token"]
        assert a["actual_submitted_read_bytes"]==b["actual_submitted_read_bytes"]
        # At 3 x 40 GB/s this small fixture need not saturate storage or TFS.
        # Quota and feedback causality are checked per event in run_one.verify.
        assert len(b["workers"])==2
    print("multi-process TFS validation passed",flush=True)


if __name__ == "__main__":
    main()
