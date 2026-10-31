"""启动真实多进程 DV + TFS 服务、回放、验收并仅清理本次进程组。"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"scripts"))


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n")


def save_admission_snapshot(folder, status):
    """Keep each final refresh's raw ledger and its offline report copy separate."""
    if (status.get('execution_feedback') in ('disabled', 'lease_events')
            and not status.get('load_observation_source')):
        from sim.stack.load_observation import with_local_observations
        write(folder/'admission.runtime.final.json', status)
        status = with_local_observations(status, sorted(folder.glob('worker*.loads.jsonl')))
    write(folder/'admission.final.json', status)
    return status


def request_json(url):
    with urllib.request.urlopen(url,timeout=2) as response:
        return json.load(response)


def frontend_command(config, workers):
    """Use native selection; never substitute an external worker picker."""
    mode = config.get("router_mode", "round-robin")
    if mode not in ("kv", "round-robin"):
        raise ValueError("router_mode must be kv or round-robin")
    command = ["-m", "dynamo.frontend", "--http-port", "28765", "--router-mode", mode]
    if mode == "kv":
        command += ["--no-router-kv-events", "--no-router-assume-kv-reuse",
                    "--router-kv-overlap-score-credit", "0", "--router-temperature", "0",
                    "--router-track-prefill-tokens", "--router-min-initial-workers", str(workers)]
    return command


def router_metrics():
    with urllib.request.urlopen("http://127.0.0.1:28765/metrics", timeout=2) as response:
        text = response.read().decode()
    return [line for line in text.splitlines()
            if line.startswith("dynamo_frontend_worker_active_")]


def frontend_has_workers(snapshot, expected):
    """The model being listed does not imply all expected engines are routable."""
    namespace = snapshot.get("namespaces",{}).get("dynamo",{})
    return (snapshot.get("model")=="sim-opt" and snapshot.get("ready") is True
            and namespace.get("ready") is True
            and namespace.get("worker_types",{}).get("aggregated",{}).get("workers")==expected)


