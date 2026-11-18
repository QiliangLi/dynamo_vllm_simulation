"""Bounded retry for idempotent score operations within one ledger epoch."""

import json
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class ScoreClient:
    def __init__(self, identity_path, factor, timeout):
        self.path = Path(identity_path)
        self.factor, self.timeout = factor, timeout
        self.identity = None
        self.sock = self.reader = None
        self.lock = threading.Lock()
        self.pid = os.getpid()
        self.failed = False

    def _connect(self):
        identity = json.loads(self.path.read_text())
        if identity.get("protocol") != "waiting-score-v1" or identity.get("factor") != self.factor:
            raise ValueError("native score protocol or factor mismatch")
        if self.identity is not None and identity["epoch"] != self.identity["epoch"]:
            raise ValueError("score ledger epoch changed; reconciliation requires stopping the run")
        address = urlsplit(identity["endpoint"])
        if address.scheme != "tcp" or address.hostname != "127.0.0.1" or not address.port:
            raise ValueError("score ledger must use loopback TCP")
        self.sock = socket.create_connection((address.hostname, address.port), self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.reader = self.sock.makefile("rb")
        self.identity = identity

    def exchange(self, op, request_id=None, *, allow_closed=False, **fields):
        with self.lock:
            if self.failed or self.pid != os.getpid():
                raise ConnectionError("closed or inherited score client")
            if op not in {"activate", "rollback", "snapshot", "heartbeat", "reset_degraded", "degrade"}:
                raise ValueError("unsupported TFS12 score operation")
            started = time.monotonic_ns()
            try:
                for attempt in range(2):
                    try:
                        if self.sock is None:
                            self._connect()
                        body = dict(op=op, epoch=self.identity["epoch"], **fields)
                        if request_id is not None:
                            body["request_id"] = request_id
                        if allow_closed:
                            body["allow_closed"] = True
                        self.sock.sendall((json.dumps(body, allow_nan=False) + "\n").encode())
                        line = self.reader.readline(128 * 1024 * 1024 + 1)
                        if not line.endswith(b"\n") or len(line) > 128 * 1024 * 1024:
                            raise ConnectionError("invalid score reply frame")
                        result = json.loads(line)
                        if result.get("ok") is not True:
                            raise RuntimeError(f"score operation rejected: {result}")
                        result["already_closed"] = bool(result.get("data", {}).get("already_closed"))
                        result["duration_wall_ns"] = time.monotonic_ns() - started
                        return result
                    except (OSError, ConnectionError):
                        self.close()
                        if attempt:
                            raise
                        # Replay exactly the same operation/permit. The server makes
                        # activate and rollback idempotent. Never change epochs.
            except BaseException:
                self.failed = True
                self.close()
                raise

    def snapshot(self):
        if self.failed:
            return dict(connected=False, failed=True)
        if self.identity is None and not self.path.exists():
            return dict(connected=False)
        return dict(connected=True, **self.exchange("snapshot")["data"])

    def close(self):
        if self.reader is not None:
            self.reader.close()
        if self.sock is not None:
            self.sock.close()
        self.reader = self.sock = None


class AsyncScoreClient:
    """Ordered best-effort score updates; no socket IO on the central loop."""
    def __init__(self, identity_path, factor, timeout):
        import queue
        self.args = identity_path, factor, timeout
        self.queue = queue.Queue(maxsize=4096)
        self.observations = queue.Queue(maxsize=4096)
        self.stop = threading.Event()
        self.reset = threading.Event()
        self.connected = False
        self.thread = threading.Thread(target=self._run, name='tfs-score', daemon=True)
        self.thread.start()

    def _log(self, event, **fields):
        import queue
        try:
            self.observations.put_nowait(dict(event=event, **fields))
        except queue.Full:
            pass

    def exchange(self, op, request_id=None, **fields):
        import queue
        try:
            self.queue.put_nowait((op, request_id, fields))
        except queue.Full:
            self.reset.set()
            self._log('routing_score_degraded', reason='queue_full')
        return dict(queued=True, duration_wall_ns=0)

    def _run(self):
        import queue
        client = None
        heartbeat = 0
        while not self.stop.is_set():
            try:
                if client is None:
                    client = ScoreClient(*self.args)
                    client.exchange('reset_degraded')
                    self.connected = True
                    self._log('routing_score_connected')
                if self.reset.is_set():
                    client.exchange('reset_degraded')
                    self.reset.clear()
                if time.monotonic() - heartbeat >= 1:
                    client.exchange('heartbeat')
                    heartbeat = time.monotonic()
                try:
                    op, rid, fields = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                if op == 'activate':
                    # A one-token response can close its routing booking before
                    # this background operation arrives. That is a valid no-op.
                    fields['allow_closed'] = True
                reply = client.exchange(op, rid, **fields)
                self._log('routing_score_applied', operation=op, request_id=rid,
                          duration_wall_ns=reply['duration_wall_ns'])
            except Exception as exc:
                self.connected = False
                self._log('routing_score_degraded', error=str(exc), fallback='full_original_load')
                if client is not None:
                    client.close()
                    client = None
                # Recovery resets existing discounts; never replay stale score
                # transitions against a new frontend epoch.
                while True:
                    try:
                        self.queue.get_nowait()
                    except queue.Empty:
                        break
                self.stop.wait(1)
        if client is not None:
            client.close()

    def drain_observations(self):
        import queue
        rows = []
        while True:
            try:
                rows.append(self.observations.get_nowait())
            except queue.Empty:
                return rows

    def snapshot(self):
        return dict(connected=self.connected, queued=self.queue.qsize(),
                    fallback='full_original_load_when_heartbeat_expires')

    def close(self):
        self.stop.set()
