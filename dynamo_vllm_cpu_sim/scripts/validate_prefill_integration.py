"""少量 Mooncake 长度/命中样本：DES、真实进程栈、独立公式三方对拍。"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare(source, directory):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(str(ROOT / "configs/model"))
    words = [" hello", " world", " test", " data", " cache", " time", " request"]
    safe = [tok.encode(w)[0] for w in words if len(tok.encode(w)) == 1]
    raw = [json.loads(s) for s in source.read_text().splitlines()]
    picked, rows = [], []
    for line in [4, 14, 17, 1]:
        record = raw[line - 1]
        rng = random.Random(20261022 + line)
        tokens = [rng.choice(safe) for _ in range(record["input_length"])]
        assert tok.encode(tok.decode(tokens)) == tokens
        remote = min(record["hit_tokens"], ((len(tokens)-1)//16)*16)
        row = dict(id=f"mooncake-line-{line}", arrival_s=0.0, prompt_token_ids=tokens,
                   output_tokens=1, remote_prefix_tokens=remote, priority=0)
        rows.append(row)
        picked.append(dict(line=line, source=record, id=row["id"],
                           simulated_remote_tokens=remote,
                           simulated_new_tokens=len(tokens)-remote))
    trace = directory / "trace.jsonl"
    trace.write_text("".join(json.dumps(r)+"\n" for r in rows))
    write(directory / "source.json", dict(path=str(source),
          sha256=hashlib.sha256(source.read_bytes()).hexdigest(), selected=picked,
          semantics="只保留长度和推导命中量；合成隔离前缀，无原始文本或跨请求共享结构。输出=1，到达同时。"))
    cfg = dict(workers=1, scheduler="upstream", policy="fcfs", block_size=16,
               num_blocks=8192, max_model_len=16384, max_num_seqs=4,
               max_num_batched_tokens=8192, enable_prefix_caching=False,
               compute=dict(mode="glm5_d950pr", sparse_HBM_hit_ratio=1.0, engine_overhead_s=0),
               storage=dict(disks=1, paths_per_disk=128, disk_Bps=40e9,
                            shared_link_Bps=2e9, path_Bps=2e9,
                            read_latency_s=1e-4, kv_bytes_per_token=78*1408,
                            bandwidth_policy="active_split"),
               ttft_slo_s=10.0, storage_interval_log=True,
               request_profile=dict(profile_token_budget=8192))
    write(directory / "config.json", cfg)
    return cfg, rows


def run_des(cfg, rows, directory, env):
    cases = [("single", rows[:1], 8192), ("remote", rows[1:2], 8192),
             ("heterogeneous", rows[:3], 8192), ("chunked", rows[3:], 2048)]
    for name, selected, budget in cases:
        folder = directory / f"des_{name}"
        folder.mkdir()
        c = deepcopy(cfg)
        c["max_num_batched_tokens"] = budget
        c["request_profile"]["profile_token_budget"] = budget
        write(folder / "config.json", c)
        (folder / "trace.jsonl").write_text("".join(json.dumps(r)+"\n" for r in selected))
        start = time.perf_counter()
        with (folder / "run.log").open("w") as log:
            subprocess.run([sys.executable, "-m", "sim.run", "--config", str(folder/"config.json"),
                            "--trace", str(folder/"trace.jsonl"), "--output", str(folder)],
                           cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=120)
        write(folder/"wall.json", dict(process_wall_s=time.perf_counter()-start))
        subprocess.run([sys.executable, "scripts/check_results.py", str(folder)],
                       cwd=ROOT, env=env, check=True)
        print(f"DES {name} passed", flush=True)


def check_timelines(directory, formula):
    """对单请求、组批和分块案例另算业务完成时间，不使用程序的 duration 求和。"""
    results = []
    for folder in sorted(directory.glob("des_*")):
        report = json.loads((folder/"report.json").read_text())
        parts = [r for r in formula["rows"] if str(folder) in r["path"]]
        compute = sum(r["manual_s"] for r in parts)
        extra = 0.0
        if folder.name == "des_remote":
            storage = report["config"]["storage"]
            extra = storage["read_latency_s"] + report["bytes_read"]/storage["shared_link_Bps"]
        # heterogeneous: 两个读取在首个冷请求计算结束前完成，读时被覆盖。
        expected = compute + extra
        error = abs(expected-report["makespan_s"])
        assert error < 1e-12, (folder, expected, report["makespan_s"])
        results.append(dict(case=folder.name, manual_compute_s=compute,
                            exposed_read_s=extra, manual_makespan_s=expected,
                            simulated_makespan_s=report["makespan_s"], error_s=error))
    return results


def check_shapes(directory):
    """核对真实执行的 h/u 连续性与原样本长度，避免公式虽一致却喂错形状。"""
    checked = []
    for folder in [*sorted(directory.glob("des_*")), *sorted(directory.glob("stack"))]:
        trace = [json.loads(s) for s in (folder/"trace.jsonl").read_text().splitlines()]
        path = folder/("worker.steps.jsonl" if folder.name == "stack" else "events.jsonl")
        progress, initial = {}, {}
        rows = [json.loads(s) for s in path.read_text().splitlines()]
        received = set()
        for row in rows:
            if row.get("event") == "kv_received":
                received.update(row.get("rids",[]))
            if not row.get("timing"):
                continue
            ids = row.get("req_ids",list(row["num_scheduled_tokens"]))
            shapes = row["timing"]["shapes"]
            assert len(ids) == len(shapes)
            for rid,(h,u) in zip(ids,shapes):
                if rid in progress:
                    assert h == progress[rid], (rid,h,progress[rid])
                else:
                    initial[rid] = h
                    if folder.name == "stack" and h:
                        assert rid in received, ("warm compute before KV completion",rid)
                assert row["num_scheduled_tokens"][rid] == u
                progress[rid] = h+u
        expected = sorted((len(r["prompt_token_ids"]),r["remote_prefix_tokens"]) for r in trace)
        actual = sorted((end,initial[rid]) for rid,end in progress.items())
        assert actual == expected, (folder,actual,expected)
        checked.append(dict(case=folder.name, requests=len(progress),
                            actual_end_and_initial_tokens=actual, status="pass"))
    return checked


def run_stack(cfg, rows, directory, python, env):
    folder = directory / "stack"
    folder.mkdir()
    c = deepcopy(cfg)
    c["max_num_batched_tokens"] = 2048
    c["request_profile"]["profile_token_budget"] = 2048
    write(folder/"config.json", c)
    (folder/"trace.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    roots, handles = [], []
    port = 28765
    base = dict(env, DYN_DISCOVERY_BACKEND="file", DYN_FILE_KV=str(folder/"discovery"),
                DYN_REQUEST_PLANE="tcp", SIM_STACK_CONFIG=str(folder/"config.json"),
                OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    start = time.perf_counter()

    def launch(role, args, extra=None):
        log = (folder/f"{role}.log").open("w")
        handles.append(log)
        proc = subprocess.Popen([str(python), *args], cwd=ROOT,
                                env=dict(base, **(extra or {})), stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        roots.append((role,proc))

    try:
        launch("storage", ["-m", "sim.stack.storage_service", "--config", str(folder/"config.json"),
                           "--trace", str(folder/"trace.jsonl"), "--socket", str(folder/"storage.sock")])
        deadline = time.monotonic()+40
        while not (folder/"storage.sock").exists():
            if roots[-1][1].poll() is not None or time.monotonic()>deadline:
                raise RuntimeError("storage startup failed; see storage.log")
            time.sleep(0.2)
        launch("frontend", ["-m", "dynamo.frontend", "--http-port", str(port),
                            "--router-mode", "round-robin"])
        launch("worker", ["scripts/stack_vllm_worker.py", "--model", "configs/model",
                          "--served-model-name", "sim-opt", "--endpoint", "dyn://dynamo.backend.generate",
                          "--dtype", "float32", "--max-model-len", "16384", "--block-size", "16",
                          "--num-gpu-blocks-override", "8192", "--max-num-seqs", "4",
                          "--max-num-batched-tokens", "2048", "--enforce-eager", "--seed", "0",
                          "--no-enable-prefix-caching",
                          "--kv-transfer-config", '{"kv_connector":"SimConnector","kv_role":"kv_both"}'],
               dict(SIM_STACK_WORKER_ID="0", SIM_STACK_TOKEN_ID="290", DYN_SYSTEM_PORT="29111",
                    SIM_STACK_STORAGE_SOCKET=str(folder/"storage.sock"),
                    SIM_STACK_STEP_LOG=str(folder/"worker.steps.jsonl")))
        deadline = time.monotonic()+100
        while True:
            if any(p.poll() is not None for _,p in roots):
                raise RuntimeError("stack process exited during startup; see logs")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models",timeout=1) as response:
                    ready = "sim-opt" in json.dumps(json.load(response))
                if ready:
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic()>deadline:
                raise TimeoutError("stack ready timeout")
            time.sleep(0.5)
        ready_s = time.perf_counter()-start
        from replay_http import replay
        replay_start = time.perf_counter()
        code = asyncio.run(replay(folder/"trace.jsonl",f"http://127.0.0.1:{port}","sim-opt",
                                  folder,False,"completions"))
        replay_s = time.perf_counter()-replay_start
        result = json.loads((folder/"report.json").read_text())
        assert code == 0 and result["n_ok"] == len(rows) and result["n_skipped"] == 0
        from sim.stack.storage_client import StorageClient
        client = StorageClient(str(folder/"storage.sock"))
        write(folder/"storage_final.json", client.stats())
        client.close()
        snapshot = subprocess.check_output(["ps","-eo","pid,ppid,args"],text=True)
        (folder/"processes.txt").write_text(snapshot)
        write(folder/"wall.json",dict(startup_wall_s=ready_s,replay_wall_s=replay_s,
                                      root_pids={role:p.pid for role,p in roots}))
        print(f"stack {result['n_ok']}/{len(rows)} passed",flush=True)
    finally:
        for _,p in reversed(roots):
            try:
                os.killpg(p.pid,signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _,p in roots:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid,signal.SIGKILL)
                p.wait(timeout=5)
        for handle in handles:
            handle.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--stack-python",type=Path)
    args = ap.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    env = dict(os.environ, VLLM_PLUGINS="", HF_HUB_OFFLINE="1", PYTHONHASHSEED="0",
               VLLM_ALLOW_LONG_MAX_MODEL_LEN="1", PYTHONPATH=str(ROOT), NO_PROXY="*", no_proxy="*")
    for key in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):
        env.pop(key,None)
        os.environ.pop(key,None)
    os.environ["NO_PROXY"] = "*"
    cfg, rows = prepare(args.source,out)
    provenance = {}
    for rel in ["sim/compute_time.py", "sim/engine.py", "sim/request_profile.py", "sim/run.py",
                "sim/stack/executor.py", "sim/stack/connector_rpc.py", "sim/stack/storage_client.py",
                "scripts/check_prefill_formula.py", "scripts/validate_prefill_integration.py"]:
        provenance[rel] = hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()
    write(out/"implementation.json",provenance)
    run_des(cfg,rows,out,env)
    if args.stack_python:
        run_stack(cfg,rows,out,args.stack_python,env)
    from check_prefill_formula import check
    logs = sorted(out.glob("des_*/events.jsonl")) + sorted(out.glob("stack/*.steps.jsonl"))
    result = check(logs)
    write(out/"formula_check.json",result)
    write(out/"timeline_check.json",check_timelines(out,result))
    write(out/"shape_check.json",check_shapes(out))
    print(json.dumps({k:v for k,v in result.items() if k!="rows"}),flush=True)


if __name__ == "__main__":
    main()
