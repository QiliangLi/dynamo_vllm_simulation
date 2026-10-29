"""Bound automatic GC deferral to an explicitly controlled benchmark window."""
import gc
import math
import os
from pathlib import Path
import resource
import sys
import threading
import time


def rss_bytes():
    if sys.platform.startswith('linux'):
        pages = int(Path('/proc/self/statm').read_text().split()[1])
        return pages * os.sysconf('SC_PAGE_SIZE')
    # macOS exposes peak RSS in bytes; peak is a conservative memory guard.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == 'darwin' else peak * 1024)


class StorageGcWindow:
    def __init__(self, config):
        self.policy = config.get('gc_policy', 'automatic')
        if self.policy not in ('automatic', 'defer_during_run'):
            raise ValueError('storage.gc_policy must be automatic or defer_during_run')
        self.max_rss = config.get('gc_max_rss_bytes', 4 * 2**30)
        self.max_wall_s = config.get('gc_max_window_s', 600.)
        if (type(self.max_rss) is not int or self.max_rss <= 0
                or type(self.max_wall_s) not in (int, float)
                or not math.isfinite(self.max_wall_s) or self.max_wall_s <= 0):
            raise ValueError('storage GC guards must be finite positive limits')
        self.active = False
        self.failure = None
        self.events = []
        self.peak_rss = 0
        self.samples = 0
        self.previous_enabled = None
        self.stop = threading.Event()
        self.monitor = None
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return dict(policy=self.policy, active=self.active, failure=self.failure,
                enabled=gc.isenabled(), max_rss_bytes=self.max_rss,
                max_window_s=self.max_wall_s, peak_rss_bytes=self.peak_rss,
                memory_samples=self.samples, events=list(self.events),
                rss_kind='current' if sys.platform.startswith('linux') else 'peak')

    def check(self):
        if self.failure:
            raise RuntimeError('storage GC guard: '+self.failure)

    def _restore(self):
        if self.previous_enabled:
            gc.enable()
        else:
            gc.disable()

    def sample(self):
        """A guard failure invalidates the window and restores the prior GC mode."""
        with self.lock:
            if not self.active:
                return
            try:
                rss = rss_bytes()
                self.peak_rss = max(self.peak_rss, rss)
                self.samples += 1
                reason = ('RSS limit exceeded' if rss > self.max_rss else
                          'window deadline exceeded' if time.monotonic()-self.started > self.max_wall_s
                          else None)
            except Exception as exc:
                reason = 'memory monitor failed: '+repr(exc)
            if reason:
                self.failure = reason
                self.active = False
                self._restore()
                self.events.append(dict(event='guard_failed', t=time.monotonic(),
                                        reason=reason, enabled_after=gc.isenabled()))

    def _watch(self):
        while not self.stop.wait(.25):
            self.sample()
            if self.failure:
                return

    def begin(self):
        self.check()
        if self.policy == 'automatic':
            return self.snapshot()
        with self.lock:
            if self.active:
                raise RuntimeError('storage GC window already active')
            self.previous_enabled = gc.isenabled()
            before = time.monotonic()
            collected = gc.collect(2)
            gc.disable()
            self.started = time.monotonic()
            self.active = True
            self.events.append(dict(event='begin', t=self.started,
                collection_start_s=before, collection_s=self.started-before,
                collected=collected, enabled_before=self.previous_enabled,
                enabled_after=gc.isenabled()))
        self.sample()
        self.check()
        self.stop.clear()
        self.monitor = threading.Thread(target=self._watch, daemon=True,
                                        name='storage-gc-memory-guard')
        try:
            self.monitor.start()
        except BaseException:
            self.end(collect=False)
            raise
        return self.snapshot()

    def end(self, collect=True):
        self.sample()
        self.stop.set()
        if (self.monitor is not None and self.monitor is not threading.current_thread()
                and self.monitor.is_alive()):
            self.monitor.join(2)
        with self.lock:
            if self.active:
                ended = time.monotonic()
                self.active = False
                self._restore()
                before = time.monotonic()
                collected = gc.collect(2) if collect else None
                self.events.append(dict(event='end', t=ended,
                    collection_s=time.monotonic()-before, collected=collected,
                    enabled_after=gc.isenabled(), drained_collection=collect))
        return self.snapshot()
