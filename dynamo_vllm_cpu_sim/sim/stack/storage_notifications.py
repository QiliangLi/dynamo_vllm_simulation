"""One blocking notification reader and local Futures per storage client."""
from concurrent.futures import Future
import json
import socket
import threading


class LayerNotifications:
    def __init__(self, path, worker):
        self.worker = worker
        self.pending = {}
        self.lock = threading.Lock()
        self.error = None
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30.)
        try:
            self.sock.connect(path)
            self.sock.sendall((json.dumps(dict(op="layer_subscribe", worker=worker)) + "\n").encode())
            self.stream = self.sock.makefile("rb")
            ack = json.loads(self.stream.readline())
            if not ack.get("ok"):
                raise RuntimeError(ack.get("error", "subscription failed"))
            self.token = ack["subscription"]
            self.sock.settimeout(None)
        except BaseException:
            if hasattr(self, "stream"):
                self.stream.close()
            self.sock.close()
            raise
        self.thread = threading.Thread(target=self._receive, name=f"kv-notify-{worker}", daemon=True)
        self.thread.start()

    def prepare(self, rids, layer):
        with self.lock:
            if self.error is not None:
                raise self.error
            for rid in rids:
                key = (rid, layer)
                if key in self.pending:
                    raise ValueError("duplicate layer Future")
            for rid in rids:
                self.pending[(rid, layer)] = Future()

    def result(self, rid, layer):
        with self.lock:
            future = self.pending[(rid, layer)]
        return future.result()

    def _receive(self):
        try:
            for line in self.stream:
                event = json.loads(line)
                if "rid" not in event:
                    raise ConnectionError(event.get("error", "invalid completion notification"))
                key = (event["rid"], event["layer"])
                with self.lock:
                    future = self.pending.get(key)
                    # Canceled/forgotten requests and duplicate notifications
                    # cannot revive an old waiter or complete it twice.
                    if future is None or future.done():
                        continue
                    if event.get("ok"):
                        future.set_result(event)
                    else:
                        future.set_exception(RuntimeError(event["error"]))
            raise ConnectionError("storage completion channel closed")
        except Exception as exc:
            self.fail(exc)

    def fail(self, error):
        with self.lock:
            if self.error is None:
                self.error = error
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.error)

    def forget(self, rid):
        with self.lock:
            for key in [key for key in self.pending if key[0] == rid]:
                future = self.pending.pop(key)
                if not future.done():
                    future.set_exception(RuntimeError("layer wait canceled"))

    def close(self):
        self.fail(ConnectionError("storage client closed"))
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(5)
        self.stream.close()
        self.sock.close()
        if self.thread.is_alive():
            raise RuntimeError("completion receiver did not stop")
