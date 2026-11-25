"""Durable state of the tuning workflow.

The orchestrator keeps one JSON document (``state.json``), rewritten atomically
after every change, and an append-only ``ledger.jsonl``. Helper processes (the
commands an agent may run) append to the ledger too, so every write takes a
short lock on ``ledger.lock``.
"""
import json
import os
import time
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    """Write ``value`` so a reader never sees a half-written file; keep the previous copy."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        try:
            os.replace(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass
    # On Windows the replace is refused while another process has the file open (a reader, a
    # virus scanner); such locks last moments, so wait for them instead of dropping the write.
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1 * (attempt + 1))


class Lock:
    """A crude cross-process lock: create the file, spin while it exists, always remove ours."""

    def __init__(self, path, timeout=30.0):
        self.path = Path(path)
        self.timeout = timeout

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                # A lock older than a minute belongs to a dead process.
                try:
                    if time.time() - self.path.stat().st_mtime > 60:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError("lock busy: %s" % self.path)
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.ledger_path = self.root / "ledger.jsonl"

    def load(self):
        if not self.state_path.exists():
            return {}
        try:
            return read_json(self.state_path)
        except ValueError:
            return read_json(self.state_path.with_name("state.json.bak"))

    def save(self, state):
        write_json(self.state_path, state)

    def append(self, record):
        """Append one event. Never rewrites or drops earlier lines."""
        record = dict(ts=round(time.time(), 3), **record)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with Lock(self.root / "ledger.lock"):
            with open(self.ledger_path, "a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())

    def events(self, kind=None, reverse=False, limit=None):
        if not self.ledger_path.exists():
            return []
        out = []
        with open(self.ledger_path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if kind is None or record.get("kind") == kind:
                    out.append(record)
        if reverse:
            out.reverse()
        return out[:limit] if limit else out
