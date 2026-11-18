"""Single owner of the storage model; socket threads only enqueue commands.

Queue synchronization and per-client Futures remain. Service._lock is never
acquired in this mode. Accepted commands are acknowledged only after execution.
"""
from concurrent.futures import Future
from dataclasses import dataclass
import math
import queue
import threading
import time

from sim.stack.storage_progress import StorageProgress


@dataclass
class Command:
    operation: object
    name: str
    accepted: float
    reply: Future


class SingleWriterProgress(StorageProgress):
    def __init__(self, service):
        super().__init__(service)
        self.commands = queue.Queue(maxsize=1024)
        self.lifecycle = threading.Lock()
        self.closing = False
        self.command_stats = {}
        self.queue_depth_peak = 0

    def start(self):
        self.thread = threading.Thread(target=self._run, name="storage-owner", daemon=True)
        self.thread.start()

    def check(self):
        super().check()
        if self.closing:
            raise RuntimeError("storage owner closing")

    def call(self, operation, name):
        if threading.current_thread() is self.thread:
            return operation()
        command = Command(operation, name, time.perf_counter(), Future())
        with self.lifecycle:
            self.check()
            if self.thread is None:
                raise RuntimeError("storage owner not started")
            try:
                self.commands.put_nowait(command)
            except queue.Full as exc:
                raise RuntimeError("storage command queue full") from exc
        return command.reply.result()

    def wake(self):
        # A command is already running on the owner. Its next iteration derives
        # the new deadline; there is no other model thread to wake.
        pass

    def _run(self):
        current = None
        try:
            while True:
                started = time.perf_counter()
                nxt = self.service.store.next_time()
                deadline = self.service.t0 + self.service.clock.to_wall(nxt)
                timeout = None if math.isinf(nxt) else max(0., deadline-time.monotonic())
                self.processing_s += time.perf_counter()-started
                if timeout == 0.:
                    started = time.perf_counter()
                    self.service._advance_to_wall()
                    self.processing_s += time.perf_counter()-started
                    self.wakeups += 1
                    continue
                try:
                    current = self.commands.get(timeout=timeout)
                except queue.Empty:
                    # The next loop advances at the due deadline. A new
                    # command wakes Queue.get immediately, without polling.
                    self.wakeups += 1
                    continue
                if current is None:
                    break
                self.queue_depth_peak = max(self.queue_depth_peak, self.commands.qsize()+1)
                begin = time.perf_counter()
                waiting = begin-current.accepted
                row = self.command_stats.setdefault(current.name,
                    dict(count=0, queue_wait_s=0., max_queue_wait_s=0., processing_s=0.))
                row["count"] += 1
                row["queue_wait_s"] += waiting
                row["max_queue_wait_s"] = max(row["max_queue_wait_s"], waiting)
                try:
                    result = current.operation()
                except Exception as exc:
                    current.reply.set_exception(exc)
                else:
                    current.reply.set_result(result)
                finally:
                    row["processing_s"] += time.perf_counter()-begin
                    current = None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            # Mark stopped before draining, atomically with respect to enqueue.
            with self.lifecycle:
                self.stopped = True
            message = "storage service closed" if self.error is None else "storage progress failed: "+self.error
            if current is not None and not current.reply.done():
                current.reply.set_exception(RuntimeError(message))
            while True:
                try:
                    pending = self.commands.get_nowait()
                except queue.Empty:
                    break
                if pending is not None:
                    pending.reply.set_exception(RuntimeError(message))
            self._end_receivers(dict(ok=False, error=message))

    def close(self):
        with self.lifecycle:
            send_stop = not self.closing and not self.stopped
            self.closing = True
        if self.thread is None:
            self.stopped = True
            return
        if send_stop:
            self.commands.put(None, timeout=5.)
        self.thread.join(5.)
        if self.thread.is_alive():
            raise RuntimeError("storage owner did not stop")

    def snapshot(self):
        result = super().snapshot()
        result.update(service_mode="single_writer", queue_depth_peak=self.queue_depth_peak,
                      commands={name:dict(row) for name,row in self.command_stats.items()})
        return result