def verify(folder,status,report,storage):
    if status.get('execution_feedback') in ('disabled', 'lease_events'):
        status = save_admission_snapshot(folder, status)
        assert not any(e['event']=='feedback_received' and e.get('engine_id') for e in status['events'])
    records = status["records"]
    assert status["status"]=="ready" and not status["invalid_reasons"]
    assert len(records)==report["n_total"]==report["n_ok"] and report["n_skipped"]==0
    assert not status["snapshot"]["pending_ids"] and status["delayed_events"]==0
    assert all(r["state"]=="FINISHED" and not r["lease_open"] for r in records.values())
    assert storage["pending_reads"]==0
    gc_state = storage['gc_window']
    assert not gc_state['active'] and not gc_state['failure'], gc_state
    if gc_state['policy'] == 'defer_during_run':
        assert gc_state['memory_samples'] > 0
        assert gc_state['events'][0]['event'] == 'begin'
        assert gc_state['events'][-1]['event'] == 'end'
        assert gc_state['events'][-1]['drained_collection']
        assert gc_state['enabled'] == gc_state['events'][0]['enabled_before']
    byte_count = sum(r["submitted_read_bytes"] for r in records.values())
    assert byte_count==storage["bytes_submitted"]
    assert abs(storage["bytes_transferred"]-byte_count)<1
    config = json.loads((folder/"config.json").read_text())
    from sim.stack.clock import TimeScale
    clock = TimeScale.from_config(config)
    assert status.get("wall_time_scale",1.)==report.get("wall_time_scale",1.)==storage.get("wall_time_scale",1.)==clock.factor
    sc = config["storage"]
    disk_bytes = storage["disk_bytes_submitted"]
    assert len(disk_bytes)==sc["disks"] and sum(disk_bytes)==byte_count
    assert all(abs(a-b)<1 for a,b in zip(disk_bytes,storage["disk_bytes_transferred"]))
    # 数千个块的速率累加会有浮点尾差；120 GB/s 池采用相对 1e-12 容差。
    rate_tolerance = max(1e-3,sc["disk_Bps"]*1e-12)
    assert all(rate <= sc["disk_Bps"]+rate_tolerance for rate in storage["disk_peak_Bps"])
    # Each disk is a separate bottleneck. Other disks cannot serve its blocks.
    transfer_lower_bound = max(disk_bytes,default=0)/sc["disk_Bps"]
    if sc.get("shared_link_Bps") is not None:
        transfer_lower_bound = max(transfer_lower_bound,byte_count/sc["shared_link_Bps"])
    from sim.stack.layerwise import transfer_mode
    layered = transfer_mode(config) != "bulk"
    if layered:
        from check_layerwise import check as check_layers
        layer_audit = check_layers(folder,config,storage)
        write(folder/"layer_check.json",layer_audit)
    if sc.get("record_reads") and not layered:
        from collections import Counter
        expected = Counter()
        from sim.engine import make_request
        for row in (json.loads(line) for line in (folder/"trace.jsonl").read_text().splitlines() if line.strip()):
            req = make_request(row,config["block_size"],"fcfs")
            end = min(row.get("remote_prefix_tokens",0)//config["block_size"],
                      (len(row["prompt_token_ids"])-1)//config["block_size"])
            expected.update(b.hex() for b in req.block_hashes[:end])
        actual = Counter(h for read in storage["read_history"] for h in read["blocks_hex"])
        assert actual==expected, "actual RPC blocks differ from trace preloaded prefixes"
        disk_totals = [0]*sc["disks"]
        for read in storage["read_history"]:
            amounts = [0]*sc["disks"]
            for h in read["blocks_hex"]:
                block = bytes.fromhex(h)
                d = max(range(sc["disks"]),key=lambda d:
                        hashlib.sha256(block+d.to_bytes(4,"little")).digest())
                amounts[d] += config["block_size"]*sc["kv_bytes_per_token"]
            assert amounts==read["disk_bytes"]
            disk_totals = [a+b for a,b in zip(disk_totals,amounts)]
            lower = sc["read_latency_s"]+max(amounts)/sc["disk_Bps"]
            if sc.get("shared_link_Bps") is not None:
                lower = sc["read_latency_s"]+max(max(amounts)/sc["disk_Bps"],sum(amounts)/sc["shared_link_Bps"])
            assert read["completed_s"]-read["submitted_s"]+1e-8>=lower
        assert disk_totals==disk_bytes
    if layered and config.get("cache_model")=="first_seen":
        from sim.engine import make_request
        from sim.mooncake import engine_key
        rows = {r["id"]:r for r in (json.loads(s) for s in (folder/"trace.jsonl").read_text().splitlines())}
        for rid,load in storage["layer_loads"].items():
            row = rows[storage["hit_bindings"][engine_key(rid)]]
            req = make_request(row,config["block_size"],"fcfs")
            assert load["blocks_hex"]==[b.hex() for b in req.block_hashes[:row["hit_tokens"]//config["block_size"]]]
    transfer_span = storage["last_completion_s"]-storage["first_submit_s"] if byte_count else 0
    assert transfer_span+1e-8>=transfer_lower_bound
    assert report.get("model_elapsed_s",report["wall_clock_s"])+1e-8>=transfer_lower_bound
    events = status["events"]
    for e in events:
        if e["event"]=="admission_reserved" and e["heavy"] and e["reason"]=="normal" and e["mode"]=="tfs":
            assert e["held_after"]<=e["cap"]
        if e["event"]=="feedback_received" and e["kind"]=="load_submitted":
            r = records[e["id"]]
            assert e["occurred_s"]>=r["submitted_s"]
            forwarded = next(x for x in events if x["event"]=="http_forwarded" and x["id"]==e["id"])
            assert e["occurred_s"]>=forwarded["t"]
        if e["event"]=="feedback_delivered":
            assert e["t"]>=e["visible_s"]>=e["occurred_s"]
    for rid,r in records.items():
        assert r["profile"]["arrival_s"]<=r["submitted_s"]<=r["first_token_s"]<=r["finish_s"]
        if r["load_attempts"]:
            assert r["load_submit_s"]<=r["load_done_s"]<=r["first_token_s"]
        release = next((e for e in reversed(events) if e["event"]=="admission_released" and e["id"]==rid), None)
        if release is None:
            assert config["admission"].get("placement") == "scheduler" and not r["lease_open"]
            assert not any(p["id"] == rid for p in status["permits"].values())
            continue
        if r["lease_release_reason"]=="first_token":
            assert release["t"]>=r["first_token_s"]
        elif r["lease_release_reason"]=="load_completed":
            assert release["t"]>=r["load_done_s"]
        elif r["lease_release_reason"]=="no_external_load":
            assert r["no_external_load"]
    if config.get("cache_model") == "first_seen":
        from sim.mooncake import engine_key
        traces = {row["id"]:row for row in (json.loads(s) for s in (folder/"trace.jsonl").read_text().splitlines() if s.strip())}
        assert storage["cache_model"]=="first_seen"
        assert set(storage["hit_bindings"].values())==set(traces)
        by_engine = storage["hit_bindings"]
        shapes = {rid:[] for rid in traces}
        for path in sorted(folder.glob("worker*.steps.jsonl")):
            for line in path.read_text().splitlines():
                step = json.loads(line)
                if not step.get("num_scheduled_tokens"):
                    continue
                assert len(step["timing"]["shapes"])==len(step["num_scheduled_tokens"])
                for engine_id,shape in zip(step["num_scheduled_tokens"],step["timing"]["shapes"]):
                    shapes[by_engine[engine_key(engine_id)]].append(shape)
        for rid,row in traces.items():
            record = records[rid]
            assert record["profile"]["estimated_remote_tokens"]==row["hit_tokens"]
            assert record["submitted_read_bytes"]==row["hit_tokens"]*sc["kv_bytes_per_token"]
            before = row["hit_tokens"]
            for h,u in shapes[rid]:
                assert h==before,(rid,h,before)
                before += u
            assert before==len(row["prompt_token_ids"])
            assert sum(u for _,u in shapes[rid])==row["u_tokens"]
            bound = next(e for e in events if e["event"]=="hit_contract_bound" and e["id"]==rid)
            forwarded = next(e for e in events if e["event"]=="http_forwarded" and e["id"]==rid)
            assert bound["t"]<=forwarded["t"]
        for q in storage["hit_queries"]:
            assert q["matched_tokens"]==max(0,q["hit_tokens"]-q["start_block"]*config["block_size"])
        write(folder/"hit_check.json",dict(status="pass",cache_model="first_seen",
            requests={rid:dict(hit_tokens=row["hit_tokens"],u_tokens=row["u_tokens"],
                              actual_shapes=shapes[rid],read_bytes=records[rid]["submitted_read_bytes"])
                      for rid,row in traces.items()}))
    if config["compute"].get("mode")=="sa_e26f":
        from check_sa_stack import check,check_storage
        formula=check(sorted(folder.glob("worker*.steps.jsonl")),config)
        write(folder/"sa_storage_check.json",check_storage(storage,config))
    else:
        from check_prefill_formula import check
        formula = check(sorted(folder.glob("worker*.steps.jsonl")))
    write(folder/"formula_check.json",formula)
    batch = [r["bs"] for r in formula["rows"]]
    summary = dict(status="pass",requests=len(records),
        workers=sorted({r["worker_id"] for r in records.values()}),
        central_wait_mean_s=sum(r["submitted_s"]-r["profile"]["arrival_s"] for r in records.values())/len(records),
        central_wait_max_s=max(r["submitted_s"]-r["profile"]["arrival_s"] for r in records.values()),
        held_heavy_peak=max(e.get("held_after",e.get("held",0)) for e in events
                            if e["event"] in ("admission_reserved","threshold_updated")),
        peak_storage_pending=storage["peak_pending_requests"],actual_batch_sizes=batch,
        model_sum_s=sum(r["program_s"] for r in formula["rows"]),formula_max_error_s=formula["max_abs_error_s"],
        ttft_mean_s=report["ttft_mean_s"],http_window_s=report["wall_clock_s"],
        model_window_s=report.get("model_elapsed_s",report["wall_clock_s"]),
        wall_time_scale=clock.factor,
        actual_submitted_read_bytes=sum(r["submitted_read_bytes"] for r in records.values()),
        transfer_lower_bound_s=transfer_lower_bound,transfer_span_s=transfer_span,
        disk_bytes_submitted=disk_bytes,disk_peak_Bps=storage["disk_peak_Bps"],
        release_reasons={rid:r["lease_release_reason"] for rid,r in records.items()})
    write(folder/"check.json",summary)
    if config["admission"].get("placement") == "scheduler":
        from check_read_admission import check as check_read_admission
        write(folder/"read_admission.check.json", check_read_admission(folder, status))
    if config["admission"].get("classification") == "bandwidth_line":
        from check_lease_accounting import check as check_lease
        write(folder/"lease_accounting.check.json", check_lease(folder, status, config))
    return summary


def run_one(config,trace,output,python,mode,release,workers=2):
    folder = Path(output).resolve()
    folder.mkdir(parents=True,exist_ok=False)
    cfg = deepcopy(config)
    cfg.setdefault('storage', {}).setdefault('gc_policy', 'defer_during_run')
    cfg["workers"] = workers
    cfg.setdefault("router_mode", "round-robin")
    frontend_args = frontend_command(cfg, workers)
    cfg.setdefault("admission",{}).update(mode=mode,release_on=release)
    from sim.request_profile import normalize_config
    cfg = normalize_config(cfg)
    lease_mode = cfg['admission']['classification'] == 'bandwidth_line'
    if cfg['routing_accounting']['enabled'] and not cfg['routing_accounting'].get('python_path'):
        raise ValueError('weighted accounting needs the isolated patched Dynamo python_path; see weighted-routing build instructions')
    if cfg['routing_accounting'].get('python_path'):
        overlay = Path(cfg['routing_accounting']['python_path']).resolve()
        receipt = json.loads((overlay.parent/'build.json').read_text())
        native_hash = hashlib.sha256((overlay/'dynamo/_core.abi3.so').read_bytes()).hexdigest()
        if (receipt['base_commit'] != 'b83b1d9304ebfc624709ac46db32b1b6f1ff1615'
                or receipt['native_sha256'] != native_hash
                or receipt.get('overlay_recipe_sha256') != hashlib.sha256((ROOT/'patched/weighted_routing/apply_patch.py').read_bytes()).hexdigest()
                or receipt['patch']['lib/kv-router/src/pending_score.rs'] != hashlib.sha256((ROOT/'patched/weighted_routing/pending_score.rs').read_bytes()).hexdigest()):
            raise ValueError('weighted Dynamo build receipt or binary SHA mismatch')
        write(folder/'routing.build.json',receipt)
    from sim.stack.clock import TimeScale
    clock = TimeScale.from_config(cfg)
    from sim.trace_io import load_trace
    rows = load_trace(trace)
    assert all(r["output_tokens"]==1 for r in rows)
    if cfg.get("cache_model") == "first_seen":
        from sim.mooncake import validate_hit_row
        for row in rows:
            validate_hit_row(row,cfg["block_size"])
        assert not cfg["enable_prefix_caching"]
    write(folder/"config.json",cfg)
    (folder/"trace.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    source = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
              for p in [*sorted((ROOT/"sim/stack").glob("*.py")),ROOT/"sim/admission.py",
                        ROOT/"sim/compute_time.py",ROOT/"sim/control_events.py",ROOT/"sim/request_profile.py",ROOT/"sim/storage.py",
                        ROOT/"sim/grouped_storage.py",ROOT/"sim/mooncake.py",ROOT/"sim/sa_model.py",ROOT/"sim/sa_admission.py",
                        ROOT/"sim/priority.py",ROOT/"sim/reference_time.py",
                        *sorted((ROOT/'sim/timing_models').glob('*.py')),
                        ROOT/'sim/timing_models/source.json',
                        ROOT/"scripts/run_tfs_stack.py",ROOT/"scripts/stack_vllm_worker.py",
                        ROOT/"scripts/check_read_admission.py",ROOT/"scripts/check_stack_routing.py",
                        ROOT/"scripts/check_routing_accounting.py",ROOT/"scripts/build_weighted_dynamo.py",
                        *sorted((ROOT/'patched/weighted_routing').glob('*.py')),
                        *sorted((ROOT/'patched/weighted_routing').glob('*.rs')),
                        ROOT/'patched/weighted_routing/source_hashes.json',
                        ROOT/"scripts/run_stack_experiment.sh"]}
    write(folder/"implementation.json",source)
    env = dict(os.environ,PYTHONPATH=str(ROOT),VLLM_PLUGINS="",HF_HUB_OFFLINE="1",
               PYTHONHASHSEED="0",VLLM_ALLOW_LONG_MAX_MODEL_LEN="1",OMP_NUM_THREADS="1",
               MKL_NUM_THREADS="1",OPENBLAS_NUM_THREADS="1",NO_PROXY="*",no_proxy="*",
               DYN_DISCOVERY_BACKEND="file",DYN_FILE_KV=str(folder/"discovery"),DYN_REQUEST_PLANE="tcp",
               SIM_STACK_CONFIG=str(folder/"config.json"),
               SIM_STACK_ADMISSION_STATUS_DIR=str(folder/'admission_rpc'))
    for key in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):
        env.pop(key,None)
        os.environ.pop(key,None)
    for key in ('SIM_STACK_ADMISSION_ENDPOINT','SIM_STACK_ADMISSION_SOCKET','SIM_STACK_ADMISSION_TIMEOUT_S',
                'SIM_STACK_ADMISSION_INSTANCE_ID','SIM_STACK_ADMISSION_LEDGER_EPOCH',
                'SIM_STACK_ROUTING_SCORE_READY','DYN_WAITING_SCORE_FACTOR','DYN_WAITING_SCORE_READY',
                'SIM_STACK_LEASE_EVENTS','DYN_LEASE_LOADING_SLOTS'):
        env.pop(key,None)
    if cfg['routing_accounting']['enabled']:
        env['SIM_STACK_ROUTING_SCORE_READY'] = str(folder/'routing.score.identity.json')
    os.environ["NO_PROXY"]="*"
    roots,handles = [],[]
    metrics_stop = threading.Event()
    metrics_thread = None
    # AF_UNIX 限制路径长度；结果目录较长时也使用独立的短 socket 目录。
    sockets = tempfile.TemporaryDirectory(prefix="dv-stack-")
    storage_socket = Path(sockets.name)/"storage.sock"
    control_socket = Path(sockets.name)/"control.sock"
    rpc_ready = folder/'admission.rpc.json'
    if cfg['admission']['rpc_transport'] == 'dynamo':
        # A fresh endpoint prevents cross-run discovery of another in-memory ledger.
        listener_args = ['--feedback-endpoint',f'dyn://tfs-{uuid.uuid4().hex}.ledger.control']
        env['DYN_REQUEST_PLANE_CODEC'] = 'msgpack'
    elif cfg['admission']['rpc_transport'] == 'tcp':
        listener_args = ['--feedback-endpoint','tcp://127.0.0.1:0']
    else:
        listener_args = ['--feedback-socket',str(control_socket)]
    started = time.perf_counter()
    gc_client = None

    def launch(role,args,extra=None):
        handle = (folder/f"{role}.log").open("w")
        handles.append(handle)
        proc = subprocess.Popen([str(python),*args],cwd=ROOT,env=dict(env,**(extra or {})),
                                stdout=handle,stderr=subprocess.STDOUT,start_new_session=True)
        roots.append((role,proc))

    def wait_for(condition,seconds=100):
        deadline = time.monotonic()+seconds
        while True:
            if any(p.poll() is not None for _,p in roots):
                raise RuntimeError("stack process exited; see per-process logs")
            try:
                if condition():
                    return
            except (OSError,ValueError):
                pass
            if time.monotonic()>deadline:
                raise TimeoutError("stack readiness timed out")
            time.sleep(0.2)

    try:
        launch("storage",["-m","sim.stack.storage_service","--config",str(folder/"config.json"),
                          "--trace",str(folder/"trace.jsonl"),"--socket",str(storage_socket)])
        wait_for(lambda:storage_socket.exists(),40)
        launch("admission",["-m","sim.stack.admission_service","--config",str(folder/"config.json"),
                            "--upstream","http://127.0.0.1:28765","--port","28766",
                            *listener_args,"--feedback-ready-file",str(rpc_ready),
                            "--event-log",str(folder/"admission.events.jsonl")],
               dict(SIM_STACK_STORAGE_SOCKET=str(storage_socket),DYN_SYSTEM_PORT='-1'))
        wait_for(lambda:rpc_ready.exists(),40)
        rpc = json.loads(rpc_ready.read_text())
        assert rpc['transport'] == cfg['admission']['rpc_transport']
        rpc_identity = (dict(SIM_STACK_ADMISSION_INSTANCE_ID=str(rpc['instance_id']),
                             SIM_STACK_ADMISSION_LEDGER_EPOCH=rpc['ledger_epoch'])
                        if rpc['transport'] == 'dynamo' else {})
        readiness = {}
        def frontend_ready(expected):
            nonlocal readiness
            readiness = request_json("http://127.0.0.1:28765/v1/models/sim-opt/ready")
            write(folder/"readiness.latest.json",readiness)
            return frontend_has_workers(readiness,expected)
        for w in range(workers):
            worker_env = dict(SIM_STACK_WORKER_ID=str(w),SIM_STACK_TOKEN_ID="290",DYN_SYSTEM_PORT=str(29111+w),
                     SIM_STACK_STORAGE_SOCKET=str(storage_socket),
                     SIM_STACK_ADMISSION_ENDPOINT=rpc['endpoint'],
                     SIM_STACK_ADMISSION_TIMEOUT_S=str(cfg['admission']['rpc_timeout_wall_s']),
                     SIM_STACK_STEP_LOG=str(folder/f"worker{w}.steps.jsonl"),**rpc_identity)
            if lease_mode:
                worker_env['SIM_STACK_LEASE_EVENTS'] = '1'
            launch(f"worker{w}",["scripts/stack_vllm_worker.py","--model","configs/model",
                "--served-model-name","sim-opt","--endpoint","dyn://dynamo.backend.generate",
                "--dtype","float32","--max-model-len",str(cfg["max_model_len"]),
                "--block-size",str(cfg["block_size"]),"--num-gpu-blocks-override",str(cfg["num_blocks"]),
                "--max-num-seqs",str(cfg["max_num_seqs"]),"--max-num-batched-tokens",str(cfg["max_num_batched_tokens"]),
                "--enforce-eager","--seed","0","--no-enable-prefix-caching",
                "--kv-transfer-config",'{"kv_connector":"SimConnector","kv_role":"kv_both"}'],
                worker_env)
        # Start the frontend after worker registration, so file discovery can
        # read a complete initial snapshot. Some macOS incremental file-watch
        # updates did not reach the frontend in the acceptance preflights.
        wait_for(lambda:all("Registered endpoint 'dynamo.backend.generate'" in
            (folder/f"worker{w}.log").read_text(errors="replace") for w in range(workers)))
        write(folder/"registration.json",dict(expected_workers=workers,
            checked_monotonic_s=time.monotonic(),
            generate_files=sorted(p.name for p in (folder/"discovery/v1/instances").glob("*generate*"))))
        frontend_env = {}
        if cfg['routing_accounting'].get('python_path'):
            frontend_env['PYTHONPATH'] = str(Path(cfg['routing_accounting']['python_path']).resolve())+os.pathsep+str(ROOT)
        if cfg['routing_accounting']['enabled']:
            rc = cfg['routing_accounting']
            frontend_env.update(DYN_WAITING_SCORE_FACTOR=str(rc['waiting_factor']),
                                DYN_WAITING_SCORE_READY=env['SIM_STACK_ROUTING_SCORE_READY'],
                                PYTHONPATH=str(Path(rc['python_path']).resolve())+os.pathsep+str(ROOT))
            if lease_mode:
                frontend_env['DYN_LEASE_LOADING_SLOTS'] = str(cfg['admission']['lease']['loading_slots_per_worker'])
        if cfg["router_mode"] == "kv":
            # Targeted native diagnostics, not a replacement selection algorithm.
            frontend_env["DYN_LOG"] = ("info,dynamo_kv_router::scheduling::selector=debug,"
                                       "dynamo_llm::kv_router::routing_host=debug")
        write(folder/"routing.launch.json",dict(router_mode=cfg["router_mode"],
              argv=frontend_args,environment=frontend_env))
        launch("frontend",frontend_args,frontend_env)
        wait_for(lambda:frontend_ready(workers))
        if cfg['routing_accounting']['enabled']:
            wait_for(lambda:(folder/'routing.score.identity.json').exists(),10)
            identity = json.loads((folder/'routing.score.identity.json').read_text())
            if identity.get('factor') != cfg['routing_accounting']['waiting_factor']:
                raise RuntimeError('native waiting-score capability/config mismatch')
        write(folder/"readiness.json",dict(expected_workers=workers,frontend=readiness,
            checked_monotonic_s=time.monotonic()))
        if cfg["router_mode"] == "kv":
            def sample_metrics():
                with (folder/"routing.metrics.jsonl").open("w",buffering=1) as stream:
                    while not metrics_stop.is_set():
                        try:
                            sample = dict(monotonic_s=time.monotonic(),lines=router_metrics())
                        except (OSError,ValueError) as error:
                            sample = dict(monotonic_s=time.monotonic(),error=str(error))
                        stream.write(json.dumps(sample)+"\n")
                        metrics_stop.wait(1.)
            metrics_thread = threading.Thread(target=sample_metrics,daemon=True)
            metrics_thread.start()
        startup = time.perf_counter()-started
        from replay_http import replay
        from sim.stack.storage_client import StorageClient
        gc_client = StorageClient(str(storage_socket))
        write(folder/'storage.gc.begin.json', gc_client.gc_begin())
        code = asyncio.run(replay(folder/"trace.jsonl","http://127.0.0.1:28766","sim-opt",folder,
                                  False,"completions",admission=True,time_scale=clock.factor))
        assert code==0
        wait_for(lambda:all(r["state"]=="FINISHED" and not r["lease_open"]
                           for r in request_json("http://127.0.0.1:28766/sim/admission")["records"].values()),clock.to_wall(10))
        write(folder/'storage.gc.end.json', gc_client.gc_end())
        gc_client.close()
        gc_client = None
        from sim.stack.admission_rpc_status import collect
        write(folder/'admission_rpc.check.json', collect(folder/'admission_rpc',
              cfg['admission']['rpc_timeout_wall_s']+2,
              expected_workers=workers if cfg['admission']['rpc_transport']=='dynamo'
              and cfg['admission']['placement']=='scheduler' else 0))
        status = request_json("http://127.0.0.1:28766/sim/admission")
        write(folder/"admission.final.json",status)
        from sim.stack.storage_client import StorageClient
        client = StorageClient(str(storage_socket))
        storage = client.stats()
        client.close()
        write(folder/"storage.final.json",storage)
        report = json.loads((folder/"report.json").read_text())
        result = verify(folder,status,report,storage)
        if cfg["router_mode"] == "kv":
            from check_stack_routing import metrics_drained,check as check_routing
            wait_for(lambda:metrics_drained(router_metrics()),5)
            metrics_stop.set()
            metrics_thread.join(3)
            write(folder/"routing.final.json",dict(lines=router_metrics()))
            # All-light short runs may finish between metric samples. Native
            # unequal-cost decisions and final zero gauges remain mandatory.
            write(folder/"routing.check.json",check_routing(folder,require_sampled_load=not cfg['routing_accounting']['enabled']))
        if cfg['routing_accounting']['enabled']:
            def score_drained():
                current = request_json('http://127.0.0.1:28766/sim/admission')
                score = current['routing_score']
                return score['connected'] and score['registered']==score['waiting']==score['active']==0
            wait_for(score_drained,5)
            status = request_json('http://127.0.0.1:28766/sim/admission')
            status = save_admission_snapshot(folder, status)
            write(folder/'routing.score.final.json',status['routing_score'])
            from check_routing_accounting import check as check_weighted_routing
            write(folder/'routing.score.check.json',check_weighted_routing(folder))
        result["router_mode"] = cfg["router_mode"]
        write(folder/"check.json",result)
        write(folder/"processes.json",dict(root_pids={role:p.pid for role,p in roots},startup_wall_s=startup))
        print(json.dumps(dict(case=folder.name,**result)),flush=True)
        return result
    finally:
        if gc_client is not None:
            try:
                write(folder/'storage.gc.cleanup.json', gc_client.gc_end(collect=False))
            except Exception as error:
                write(folder/'storage.gc.cleanup.json', dict(error=repr(error)))
            finally:
                gc_client.close()
        metrics_stop.set()
        if metrics_thread is not None:
            metrics_thread.join(3)
        for _,proc in reversed(roots):
            try:
                os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _,proc in roots:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL)
                proc.wait(timeout=5)
        for handle in handles:
            handle.close()
        sockets.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--trace",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--python",type=Path,default=Path(sys.executable))
    parser.add_argument("--mode",choices=["immediate","tfs","quota_fcfs","slack_only"],default="tfs")
    parser.add_argument("--release-on",choices=["first_token","lease_events"],default="first_token")
    parser.add_argument("--workers",type=int,default=2)
    parser.add_argument("--router-mode",choices=["kv","round-robin"],help="native Dynamo selection; inherit config when omitted")
    parser.add_argument("--kv-mode",choices=["bulk","layer_serial","layer_pipeline"],help="KV loading/execution mode")
    parser.add_argument("--time-scale",type=float,help="override wall_time_scale (>=1)")
    parser.add_argument('--storage-gc-policy', choices=['automatic', 'defer_during_run'],
                        help='default defer_during_run; automatic reproduces legacy GC behavior')
    parser.add_argument('--request-ordering',choices=['fcfs','edf'],
                        help='EDF selects native policy=priority; default inherits config')
    parser.add_argument("--admission-placement", choices=["gateway", "scheduler"],
                        help="scheduler opts into v1 frozen hints and late heavy permits")
    parser.add_argument('--admission-transport',choices=['dynamo','tcp','unix'],
                        help='default native Dynamo TCP/MessagePack; tcp/unix are JSON-lines baselines')
    parser.add_argument('--idle-bypass', action=argparse.BooleanOptionalAction, default=None,
                        help='allow one allocated heavy request on an otherwise idle scheduler')
    parser.add_argument('--waiting-score-factor',type=float,
                        help='enable native weighted accounting: pending-heavy score factor in [0,1]')
    parser.add_argument('--routing-python-path',type=Path,
                        help='isolated patched Dynamo wheel extraction, used by frontend only')
    args = parser.parse_args()
    if os.environ.get("PYTHONHASHSEED") != "0":
        parser.error("set PYTHONHASHSEED=0 for deterministic verification and child-process block hashes")
    cfg = json.loads(args.config.read_text())
    if args.request_ordering is not None:
        cfg.update(request_ordering=args.request_ordering,
                   policy='priority' if args.request_ordering=='edf' else 'fcfs')
    if args.storage_gc_policy is not None:
        cfg.setdefault('storage', {})['gc_policy'] = args.storage_gc_policy
    if args.waiting_score_factor is not None:
        cfg.setdefault('routing_accounting',{}).update(enabled=True,waiting_factor=args.waiting_score_factor)
    if args.routing_python_path is not None:
        cfg.setdefault('routing_accounting',{})['python_path']=str(args.routing_python_path.resolve())
    if args.admission_transport is not None:
        cfg.setdefault('admission',{})['rpc_transport'] = args.admission_transport
    if args.idle_bypass is not None:
        cfg.setdefault('admission',{})['idle_bypass'] = args.idle_bypass
    if args.router_mode is not None:
        cfg["router_mode"] = args.router_mode
    if args.time_scale is not None:
        cfg["wall_time_scale"] = args.time_scale
    if args.kv_mode is not None:
        cfg["kv_transfer_mode"] = args.kv_mode
    if args.admission_placement is not None:
        cfg.setdefault("admission", {})["placement"] = args.admission_placement
        if args.admission_placement == "scheduler":
            cfg["admission"].update(classification="arrival", aging_enabled=False)
    run_one(cfg,args.trace,args.output,args.python,args.mode,args.release_on,args.workers)


if __name__ == "__main__":
    main()
