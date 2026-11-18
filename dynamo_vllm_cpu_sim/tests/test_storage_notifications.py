"""Real sockets/threads: autonomous progress, Future wakeup and failures."""
from copy import deepcopy
import math
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sim.stack.clock import TimeScale
from sim.stack.layerwise import LayerRunner
from sim.stack.storage_client import StorageClient
from sim.stack.storage_service import Service, handle
from test_layerwise import config


class NotificationTest(unittest.TestCase):
    def start(self, bandwidth=10000., latency=.002, factor=1., service_mode="locked"):
        cfg = config()
        cfg["wall_time_scale"] = factor
        cfg["storage"].update(completion_mode="notify", disk_Bps=bandwidth,
                              read_latency_s=latency, service_mode=service_mode)
        service = Service(cfg, [])
        service.store.present.add(b"a")
        tmp = tempfile.TemporaryDirectory()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = tmp.name + "/storage.sock"
        listener.bind(path)
        listener.listen()
        handlers, peers = [], []

        def accept():
            while True:
                try:
                    peer, _ = listener.accept()
                except OSError:
                    return
                peers.append(peer)
                def run(conn):
                    try:
                        with conn:
                            handle(conn, service)
                    except (OSError, ConnectionError):
                        pass
                thread = threading.Thread(target=run, args=(peer,), daemon=True)
                handlers.append(thread)
                thread.start()

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        service.start()
        clients = []

        def client():
            item = StorageClient(path)
            clients.append(item)
            return item

        def cleanup():
            for item in clients:
                item.close()
            service.close()
            listener.shutdown(socket.SHUT_RDWR)
            listener.close()
            thread.join(3)
            for peer in peers:
                try:
                    peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            for item in handlers:
                item.join(3)
                self.assertFalse(item.is_alive())
            tmp.cleanup()
        self.addCleanup(cleanup)
        return cfg, service, client

    def submit(self, client, rid, layer=0, worker=0):
        token = client.prepare_layers(worker, [rid], layer)
        future = client._notifications.pending[(rid, layer)]
        client.layer_submit(worker, [rid], layer, subscription=token)
        return future

    def test_background_completes_without_status_queries_and_scales_time(self):
        for factor in (1., 3.):
            cfg, service, make_client = self.start(factor=factor)
            client = make_client()
            client.layer_register(0, "a", [b"a"], 10000.)
            future = self.submit(client, "a")
            event = future.result(timeout=3)
            with service._lock:
                row = service.layer_state.reads[(0, "a", 0)]
                self.assertAlmostEqual(event["completed_s"] - row["submitted_s"], .012, places=9)
                self.assertAlmostEqual(service.store.bytes_transferred, 100.)
            self.assertGreaterEqual(time.monotonic() + 1e-8,
                                    service.t0 + factor * event["completed_s"])

    def test_new_read_wakes_and_revises_previous_deadline(self):
        _, service, make_client = self.start(bandwidth=1000., latency=0.)
        first, second = make_client(), make_client()
        first.layer_register(0, "a", [b"a"], 10000.)
        second.layer_register(1, "b", [b"a"], 10000.)
        fa = self.submit(first, "a")
        time.sleep(.025)
        fb = self.submit(second, "b", worker=1)
        a, b = fa.result(3), fb.result(3)
        with service._lock:
            ta = service.layer_state.reads[(0, "a", 0)]["submitted_s"]
            tb = service.layer_state.reads[(1, "b", 0)]["submitted_s"]
        self.assertLess(tb - ta, .1)
        self.assertAlmostEqual(a["completed_s"], ta + .2 - (tb - ta), places=8)
        self.assertAlmostEqual(b["completed_s"], ta + .2, places=8)

    def test_runner_uses_notifications_and_keeps_residency(self):
        cfg, service, make_client = self.start()
        client = make_client()
        client.layer_status = Mock(side_effect=AssertionError("unexpected polling"))
        load = client.layer_register(0, "a", [b"a"], 10000.)
        events = []
        runner = LayerRunner(cfg, client, 0, TimeScale(), Mock(),
                             lambda event, **kw: events.append(dict(event=event, **kw)))
        runner.install(SimpleNamespace(loads={"a":load}))
        timing = dict(mode="linear", duration_s=.009)
        runner.run(["a", "light"], timing, 1)
        runner.run(["a", "light"], timing, 2)
        self.assertEqual(len(service.layer_state.history), 3)
        self.assertEqual(len([e for e in events if e["event"] == "layer_read_observed"]), 3)
        self.assertIn("a", runner.resident)
        runner.forget("a")
        self.assertFalse(client._notifications.pending)

    def test_completion_before_submit_returns_is_not_lost(self):
        cfg, _, make_client = self.start(bandwidth=1e9, latency=0.)
        client = make_client()
        load = client.layer_register(0, "a", [b"a"], 10000.)
        original = client._call
        def delayed_reply(payload):
            reply = original(payload)
            if payload["op"] == "layer_submit":
                # Hold the submit caller until its completion has already
                # arrived. The Future must exist before that caller returns.
                client._notifications.pending[("a", payload["layer"])].result(3)
            return reply
        client._call = delayed_reply
        runner = LayerRunner(cfg, client, 0, TimeScale(), None, lambda *a, **kw: None)
        runner.install(SimpleNamespace(loads={"a":load}))
        runner.run(["a"], dict(mode="linear", duration_s=.003), 1)
        self.assertIn("a", runner.resident)

    def test_cancel_releases_resources_and_does_not_revive_future(self):
        _, service, make_client = self.start(bandwidth=100., latency=0.)
        client = make_client()
        client.layer_register(0, "a", [b"a"], 10000.)
        future = self.submit(client, "a")
        client.layer_cancel(0, "a")
        with self.assertRaisesRegex(RuntimeError, "cancel"):
            future.result(3)
        with service._lock:
            self.assertFalse(service.store.pending)
            self.assertFalse(service.store.queues)
            self.assertTrue(math.isinf(service.store.next_time()))
        with self.assertRaisesRegex(RuntimeError, "canceled"):
            client.layer_submit(0, ["a"], 1, subscription=client._notifications.token)

    def test_shutdown_and_disconnect_fail_waiters(self):
        for disconnect in (False, True):
            _, service, make_client = self.start(bandwidth=100., latency=0.)
            client = make_client()
            client.layer_register(0, "a", [b"a"], 10000.)
            future = self.submit(client, "a")
            if disconnect:
                client._notifications.sock.shutdown(socket.SHUT_RDWR)
            else:
                service.close()
            with self.assertRaises(ConnectionError):
                future.result(3)

    def test_query_only_reads_completion_ledger_and_idle_thread_sleeps(self):
        _, service, make_client = self.start()
        client = make_client()
        client.layer_register(0, "a", [b"a"], 10000.)
        self.submit(client, "a").result(3)
        time.sleep(.01)
        with service._lock:
            previous = service.progress.wakeups
            service._advance_to_wall = Mock(side_effect=AssertionError("query advanced storage"))
        self.assertEqual(client.layer_status(0, ["a"], 0)["ready"], ["a"])
        client.stats()
        time.sleep(.015)
        self.assertEqual(service.progress.wakeups, previous)

    def test_background_failure_fails_all_waiters(self):
        _, service, make_client = self.start(bandwidth=100., latency=0.)
        client = make_client()
        client.layer_register(0, "a", [b"a"], 10000.)
        future = self.submit(client, "a")
        with service.progress.changed:
            service._advance_to_wall = Mock(side_effect=RuntimeError("test progress failure"))
            service.progress.changed.notify()
        with self.assertRaisesRegex(ConnectionError, "progress failure"):
            future.result(3)
        with self.assertRaisesRegex(RuntimeError, "progress failed"):
            client.layer_submit(0, ["a"], 1, subscription=client._notifications.token)

    def test_rejects_wrong_subscription_and_unsupported_modes(self):
        cfg, service, make_client = self.start()
        client = make_client()
        client.layer_register(0, "a", [b"a"], 10000.)
        with self.assertRaisesRegex(RuntimeError, "subscription"):
            client.layer_submit(0, ["a"], 0)
        self.assertFalse(service.store.pending)
        for update in (dict(completion_mode="bad"), dict(max_min_read_representation="per_block")):
            bad = deepcopy(cfg)
            bad["storage"].update(update)
            with self.assertRaises(ValueError):
                Service(bad, [])
        bad = deepcopy(cfg)
        bad["kv_transfer_mode"] = "bulk"
        with self.assertRaises(ValueError):
            Service(bad, [])
