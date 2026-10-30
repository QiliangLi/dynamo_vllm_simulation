"""Ordered admission controls; forced requests never wait on their registration.

No reconnect, transport fallback or ambiguous replay. A background failure is
raised on the next scheduler call. Each forced request is registered separately;
neither registration nor gateway release of an earlier request gates the next
local idle decision. Gateway first tokens release the central permits.
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
        self.error = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name='admission-outbox', daemon=True)
        self.thread.start()

    def check(self):
        if self.error is not None:
            raise RuntimeError('ordered admission outbox failed') from self.error
        if self.stopping.is_set():
            raise ConnectionError('admission outbox is closed')

    def send(self, row, *, wait):
        self.check()
        result = Future()
        self.queue.put((row, result))
        return result.result(timeout=self.client.timeout_s + 1) if wait else result

    def force(self, rid, hint, proof):
        self.check()
        token = uuid.uuid4().hex
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
                    continue
                try:
                    response = self.client._exchange(row)
                    if row.get('op') == 'force_acquire':
                        if not response.get('granted') or response['permit_id'] != row['permit_id']:
                            raise RuntimeError('forced admission was not acknowledged')
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
