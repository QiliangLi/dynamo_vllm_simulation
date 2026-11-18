"""Real-socket owner tests, including the existing notification contracts."""
from copy import deepcopy
import threading
import time
from unittest.mock import Mock

from sim.stack.storage_service import Service
import test_storage_notifications as notification_contracts
from test_layerwise import config


class SingleWriterTest(notification_contracts.NotificationTest):
    def start(self, **kwargs):
        return super().start(service_mode="single_writer", **kwargs)

    def test_query_only_reads_completion_ledger_and_idle_thread_sleeps(self):
        _, service, make_client = self.start()
        client = make_client()
        client.layer_register(0, "a", [b"a"], 10000.)
        self.submit(client, "a").result(3)
        def freeze():
            service._advance_to_wall = Mock(side_effect=AssertionError("query advanced storage"))
            return service.progress.wakeups
        previous = service.serialized(freeze, "test-freeze")
        self.assertEqual(client.layer_status(0, ["a"], 0)["ready"], ["a"])
        client.stats()
        time.sleep(.015)
        self.assertEqual(service.progress.wakeups, previous)

    def test_background_failure_fails_all_waiters(self):
        _, service, make_client = self.start(bandwidth=100., latency=0.)
        client = make_client()
        client.layer_register(0, "a", [b"a"], 10000.)
        future = self.submit(client, "a")
        service.serialized(lambda: setattr(service, "_advance_to_wall",
            Mock(side_effect=RuntimeError("test progress failure"))), "test-inject")
        with self.assertRaisesRegex(ConnectionError, "progress failure"):
            future.result(3)
        with self.assertRaisesRegex(RuntimeError, "progress failed"):
            client.layer_submit(0, ["a"], 1, subscription=client._notifications.token)

    def test_global_model_lock_is_unused_and_mutations_have_one_owner(self):
        _, service, make_client = self.start()
        client = make_client()
        for name in ("advance", "submit_counted"):
            original = getattr(service.store, name)
            def checked(*args, original=original, **kwargs):
                self.assertIs(threading.current_thread(), service.progress.thread)
                return original(*args, **kwargs)
            setattr(service.store, name, checked)
        # Holding the old model lock cannot delay any owner-mode RPC or I/O.
        with service._lock:
            client.layer_register(0, "a", [b"a"], 10000.)
            self.submit(client, "a").result(3)
            self.assertEqual(client.stats()["pending_reads"], 0)
        with self.assertRaisesRegex(RuntimeError, "only the storage owner"):
            service._advance_to_wall()

    def test_queued_ack_is_after_execution_and_bad_command_does_not_stop_owner(self):
        _, service, _ = self.start()
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        order = []
        def first():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release")
            order.append("first")
        t1 = threading.Thread(target=lambda: service.serialized(first, "first"))
        t2 = threading.Thread(target=lambda: (service.serialized(lambda: order.append("second"), "second"), returned.set()))
        t1.start()
        self.assertTrue(entered.wait(3))
        t2.start()
        try:
            self.assertFalse(returned.wait(.02))
        finally:
            release.set()
            t1.join(3)
            t2.join(3)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(order, ["first", "second"])
        with self.assertRaisesRegex(ValueError, "bad command"):
            service.serialized(lambda: (_ for _ in ()).throw(ValueError("bad command")), "bad")
        self.assertEqual(service.serialized(lambda: "alive", "probe"), "alive")

    def test_invalid_service_configuration_is_rejected(self):
        for options in (dict(service_mode="bad"), dict(service_mode="single_writer", completion_mode="poll")):
            cfg = deepcopy(config())
            cfg["storage"].update(options)
            with self.assertRaises(ValueError):
                Service(cfg, [])
