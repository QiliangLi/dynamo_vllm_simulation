"""Admission controls only; execution observations never use this RPC client."""
import atexit
import json
import os
import socket
import threading
import math

from sim.stack.admission_rpc import MAX_MESSAGE_BYTES, clock_id, parse_endpoint


class FeedbackClient:
    def __init__(self, path, worker, timeout_s=5, *, instance_id=None, ledger_epoch=None):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('RPC timeout must be finite and positive')
        self.worker = worker
        self.timeout_s = timeout_s
        self.clock_id = clock_id()
        self.outbox = None
        self.pid = os.getpid()
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
        self.check()
        if self.native is not None:
            try:
                return self.native.exchange(row)
            except BaseException:
                self.close()
                raise
        return self._exchange_socket(row)

    def check(self):
        if self.closed:
            raise ConnectionError('admission RPC connection is closed')
        if self.native is not None:
            self.native.check()

    def _exchange_socket(self, row):
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

    def enable_idle_bypass(self):
        if self.outbox is None:
            from sim.stack.admission_outbox import AdmissionOutbox
            self.outbox = AdmissionOutbox(self)

    def force_admit_async(self, rid, hint, proof):
        self.check()
        return self.outbox.force(rid, hint, proof)

    def control(self, op, rid, **payload):
        """Immediate reply; ambiguous transport errors fail the run closed.

        Any acquire retry must retain acquisition_key. Never invent a second
        key after a timeout: the first operation may already hold a permit.
        """
        self.check()
        row = dict(op=op, request_id=rid, worker_id=self.worker, **payload)
        if self.outbox is not None:
            return self.outbox.send(row, wait=True)
        with self.lock:
            return self._exchange(row)

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
