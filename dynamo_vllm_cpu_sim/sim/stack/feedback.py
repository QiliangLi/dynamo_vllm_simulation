"""Engine feedback and permits over native Dynamo RPC or explicit baselines."""
import atexit
import json
import os
import socket
import threading
import time
import math
import uuid

from sim.stack.admission_rpc import MAX_MESSAGE_BYTES, clock_id, parse_endpoint


class FeedbackClient:
    def __init__(self, path, worker, timeout_s=5, *, instance_id=None, ledger_epoch=None):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('RPC timeout must be finite and positive')
        self.worker = worker
        self.timeout_s = timeout_s
        self.clock_id = clock_id()
        self.outbox = None
        self.layer_buffer_limit = 0
        self.pending_layer_facts = []
        self.pid = os.getpid()
        self.session_id = uuid.uuid4().hex
        self.sequence = 0
        self.attempts = {}
        self.started = set()
        self.closed = False
        self.lock = threading.Lock()
        self.native = None
        self.transport, address = parse_endpoint(path)
        if self.transport == 'dynamo':
            from sim.stack.dynamo_admission_rpc import DynamoAdmissionRPC
            self.native = DynamoAdmissionRPC(address, timeout_s,
                instance_id=instance_id, ledger_epoch=ledger_epoch)
            return
        if self.transport == 'tcp':
            self.sock = socket.create_connection(address, timeout=timeout_s)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout_s)
            try:
                self.sock.connect(address)
            except OSError:
                self.sock.close()
                raise
        self.stream = self.sock.makefile("rb")

    def _exchange(self, row):
        if self.closed:
            raise ConnectionError('admission RPC connection is closed')
        if self.native is not None:
            try:
                return self.native.exchange(row)
            except BaseException:
                self.close()
                raise
        wire = (json.dumps(row, allow_nan=False)+'\n').encode()
        if len(wire) > MAX_MESSAGE_BYTES:
            raise ValueError('admission RPC message exceeds size limit')
        try:
            self.sock.sendall(wire)
            line = self.stream.readline(MAX_MESSAGE_BYTES+1)
            if not line:
                raise ConnectionError('admission service disconnected')
            if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b'\n'):
                raise ConnectionError('invalid admission response frame')
            response = json.loads(line)
            if not isinstance(response, dict) or response.get('ok') is not True:
                raise RuntimeError(f'admission RPC rejected: {response}')
            return response
        except Exception:
            # No reconnect/replay after an ambiguous result: caller fails closed.
            self.close()
            raise

    def enable_layer_buffer(self, limit):
        if self.outbox is not None or type(limit) is not int or limit<1:
            raise ValueError('invalid layer buffer or idle bypass combination')
        self.layer_buffer_limit=limit

    def flush_layers(self):
        with self.lock:
            if not self.pending_layer_facts:
                return
            start=time.monotonic_ns()
            count=len(self.pending_layer_facts)
            self._exchange(dict(op='layer_feedback_flush',rows=self.pending_layer_facts))
            self.pending_layer_facts.clear()
            if os.environ.get('SIM_FINE_TRACE_DIR'):
                from sim.stack.fine_trace import emit
                emit('layer_feedback_flush_rpc',start,count=count)

    def emit(self, kind, rid, payload=None):
        # Synchronous parent/completion/no-load events cannot overtake facts.
        self.flush_layers()
        with self.lock:
            self.sequence += 1
            row = dict(event_id=f"engine:{self.session_id}:{self.sequence}", kind=kind,
                       request_id=rid, worker_id=self.worker,
                       occurred_mono=time.monotonic(), clock_id=clock_id(), payload=payload or {})
            if self.outbox is None:
                return self._exchange(row)
        # Forced request facts follow the force notice on the same wire. They
        # retain their occurrence time even if the receiver is temporarily slow.
        forced = rid in self.outbox.forces
        if forced and self.transport == 'unix':
            row.pop('clock_id')  # Unix peers are on the same monotonic host.
        return self.outbox.send(row, wait=not forced)

    def enable_idle_bypass(self):
        if self.outbox is None:
            from sim.stack.admission_outbox import AdmissionOutbox
            self.outbox = AdmissionOutbox(self)

    def force_admit_async(self, rid, hint, proof):
        return self.outbox.force(rid, hint, proof)

    def submitted(self, rid, byte_count, **metadata):
        attempt = self.attempts.get(rid, 0)+1
        self.attempts[rid] = attempt
        self.emit("load_submitted",rid,dict(attempt_id=attempt,submitted_read_bytes=byte_count,**metadata))

    def completed(self, rid):
        self.emit("load_completed",rid,dict(attempt_id=self.attempts[rid]))

    def control(self, op, rid, **payload):
        """Immediate reply; ambiguous transport errors fail the run closed.

        Any acquire retry must retain acquisition_key. Never invent a second
        key after a timeout: the first operation may already hold a permit.
        """
        self.flush_layers()
        row = dict(op=op, request_id=rid, worker_id=self.worker, **payload)
        if self.outbox is not None:
            return self.outbox.send(row, wait=True)
        with self.lock:
            return self._exchange(row)

    def layer_submitted(self,rid,layer,byte_count):
        self.emit("layer_load_submitted",rid,dict(attempt_id=self.attempts[rid],
                  layer=layer,submitted_read_bytes=byte_count))

    def layer_batch(self, ids, layer, byte_counts):
        # All original per-request facts remain; only the wire frame is shared.
        with self.lock:
            rows=[]
            for rid, size in zip(ids, byte_counts, strict=True):
                self.sequence += 1
                rows.append(dict(event_id=f"engine:{self.session_id}:{self.sequence}",
                    kind="layer_load_submitted", request_id=rid, worker_id=self.worker,
                    occurred_mono=time.monotonic(), clock_id=clock_id(),
                    payload=dict(attempt_id=self.attempts[rid],layer=layer,submitted_read_bytes=size)))
            start=time.monotonic_ns()
            if self.layer_buffer_limit:
                if len(self.pending_layer_facts)+len(rows)>self.layer_buffer_limit:
                    raise RuntimeError('layer telemetry buffer bound exceeded')
                self.pending_layer_facts.extend(rows)
                if os.environ.get('SIM_FINE_TRACE_DIR'):
                    from sim.stack.fine_trace import emit
                    emit('layer_feedback_buffered',start,ids=ids,layer=layer,count=len(rows))
                return dict(ok=True,buffered=True)
            if self.outbox is not None:
                raise RuntimeError("batched-feedback prototype does not support idle-bypass outbox")
            result=self._exchange(dict(op="layer_feedback_batch",rows=rows))
            if os.environ.get("SIM_FINE_TRACE_DIR"):
                from sim.stack.fine_trace import emit
                emit("batch_feedback_rpc",start,ids=ids,layer=layer,count=len(rows))
            return result

    def starting(self, rid):
        if rid not in self.started:
            self.started.add(rid)
            if rid not in self.attempts:
                self.emit("no_external_load",rid)

    def forget(self,rid):
        self.attempts.pop(rid,None)
        self.started.discard(rid)

    def close(self):
        if self.closed:
            return
        if self.outbox is not None:
            self.outbox.close()
        self.closed = True
        if self.native is not None:
            self.native.close()
        else:
            self.stream.close()
            self.sock.close()


_clients = {}


def feedback_client():
    path = os.environ.get("SIM_STACK_ADMISSION_ENDPOINT")
    legacy = os.environ.get("SIM_STACK_ADMISSION_SOCKET")
    if path and legacy:
        raise ValueError('configure only one admission RPC endpoint')
    path = path or legacy
    if not path:
        return None
    key = (os.getpid(),path)
    if key not in _clients:
        _clients[key] = FeedbackClient(path,int(os.environ.get("SIM_STACK_WORKER_ID","0")),
            timeout_s=float(os.environ.get('SIM_STACK_ADMISSION_TIMEOUT_S','5')),
            instance_id=int(os.environ['SIM_STACK_ADMISSION_INSTANCE_ID'])
                if os.environ.get('SIM_STACK_ADMISSION_INSTANCE_ID') else None,
            ledger_epoch=os.environ.get('SIM_STACK_ADMISSION_LEDGER_EPOCH'))
        atexit.register(_clients[key].close)
    if _clients[key].closed:
        raise ConnectionError('admission RPC failed or closed; automatic reconnect is disabled')
    return _clients[key]
