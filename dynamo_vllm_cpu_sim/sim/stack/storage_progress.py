"""Deadline-driven storage progress and per-worker completion mailboxes.

All model access uses Service._lock. Socket writers consume mailboxes outside
that lock, so a slow receiver cannot hold the storage model lock.
"""
import math
import queue
import threading
import time
import uuid


def completion_mode(config):
    mode = config.get("storage", {}).get("completion_mode", "poll")
    if mode not in ("poll", "notify"):
        raise ValueError("storage.completion_mode must be poll or notify")
    return mode


class StorageProgress:
    def __init__(self, service):
        self.service = service
        self.changed = threading.Condition(service._lock)
        self.subscribers = {}
        self.stopped = False
        self.error = None
        self.thread = None
        self.wakeups = 0
        self.processing_s = 0.
        self.completions = 0

    def start(self):
        self.thread = threading.Thread(target=self._run, name="storage-progress", daemon=True)
        self.thread.start()

    def subscribe(self, worker):
        self.check()
        if type(worker) is not int or worker < 0 or worker in self.subscribers:
            raise ValueError("invalid or duplicate completion subscriber")
        token, mailbox = uuid.uuid4().hex, queue.Queue(maxsize=1024)
        self.subscribers[worker] = (token, mailbox)
        return token, mailbox

    def require(self, worker, token):
        self.check()
        if worker not in self.subscribers or self.subscribers[worker][0] != token:
            raise ValueError("missing or stale completion subscription")

    def unsubscribe(self, worker, token):
        if self.subscribers.get(worker, (None,))[0] == token:
            del self.subscribers[worker]

    def publish(self, key, completed_s, error=None):
        worker, rid, layer = key
        if worker not in self.subscribers:
            raise RuntimeError("completion receiver disconnected")
        event = dict(ok=error is None, rid=rid, layer=layer, completed_s=completed_s,
                     published_mono=time.monotonic())
        if error is not None:
            event["error"] = error
        try:
            self.subscribers[worker][1].put_nowait(event)
        except queue.Full as exc:
            raise RuntimeError("completion mailbox overflow") from exc
        self.completions += 1

    def check(self):
        if self.error is not None:
            raise RuntimeError("storage progress failed: " + self.error)
        if self.stopped:
            raise RuntimeError("storage progress stopped")

    def wake(self):
        self.changed.notify()

    def _run(self):
        with self.changed:
            try:
                while not self.stopped:
                    started = time.perf_counter()
                    self.service._advance_to_wall()
                    nxt = self.service.store.next_time()
                    self.processing_s += time.perf_counter() - started
                    deadline = self.service.t0 + self.service.clock.to_wall(nxt)
                    timeout = None if math.isinf(nxt) else max(0., deadline - time.monotonic())
                    self.changed.wait(timeout)
                    self.wakeups += 1
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self._end_receivers(dict(ok=False, error="storage progress failed: " + self.error))

    def _end_receivers(self, event):
        for _, mailbox in self.subscribers.values():
            # Shutdown/error must never wait for a slow socket writer.
            while True:
                try:
                    mailbox.put_nowait(event)
                    break
                except queue.Full:
                    mailbox.get_nowait()

    def close(self):
        with self.changed:
            self.stopped = True
            self._end_receivers(dict(ok=False, error="storage service closed"))
            self.changed.notify_all()
        if self.thread is not None:
            self.thread.join(5)
            if self.thread.is_alive():
                raise RuntimeError("storage progress thread did not stop")

    def snapshot(self):
        return dict(mode="notify", wakeups=self.wakeups, processing_s=self.processing_s,
                    completions=self.completions, error=self.error,
                    subscribers=len(self.subscribers))
