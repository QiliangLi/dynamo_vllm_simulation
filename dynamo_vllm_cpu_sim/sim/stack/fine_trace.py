"""Opt-in, buffered timing probes for the fixed vLLM 0.20.2 experiment.

No profiler, background writer, simulated delay, or upstream file modification.
SIGUSR1 flushes only after replay; every instrumented process registers its PID.
"""
import functools
import inspect
import json
import os
from pathlib import Path
import signal
import textwrap
import threading
import time

_rows = []
_installed = set()
_tls = threading.local()


def emit(event, start_ns=None, **fields):
    end = time.monotonic_ns()
    _rows.append(dict(event=event, start_ns=end if start_ns is None else start_ns,
                      end_ns=end, thread=threading.get_ident(), **fields))


def flush(*_):
    folder = os.environ.get("SIM_FINE_TRACE_DIR")
    if not folder:
        return
    # Replay has finished. IO threads can still append: swap, never clear in place.
    global _rows
    rows, _rows = _rows, []
    with (Path(folder)/f"trace-{os.getpid()}.jsonl").open("a") as stream:
        stream.writelines(json.dumps(r,separators=(",",":"))+"\n" for r in rows)
    (Path(folder)/f"flushed-{os.getpid()}.json").write_text(json.dumps(dict(rows=len(rows))))


def activate(role):
    if "active" in _installed:
        return
    _installed.add("active")
    folder = Path(os.environ["SIM_FINE_TRACE_DIR"])
    folder.mkdir(parents=True,exist_ok=True)
    signal.signal(signal.SIGUSR1,flush)
    (folder/f"ready-{os.getpid()}.json").write_text(json.dumps(dict(
        pid=os.getpid(),role=role,worker=os.environ.get("SIM_STACK_WORKER_ID"),
        clock="monotonic_ns",buffered=True)))


def rid(value):
    return getattr(value,"request_id",getattr(value,"req_id",None))


def rewrite(owner, name, changes):
    """Fail closed if pinned source no longer matches an instrumentation site."""
    original = getattr(owner,name)
    source = textwrap.dedent(inspect.getsource(original))
    for before, after in changes:
        if source.count(before) != 1:
            raise RuntimeError(f"fine trace source mismatch: {owner}.{name}: {before!r}")
        source = source.replace(before,after,1)
    scope = dict(original.__globals__,_ft_emit=emit,_ft_ns=time.monotonic_ns)
    exec(compile(source,f"<fine-trace:{original.__module__}.{name}>","exec"),scope)
    setattr(owner,name,scope[name])


def sync_span(owner,name,event,identity=None):
    original = getattr(owner,name)
    @functools.wraps(original)
    def measured(*args,**kwargs):
        fields = identity(*args,**kwargs) if identity else {}
        start=time.monotonic_ns()
        try:
            return original(*args,**kwargs)
        finally:
            emit(event,start,**fields)
    setattr(owner,name,measured)


