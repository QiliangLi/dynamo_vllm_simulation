"""Dynamo 前的请求级准入服务；复用既有 TFS，不访问引擎队列或存储真值。"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
import heapq
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from sim.admission import AdmissionController
from sim.control_events import ControlEvent
from sim.request_profile import build_profile, normalize_config
from sim.stack.clock import TimeScale
from sim.stack.admission_rpc import MAX_MESSAGE_BYTES, clock_id, parse_endpoint, tcp_endpoint

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}")


class AdmissionState:
    def __init__(self, config, log_path=None):
        self.config = normalize_config(config)
        if self.config['admission']['release_on'] != 'first_token':
            raise ValueError('process stack requires release_on=first_token; execution feedback RPCs were removed')
        self.clock = TimeScale.from_config(self.config)
        controller_class = AdmissionController
        if self.config["compute"].get("mode") == "sa_e26f":
            from sim.sa_admission import SAAdmission
            controller_class = SAAdmission
        if self.config["admission"]["classification"] == "arrival":
            from sim.stack.read_admission import FrozenAdmission, FrozenSAAdmission
            controller_class = (FrozenSAAdmission if self.config["compute"].get("mode") == "sa_e26f"
                                else FrozenAdmission)
        self.controller = controller_class(self.config["admission"])
        self.origin = time.monotonic()
        self.aliases = {}
        self.futures = {}
        self.uuid_by_id = {}
        self.tier_pending = {}
        self.tier_snapshots = {}
        self.events = []
        self.delayed = []
        self.sequence = 0
        self.wakeup = asyncio.Event()
        self.fatal = None
        self.rpc_info = None
        self.routing_score = None
        if self.config['routing_accounting']['enabled']:
            from sim.stack.routing_accounting import ScoreClient
            rc = self.config['routing_accounting']
            self.routing_score = ScoreClient(os.environ['SIM_STACK_ROUTING_SCORE_READY'],
                                             rc['waiting_factor'],rc['rpc_timeout_wall_s'])
        self.log = Path(log_path).open("w",buffering=1) if log_path else None

    def now(self):
        return self.clock.to_model(time.monotonic()-self.origin)

    def log_event(self, event, **fields):
        row = dict(t=self.now(),event=event,**fields)
        self.events.append(row)
        if self.log:
            self.log.write(json.dumps(row)+"\n")

    def drain(self):
        for row in self.controller.logs:
            self.events.append(row)
            if self.log:
                self.log.write(json.dumps(row)+"\n")
        self.controller.logs.clear()

    def arrive(self, row):
        now = self.now()
        row = dict(row,arrival_s=now)
        from sim.stack.tier_profile import enabled as tier_enabled
        if not tier_enabled(self.config):
            profile = build_profile(row,self.config)
            self.controller.on_arrivals([profile],now)
            self.drain()
        identity = uuid.uuid4()
        self.aliases[identity.hex] = row["id"]
        self.uuid_by_id[row["id"]] = str(identity)
        self.futures[row["id"]] = asyncio.get_running_loop().create_future()
        self.log_event("request_correlated",id=row["id"],engine_uuid=str(identity))
        if tier_enabled(self.config):
            self.tier_pending[row['id']] = dict(arrival_s=now,
                                               prompt_tokens=len(row['prompt_token_ids']))
            self.futures[row['id']].set_result(str(identity))
            self.log_event('tier_profile_pending', id=row['id'])
        self.wakeup.set()
        return self.futures[row["id"]]

    def resolve_tier_profile(self, snapshot):
        from sim.stack.tier_profile import build_tier_profile, enabled, validate_snapshot
        if not enabled(self.config):
            raise ValueError('Dynamo tier profile mode is disabled')
        snapshot = validate_snapshot(snapshot, self.config)
        match = UUID_RE.search(snapshot['request_id'])
        key = uuid.UUID(match.group()).hex if match else None
        rid = self.aliases.get(key)
        if rid is None or rid not in self.tier_pending:
            raise ValueError('unregistered tier-profile request')
        row = self.tier_pending[rid]
        if snapshot['prompt_tokens'] != row['prompt_tokens']:
            raise ValueError('Dynamo and ingress token counts differ')
        if rid in self.tier_snapshots:
            if self.tier_snapshots[rid] != snapshot:
                raise ValueError('tier profile changed after classification; retry/reconciliation is unsupported')
        else:
            profile = build_tier_profile(snapshot, self.config, rid, row['arrival_s'])
            self.controller.on_arrivals([profile], self.now())
            self.controller.dispatch_unreserved(rid, self.now())
            self.tier_snapshots[rid] = snapshot
            self.log_event('dynamo_tier_profile_resolved', id=rid, snapshot=snapshot,
                           estimated_remote_tokens=profile.estimated_remote_tokens,
                           cached_prefix_tokens=min(snapshot['disk_blocks'],
                               (profile.prompt_tokens - 1)//snapshot['block_size'])*snapshot['block_size'])
            self.drain()
        from sim.stack.storage_hint import attach_http_hint
        body = attach_http_hint({}, self.controller.hints[rid])
        from sim.stack.edf_priority import enabled as edf_enabled, attach_http
        if edf_enabled(self.config):
            body, _ = attach_http(body, self.controller.records[rid].profile,
                                  self.origin, self.clock.factor)
        self.log_event('storage_hint_forwarded', id=rid, hint=self.controller.hints[rid],
                       engine_uuid=self.uuid_by_id[rid], source='dynamo_tier')
        return dict(annotations=body['nvext']['annotations'],
                    is_heavy_read=self.controller.hints[rid]['is_heavy_read'])

    def receive(self, raw, *, transport="unix"):
        raise ValueError('execution feedback RPCs were removed; only admission controls are accepted')

    def control(self, raw, *, transport='unix'):
        """Atomic ledger mutation: no await between checking and debiting."""
        if self.fatal:
            raise RuntimeError(self.fatal)
        if self.config["admission"]["placement"] != "scheduler":
            raise ValueError("scheduler admission is disabled")
        match = UUID_RE.search(raw["request_id"])
        key = uuid.UUID(match.group()).hex if match else None
        if key not in self.aliases:
            raise ValueError("unregistered engine request ID")
        if type(raw["worker_id"]) is not int or not 0 <= raw["worker_id"] < self.config["workers"]:
            raise ValueError("invalid worker ID")
        rid = self.aliases[key]
        self.deliver()
        now = self.now()
        if raw["op"] in ('acquire', 'force_acquire'):
            from sim.stack.storage_hint import validate_hint
            forced = raw['op'] == 'force_acquire'
            force_fields = {}
            if forced:
                if not self.config['admission']['idle_bypass']:
                    raise ValueError('idle bypass is disabled')
                if transport != 'unix' and (clock_id() is None or raw.get('clock_id') != clock_id()):
                    raise ValueError('idle bypass requires Unix or a shared monotonic clock')
                issued = raw['occurred_mono']
                if type(issued) not in (int, float) or not math.isfinite(issued):
                    raise ValueError('invalid force decision time')
                force_fields = dict(force=True, permit_id=raw['permit_id'], proof=raw['proof'],
                                    decision_s=self.clock.to_model(issued-self.origin))
            result = self.controller.acquire(rid, raw["worker_id"], raw["acquisition_key"],
                raw["load_attempt_id"], validate_hint(raw["storage_hint"]), now, **force_fields)
            if result['granted'] and self.routing_score is not None:
                kwargs = dict(allow_closed=True) if forced else {}
                reply = self.routing_score.exchange('activate',raw['request_id'],permit_id=result['permit_id'], **kwargs)
                self.log_event('routing_score_force_already_closed' if reply.get('already_closed') else 'routing_score_activated',
                               id=rid,permit_id=result['permit_id'],
                               duration_wall_ns=reply['duration_wall_ns'])
        elif raw["op"] == "rollback":
            self.controller.rollback(rid, raw["worker_id"], raw["permit_id"], now)
            if self.routing_score is not None:
                reply = self.routing_score.exchange('rollback',raw['request_id'],permit_id=raw['permit_id'])
                self.log_event('routing_score_rolled_back',id=rid,permit_id=raw['permit_id'],
                               duration_wall_ns=reply['duration_wall_ns'])
            result = dict(released=True)
        elif raw['op'] == 'routing_ready' and self.routing_score is not None:
            record = self.controller.records[rid]
            if (not self.controller.hints[rid]['is_heavy_read'] or record.lease_open
                    or record.worker_id not in (-1,raw['worker_id'])):
                raise ValueError('no-external-load transition conflicts with request ownership or permit')
            reply = self.routing_score.exchange('activate',raw['request_id'],permit_id='no-external-load')
            self.log_event('routing_score_no_external_load',id=rid,duration_wall_ns=reply['duration_wall_ns'])
            result = dict(ready=True)
        else:
            raise ValueError("unknown admission operation")
        if raw['op'] == 'force_acquire' and result['granted']:
            rec = self.controller.records[rid]
            # The HTTP stream can overtake the asynchronous force notice. Store
            # HTTP facts immediately, then release a late-registered permit.
            kind = 'first_token' if rec.first_token_s is not None else 'request_finished'
            occurred = rec.first_token_s if rec.first_token_s is not None else rec.finish_s
            if occurred is not None and rec.lease_open:
                event = ControlEvent(f"reconcile:{result['permit_id']}", kind, rid,
                                     rec.worker_id, occurred, self.now())
                self.queue_event(event)
        self.drain()
        return dict(ok=True, **result)

    def queue_event(self, event, engine_id=None, **clock_fields):
        self.sequence += 1
        heapq.heappush(self.delayed,(event.visible_s,self.sequence,event))
        self.log_event("feedback_received",id=event.request_id,kind=event.kind,
                       event_id=event.event_id,worker=event.worker_id,
                       occurred_s=event.occurred_s,visible_s=event.visible_s,
                       payload=event.payload,engine_id=engine_id,**clock_fields)
        self.deliver()
        self.wakeup.set()

    def local_event(self, kind, rid):
        if kind not in ('first_token', 'request_finished'):
            raise ValueError('only HTTP token/completion observations belong in the online ledger')
        now = self.now()
        worker = self.controller.records[rid].worker_id
        self.sequence += 1
        event = ControlEvent(f"gateway:{self.sequence}",kind,rid,worker,now,
                             now+self.config["admission"]["feedback_delay_s"])
        self.queue_event(event)

    def deliver(self):
        now = self.now()
        while self.delayed and self.delayed[0][0] <= now:
            _,_,event = heapq.heappop(self.delayed)
            event = replace(event, worker_id=self.controller.records[event.request_id].worker_id)
            self.controller.on_event(event,now)
            self.log_event("feedback_delivered",id=event.request_id,kind=event.kind,
                           event_id=event.event_id,occurred_s=event.occurred_s,
                           visible_s=event.visible_s,worker=event.worker_id)
        self.drain()

    def admit(self):
        self.deliver()
        if self.fatal:
            return
        now = self.now()
        if self.config["admission"]["placement"] == "scheduler":
            for rid in sorted(self.controller.pending,
                              key=lambda r: (self.controller.records[r].profile.arrival_s, r)):
                self.controller.dispatch_unreserved(rid, now)
                self.futures[rid].set_result(self.uuid_by_id[rid])
            self.drain()
            return
        while (decision := self.controller.pick(now)) is not None:
            rid = decision.request_id
            self.controller.reserve(decision,now)
            self.controller.mark_submitted(rid,-1,now)
            self.futures[rid].set_result(self.uuid_by_id[rid])
            self.drain()

    def cancel_queued(self,rid):
        if rid not in self.controller.records:
            return False
        record = self.controller.records[rid]
        if record.state != "QUEUED":
            return False  # 已放行的请求必须继续执行/排空，不能在此伪造完成。
        self.controller.pending.remove(rid)
        record.state = "CANCELLED"
        self.futures[rid].cancel()
        self.log_event("queued_request_cancelled",id=rid)
        self.wakeup.set()
        return True

    async def pump(self):
        while True:
            self.wakeup.clear()
            self.admit()
            now = self.now()
            wake = self.controller.next_wakeup(now)
            # Heartbeat and minimum wait are implementation polling intervals
            # in wall seconds; aging and delayed feedback are model deadlines.
            targets = [now+self.clock.to_model(0.25)]
            if wake is not None:
                targets.append(wake)
            if self.delayed:
                targets.append(self.delayed[0][0])
            try:
                await asyncio.wait_for(self.wakeup.wait(),max(0.001,self.clock.to_wall(min(targets)-now)))
            except asyncio.TimeoutError:
                pass

    def snapshot(self):
        self.deliver()
        return dict(status="failed" if self.fatal else "ready",fatal=self.fatal,
                    execution_feedback='disabled', origin_monotonic_s=self.origin,
                    rpc=self.rpc_info,
                    routing_score=self.routing_score.snapshot() if self.routing_score is not None else None,
                    permits=getattr(self.controller,"permits",{}),
                    storage_hints=getattr(self.controller,"hints",{}),
                    wall_time_scale=self.clock.factor,
                    snapshot=asdict(self.controller.snapshot(self.now())),
                    records={rid:asdict(r) for rid,r in self.controller.records.items()},
                    delayed_events=len(self.delayed),invalid_reasons=sorted(self.controller.invalid_reasons),
                    events=self.events)


def create_app(config, upstream, feedback_socket=None, event_log=None, *,
               feedback_endpoint=None, feedback_ready_file=None):
    state = AdmissionState(config,event_log)
    if feedback_socket and feedback_endpoint:
        raise ValueError('configure one admission listener')
    endpoint = feedback_endpoint or feedback_socket or (
        'dyn://tfs.ledger.control' if state.config['admission']['rpc_transport'] == 'dynamo'
        else 'tcp://127.0.0.1:28767')
    transport, address = parse_endpoint(endpoint, server=True)
    connections = set()

    def process(raw):
        try:
            if raw.get('op') == 'ping':
                response = dict(ok=True, service='admission', protocol=1)
            elif raw.get('op'):
                response = state.control(raw, transport=transport)
            else:
                state.receive(raw, transport=transport)
                response = dict(ok=True)
        except Exception as exc:
            state.fatal = f'feedback: {type(exc).__name__}: {exc}'
            state.log_event('feedback_error', error=state.fatal)
            response = dict(ok=False, error=state.fatal)
        return dict(response, rpc_transport=transport)

    async def feedback(reader,writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            while line := await reader.readline():
                try:
                    if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b'\n'):
                        raise ValueError('invalid admission request frame')
                    raw = json.loads(line)
                    response = process(raw)
                except Exception as exc:
                    state.fatal = f"feedback: {type(exc).__name__}: {exc}"
                    state.log_event("feedback_error",error=state.fatal)
                    response = dict(ok=False,error=state.fatal)
                response['rpc_transport'] = transport
                writer.write((json.dumps(response)+"\n").encode())
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass
            connections.discard(task)

    @asynccontextmanager
    async def lifespan(app):
        path = Path(address) if transport == 'unix' else None
        native = None
        server = None
        if transport == 'dynamo':
            from sim.stack.dynamo_admission_rpc import DynamoAdmissionServer
            native = DynamoAdmissionServer(address, process, state.config['admission']['rpc_timeout_wall_s'])
            state.rpc_info = await native.start()
            state.rpc_info['clock_id'] = clock_id()
        elif path is not None:
            if path.exists():
                raise RuntimeError("feedback socket already exists; use a new run directory")
            server = await asyncio.start_unix_server(feedback,path=str(path),limit=MAX_MESSAGE_BYTES)
            bound = 'unix://'+str(path)
        else:
            server = await asyncio.start_server(feedback,*address,limit=MAX_MESSAGE_BYTES)
            host, port = server.sockets[0].getsockname()[:2]
            bound = tcp_endpoint(host,port)
        if native is None:
            state.rpc_info = dict(endpoint=bound,transport=transport,protocol='json-lines-v1',
                                  clock_id=clock_id(),persistent=True,tcp_nodelay=transport=='tcp')
        app.state.client = httpx.AsyncClient(timeout=state.clock.to_wall(120),trust_env=False)
        app.state.producers = set()
        pump = asyncio.create_task(state.pump())
        try:
            if feedback_ready_file:
                ready = Path(feedback_ready_file)
                temporary = ready.with_suffix(ready.suffix+'.tmp')
                temporary.write_text(json.dumps(state.rpc_info)+'\n')
                temporary.replace(ready)
            yield
        finally:
            pump.cancel()
            await asyncio.gather(pump,return_exceptions=True)
            for task in app.state.producers:
                task.cancel()
            await asyncio.gather(*app.state.producers,return_exceptions=True)
            await app.state.client.aclose()
            if state.routing_score is not None:
                state.routing_score.close()
            if native is not None:
                await native.close()
            if server is not None:
                server.close()
                await server.wait_closed()
            tasks = list(connections)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            if path is not None:
                path.unlink(missing_ok=True)
            if state.log:
                state.log.close()

    app = FastAPI(lifespan=lifespan)
    app.state.admission = state

    @app.get("/sim/admission")
    async def status():
        return state.snapshot()

    @app.get("/v1/models")
    async def models():
        response = await app.state.client.get(upstream+"/v1/models")
        return response.json()

    @app.post('/internal/dynamo-tier-profile')
    async def tier_profile(request: Request):
        import hmac
        secret = os.environ.get('SIM_STACK_TIER_PROFILE_TOKEN', '')
        if not secret or not hmac.compare_digest(
                request.headers.get('x-tier-profile-token', ''), secret):
            raise HTTPException(403, 'invalid internal tier-profile identity')
        if state.fatal:
            raise HTTPException(503, state.fatal)
        try:
            return state.resolve_tier_profile(await request.json())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/completions")
    async def completion(request: Request):
        if state.fatal:
            raise HTTPException(503,state.fatal)
        body = await request.json()
        from sim.stack.tier_profile import enabled as tier_enabled
        tier_mode = tier_enabled(state.config)
        meta = body.pop("_sim_admission",{})
        if not isinstance(meta, dict):
            raise HTTPException(400, 'invalid simulation metadata')
        if tier_mode and set(meta) - {'id'}:
            raise HTTPException(400, 'Dynamo tier mode does not accept client hit or timing metadata')
        rid = meta.get("id", uuid.uuid4().hex if tier_mode else None)
        if not isinstance(rid,str) or not rid or rid in state.uuid_by_id:
            raise HTTPException(400,"unique _sim_admission.id is required")
        if body.get("max_tokens") != 1 or body.get("stream") is not True or not isinstance(body.get("prompt"),str):
            raise HTTPException(400,"this prefill gateway requires text prompt, stream=true, max_tokens=1")
        tokens = app.state.tokenizer.encode(body["prompt"])
        remote = meta.get("estimated_remote_tokens",0)
        if (not isinstance(remote,int) or remote<0 or remote>len(tokens)
                or remote%state.config["block_size"]):
            raise HTTPException(400,"invalid estimated_remote_tokens")
        if len(tokens)+1>state.config["max_model_len"] or not tokens:
            raise HTTPException(400,"invalid prompt length")
        if state.config["admission"]["placement"] == "scheduler":
            from sim.stack.storage_hint import validate_http_metadata
            try:
                validate_http_metadata(body)
            except ValueError as exc:
                raise HTTPException(400,str(exc)) from exc
        if tier_mode and any(a.startswith(('edf_priority_v1:', 'budget_priority_v1:'))
                             for a in (body.get('nvext') or {}).get('annotations', [])):
            raise HTTPException(400, 'caller cannot supply EDF metadata')
        future = state.arrive(dict(id=rid,prompt_token_ids=tokens,output_tokens=1,
                                   remote_prefix_tokens=remote,class_id=meta.get("class_id","unclassified")))
        try:
            while not future.done():
                if state.fatal:
                    raise HTTPException(503,state.fatal)
                if await request.is_disconnected():
                    if state.cancel_queued(rid):
                        raise HTTPException(499,"client disconnected while queued")
                    break  # 检查连接期间已放行：仍启动独立 producer 排空上游。
                await asyncio.sleep(0.002)
            engine_uuid = future.result()
            if state.config["admission"]["placement"] == "scheduler" and not tier_mode:
                from sim.stack.storage_hint import attach_http_hint
                hint = state.controller.hints[rid]
                body = attach_http_hint(body, hint)
                state.log_event("storage_hint_forwarded", id=rid, hint=hint,
                                engine_uuid=engine_uuid)
            from sim.stack.edf_priority import enabled as edf_priority_enabled
            if edf_priority_enabled(state.config) and not tier_mode:
                from sim.stack.edf_priority import attach_http as attach_edf_priority
                body, priority_metadata = attach_edf_priority(
                    body, state.controller.records[rid].profile,
                    state.origin, state.clock.factor)
                state.log_event("edf_priority_forwarded", id=rid,
                                engine_uuid=engine_uuid, metadata=priority_metadata)
        except asyncio.CancelledError:
            if not future.done():
                state.cancel_queued(rid)
            else:
                # 尚未发往上游，可以确认不存在设备在途工作。
                if rid in state.controller.records:
                    state.local_event("request_finished",rid)
            raise
        queue = asyncio.Queue()

        async def produce():
            first = done = False
            try:
                if state.config.get("cache_model") == "first_seen":
                    # 只绑定固定输入；不查询存储负载。必须先完成绑定再转发 HTTP。
                    def bind_hit():
                        from sim.stack.storage_client import StorageClient
                        client = StorageClient(os.environ["SIM_STACK_STORAGE_SOCKET"])
                        try:
                            client.bind(engine_uuid,rid,tokens,remote)
                        finally:
                            client.close()
                    await asyncio.to_thread(bind_hit)
                    state.log_event("hit_contract_bound",id=rid,engine_uuid=engine_uuid,hit_tokens=remote)
                if not tier_mode and state.routing_score is not None and state.controller.hints[rid]['is_heavy_read']:
                    reply = await asyncio.to_thread(state.routing_score.exchange,'register',engine_uuid,
                                                    prompt_tokens=len(tokens))
                    state.log_event('routing_score_registered',id=rid,engine_uuid=engine_uuid,
                                    duration_wall_ns=reply['duration_wall_ns'])
                state.log_event("http_forwarded",id=rid,engine_uuid=engine_uuid)
                async with app.state.client.stream("POST",upstream+"/v1/completions",json=body,
                         headers={"x-dynamo-request-id":engine_uuid}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        await queue.put((line+"\n").encode())
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip()=="[DONE]":
                            done = True
                            continue
                        chunk = json.loads(data)
                        for choice in chunk.get("choices",[]):
                            content = choice.get("text") or choice.get("delta",{}).get("content")
                            if content and not first:
                                first = True
                                state.local_event("first_token",rid)
                            done = done or bool(choice.get("finish_reason"))
                if not first or not done:
                    raise RuntimeError("upstream ended without content token or completion marker")
                state.local_event("request_finished",rid)
            except Exception as exc:
                # 不能确定上游是否仍在执行。保留租约并停止新准入，禁止伪造完成。
                state.fatal = f"upstream: {type(exc).__name__}: {exc}"
                state.log_event("upstream_uncertain",id=rid,error=state.fatal)
                await queue.put(("data: "+json.dumps({"error":state.fatal})+"\n\n").encode())
                state.wakeup.set()
            finally:
                await queue.put(None)

        task = asyncio.create_task(produce())
        app.state.producers.add(task)
        task.add_done_callback(app.state.producers.discard)

        async def stream():
            # 客户端断开只取消此迭代器；produce 继续排空实际上游响应并回写事实。
            while (chunk := await queue.get()) is not None:
                yield chunk

        return StreamingResponse(stream(),media_type="text/event-stream")

    return app


def main():
    import uvicorn
    from transformers import AutoTokenizer
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",required=True,type=Path)
    parser.add_argument("--upstream",default="http://127.0.0.1:28765")
    parser.add_argument("--port",type=int,default=28766)
    listener = parser.add_mutually_exclusive_group()
    listener.add_argument("--feedback-endpoint",help="dyn://namespace.component.endpoint (default), or legacy tcp://IP:port")
    listener.add_argument("--feedback-socket",help="legacy Unix socket baseline")
    parser.add_argument("--feedback-ready-file",type=Path,help="write actual listening endpoint after startup")
    parser.add_argument("--event-log",required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    app = create_app(config,args.upstream,args.feedback_socket,args.event_log,
                     feedback_endpoint=args.feedback_endpoint,feedback_ready_file=args.feedback_ready_file)
    root = Path(__file__).resolve().parents[2]
    app.state.tokenizer = AutoTokenizer.from_pretrained(str(root/"configs/model"))
    uvicorn.run(app,host="127.0.0.1",port=args.port,log_level="warning")


if __name__ == "__main__":
    main()
