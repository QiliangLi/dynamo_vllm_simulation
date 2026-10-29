"""One ordered, acknowledged wire stream; forced requests never wait on its RPC.

No reconnect, transport fallback or ambiguous replay. A background failure is
raised on the next scheduler call. Polling starts only after load completion;
it keeps the per-worker exception occupied until the gateway's first token.
"""
from concurrent.futures import Future
import queue
import threading
import time
import uuid


class AdmissionOutbox:
    def __init__(self, client):
        self.client = client
        self.queue = queue.Queue()
        self.forces = {}
        self.error = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name='admission-outbox', daemon=True)
        self.thread.start()

    def check(self):
        if self.error is not None:
            raise RuntimeError('ordered admission outbox failed') from self.error
        if self.stopping.is_set():
            raise ConnectionError('admission outbox is closed')

    def busy(self):
        self.check()
        return any(not f['released'].is_set() for f in list(self.forces.values()))

    def send(self, row, *, wait):
        self.check()
        result = Future()
        self.queue.put((row, result))
        return result.result(timeout=self.client.timeout_s + 1) if wait else result

    def force(self, rid, hint, proof):
        if self.busy():
            raise RuntimeError('worker already owns a forced admission')
        token = uuid.uuid4().hex
        self.forces[rid] = dict(token=token, released=threading.Event(), completed=False)
        row = dict(op='force_acquire', request_id=rid, worker_id=self.client.worker,
                   acquisition_key=f'{rid}:idle:{token}', permit_id=token,
                   load_attempt_id=1, storage_hint=hint, proof=proof,
                   occurred_mono=time.monotonic(), clock_id=self.client.clock_id)
        self.send(row, wait=False)
        return token, row['occurred_mono']

    def run(self):
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    row, result = self.queue.get(timeout=.002)
                except queue.Empty:
                    if self.stopping.is_set():
                        break
                    for rid, force in list(self.forces.items()):
                        if not force['completed'] or force['released'].is_set():
                            continue
                        response = self.client._exchange(dict(op='force_status',
                            request_id=rid, worker_id=self.client.worker,
                            permit_id=force['token']))
                        if response['released']:
                            force['released'].set()
                    continue
                try:
                    response = self.client._exchange(row)
                    if row.get('op') == 'force_acquire':
                        if not response.get('granted') or response['permit_id'] != row['permit_id']:
                            raise RuntimeError('forced admission was not acknowledged')
                    if row.get('kind') == 'load_completed' and row['request_id'] in self.forces:
                        self.forces[row['request_id']]['completed'] = True
                    result.set_result(response)
                except BaseException as exc:
                    result.set_exception(exc)
                    raise
        except BaseException as exc:
            self.error = exc
            while True:
                try:
                    _, pending = self.queue.get_nowait()
                except queue.Empty:
                    break
                pending.set_exception(exc)

    def close(self):
        self.stopping.set()
        if threading.current_thread() is not self.thread:
            self.thread.join(self.client.timeout_s + 1)