def install_vllm():
    if "vllm" in _installed:
        return
    _installed.add("vllm")
    activate("worker_or_core")
    from vllm.v1.engine.async_llm import AsyncLLM
    from vllm.v1.engine.input_processor import InputProcessor
    from vllm.v1.engine import core, core_client
    from vllm.v1.core.sched.scheduler import Scheduler
    from vllm.v1.core.kv_cache_manager import KVCacheManager
    from sim.stack.executor import SimExecutor
    from sim.stack.layerwise import LayerRunner
    import sim.stack.layerwise as layerwise
    install_client()

    original_add = AsyncLLM.add_request
    @functools.wraps(original_add)
    async def add(self,*args,**kwargs):
        request_id = kwargs.get("request_id",args[0] if args else None)
        emit("async_add_enter",rid=request_id)
        result=await original_add(self,*args,**kwargs)
        emit("async_add_exit",rid=request_id)
        return result
    AsyncLLM.add_request=add
    sync_span(InputProcessor,"process_inputs","input_process",
              lambda self,*a,**k:dict(rid=k.get("request_id",a[0] if a else None)))
    rewrite(AsyncLLM,"_add_request",[(
        "self.output_processor.add_request(request, prompt, parent_req, index, queue)",
        "_ft_begin = _ft_ns()\n    self.output_processor.add_request(request, prompt, parent_req, index, queue)\n    _ft_emit('output_register', _ft_begin, rid=request.request_id)")])
    # AsyncMPClient owns these methods; select by definition instead of an alias.
    candidates=[c for c in vars(core_client).values() if inspect.isclass(c)
                and c.__module__==core_client.__name__ and "_send_input" in c.__dict__
                and "add_request_async" in c.__dict__]
    assert len(candidates)==1, candidates
    client=candidates[0]
    rewrite(client,"_send_input",[(
        "message = (request_type.value, *self.encoder.encode(request))",
        "_ft_begin = _ft_ns()\n    message = (request_type.value, *self.encoder.encode(request))\n    if request_type == EngineCoreRequestType.ADD:\n        _ft_emit('ipc_encode', _ft_begin, rid=request.request_id)\n        _ft_emit('ipc_send_ready', rid=request.request_id)")])
    original_send=client.add_request_async
    @functools.wraps(original_send)
    async def send(self,request):
        start=time.monotonic_ns()
        await original_send(self,request)
        emit("ipc_add_send",start,rid=request.request_id)
    client.add_request_async=send

    rewrite(core.EngineCoreProc,"process_input_sockets",[
        ("type_frame, *data_frames = input_socket.recv_multipart(copy=False)",
         "type_frame, *data_frames = input_socket.recv_multipart(copy=False)\n                _ft_received = _ft_ns()"),
        ("req: EngineCoreRequest = add_request_decoder.decode(data_frames)",
         "req: EngineCoreRequest = add_request_decoder.decode(data_frames)\n                    _ft_emit('ipc_decode', _ft_received, rid=req.request_id)"),
        ("self.input_queue.put_nowait((request_type, request))",
         "if request_type == EngineCoreRequestType.ADD:\n                    _ft_emit('core_queue_put', rid=request[0].request_id)\n                self.input_queue.put_nowait((request_type, request))")])
    sync_span(core.EngineCore,"preprocess_add_request","core_preprocess",
              lambda self,request:dict(rid=rid(request)))
    original_handle=core.EngineCoreProc._handle_client_request
    @functools.wraps(original_handle)
    def handle(self,request_type,request):
        if request_type==core.EngineCoreRequestType.ADD:
            emit("core_queue_get",rid=rid(request[0]))
        return original_handle(self,request_type,request)
    core.EngineCoreProc._handle_client_request=handle
    sync_span(Scheduler,"add_request","scheduler_add",lambda self,request:dict(rid=rid(request)))
    original_schedule=Scheduler.schedule
    @functools.wraps(original_schedule)
    def schedule(self):
        start=time.monotonic_ns()
        result=original_schedule(self)
        if result.num_scheduled_tokens:
            emit("schedule",start,ids=list(result.num_scheduled_tokens),
                 tokens=dict(result.num_scheduled_tokens))
        return result
    Scheduler.schedule=schedule
    sync_span(KVCacheManager,"allocate_slots","kv_allocate",
              lambda self,request,*a,**k:dict(rid=rid(request)))
    original_execute=SimExecutor.execute_model
    @functools.wraps(original_execute)
    def execute(self,out,*a,**k):
        ids=list(out.num_scheduled_tokens)
        start=time.monotonic_ns()
        if ids:emit("executor_enter",ids=ids,step=self.steps+1)
        result=original_execute(self,out,*a,**k)
        if ids:emit("executor",start,ids=ids,step=self.steps)
        return result
    SimExecutor.execute_model=execute
    sync_span(Scheduler,"update_from_output","scheduler_update",
              lambda self,out,*a,**k:dict(ids=list(out.num_scheduled_tokens)))
    original_layer=SimExecutor._layer_event
    @functools.wraps(original_layer)
    def layer_event(self,event,**values):
        emit(event,**{k:v for k,v in values.items() if k!="occurred_mono"})
        return original_layer(self,event,**values)
    SimExecutor._layer_event=layer_event
    for name in ("_submit","_wait"):
        sync_span(LayerRunner,name,"layer"+name,
            lambda self,ids,layer,*a,**k:dict(ids=ids,layer=layer))
    original_run=LayerRunner.run
    @functools.wraps(original_run)
    def run(self,ids,timing,step):
        _tls.ids,_tls.step=ids,step
        try:return original_run(self,ids,timing,step)
        finally:_tls.ids=[]
    LayerRunner.run=run
    # A module-local time proxy avoids timing unrelated library sleeps.
    class LayerTime:
        def __getattr__(self,key):return getattr(time,key)
        @staticmethod
        def sleep(seconds):
            start=time.monotonic_ns();time.sleep(seconds)
            emit("layer_sleep",start,requested_s=seconds,ids=getattr(_tls,"ids",[]),
                 step=getattr(_tls,"step",None))
    layerwise.time=LayerTime()
    # Correlate the raw Dynamo context UUID before request suffixes are added.
    from dynamo.vllm.handlers import DecodeWorkerHandler
    original_generate=DecodeWorkerHandler.generate
    @functools.wraps(original_generate)
    async def generate(self,request,context):
        request_id=context.id()
        emit("worker_enter",rid=request_id)
        first=True
        async for item in original_generate(self,request,context):
            if first:emit("worker_first_yield",rid=request_id);first=False
            yield item
    DecodeWorkerHandler.generate=generate


def install_client():
    if "client" in _installed:return
    _installed.add("client")
    from sim.stack.storage_client import StorageClient
    rewrite(StorageClient,"_call",[
        ('self.sock.sendall((json.dumps(payload) + "\\n").encode())',
         '_ft_begin = _ft_ns()\n    _ft_wire = (json.dumps(payload) + "\\n").encode()\n    _ft_encoded = _ft_ns()\n    self.sock.sendall(_ft_wire)\n    _ft_sent = _ft_ns()'),
        ('resp = json.loads(line)',
         '_ft_got = _ft_ns()\n    resp = json.loads(line)\n    _ft_emit("storage_rpc", _ft_begin, op=payload.get("op"), rid=payload.get("rid",payload.get("engine_id")), ids=payload.get("rids",[]), layer=payload.get("layer"), encoded_ns=_ft_encoded, sent_ns=_ft_sent, got_ns=_ft_got)')])


def install_service():
    activate("storage")
    import sim.stack.storage_service as service
    rewrite(service,"handle",[
        ('req = json.loads(line)',
         '_ft_begin = _ft_ns()\n        req = json.loads(line)'),
        ('with service._lock:\n            try:',
         'with service._lock:\n            _ft_locked = _ft_ns()\n            try:'),
        ('conn.sendall((json.dumps(resp) + "\\n").encode())',
         '_ft_processed = _ft_ns()\n        conn.sendall((json.dumps(resp) + "\\n").encode())\n        _ft_emit("storage_service", _ft_begin, op=req.get("op"), rid=req.get("rid",req.get("engine_id")), ids=req.get("rids",[]), layer=req.get("layer"), locked_ns=_ft_locked, processed_ns=_ft_processed)')])


def install_gateway():
    activate("gateway")
    install_client()
    from sim.stack.admission_service import AdmissionState
    original=AdmissionState.__init__
    @functools.wraps(original)
    def init(self,*a,**k):
        original(self,*a,**k)
        emit("gateway_origin",origin_s=self.origin,scale=self.clock.factor)
    AdmissionState.__init__=init


if __name__=="__main__":
    import runpy
    import sys
    role,target,*args=sys.argv[1:]
    if role=="storage":install_service()
    elif role=="gateway":install_gateway()
    else:raise ValueError(role)
    sys.argv=[target,*args]
    runpy.run_path(target,run_name="__main__")
