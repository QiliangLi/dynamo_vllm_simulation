"""GC deferral must restore state and reject unsafe or failed windows."""
from types import SimpleNamespace
import unittest
from unittest import mock

from sim.stack.storage_gc import StorageGcWindow


class StorageGcWindowTest(unittest.TestCase):
    def setUp(self):
        self.enabled = True
        def enable(): self.enabled = True
        def disable(): self.enabled = False
        for name, value in [('enable', enable), ('disable', disable),
                            ('isenabled', lambda: self.enabled), ('collect', lambda gen: 3)]:
            p = mock.patch('sim.stack.storage_gc.gc.'+name, side_effect=value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in [('rss_bytes', 1024), ('threading.Thread', None)]:
            p = mock.patch('sim.stack.storage_gc.'+name)
            target = p.start()
            if value is not None: target.return_value = value
            self.addCleanup(p.stop)
        self.window = StorageGcWindow(dict(gc_policy='defer_during_run'))
        self.addCleanup(lambda: self.window.end(collect=False))

    def test_collect_only_at_boundaries_restore_and_idempotent_end(self):
        from sim.stack.storage_gc import gc
        state = self.window.begin()
        self.assertTrue(state['active'])
        self.assertFalse(self.enabled)
        self.window.sample()
        self.assertEqual(gc.collect.call_count, 1)
        with self.assertRaisesRegex(RuntimeError, 'already active'):
            self.window.begin()
        state = self.window.end()
        self.assertTrue(self.enabled)
        self.assertFalse(state['active'])
        self.assertEqual(gc.collect.call_count, 2)
        self.window.end()
        self.assertEqual(gc.collect.call_count, 2)

    def test_preserve_previously_disabled_gc(self):
        self.enabled = False
        self.window.begin()
        self.window.end()
        self.assertFalse(self.enabled)

    def test_abort_restores_without_forced_collection(self):
        from sim.stack.storage_gc import gc
        self.window.begin()
        self.window.end(collect=False)
        self.assertTrue(self.enabled)
        self.assertEqual(gc.collect.call_count, 1)

    def test_rss_guard_restores_gc_and_invalidates_run(self):
        self.window.begin()
        with mock.patch('sim.stack.storage_gc.rss_bytes', return_value=8*2**30):
            self.window.sample()
        self.assertTrue(self.enabled)
        self.assertFalse(self.window.active)
        with self.assertRaisesRegex(RuntimeError, 'RSS limit'):
            self.window.check()

    def test_deadline_guard_restores_gc(self):
        self.window.begin()
        with mock.patch('sim.stack.storage_gc.time.monotonic',
                        return_value=self.window.started+601):
            self.window.sample()
        self.assertTrue(self.enabled)
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            self.window.check()

    def test_memory_probe_error_invalidates_run(self):
        self.window.begin()
        with mock.patch('sim.stack.storage_gc.rss_bytes', side_effect=OSError('probe')):
            self.window.sample()
        self.assertTrue(self.enabled)
        with self.assertRaisesRegex(RuntimeError, 'memory monitor failed'):
            self.window.check()

    def test_automatic_policy_does_not_change_gc(self):
        from sim.stack.storage_gc import gc
        window = StorageGcWindow(dict(gc_policy='automatic'))
        self.assertFalse(window.begin()['active'])
        window.end()
        self.assertTrue(self.enabled)
        gc.collect.assert_not_called()

    def test_invalid_limits_and_policy(self):
        for cfg in [dict(gc_policy='bad'), dict(gc_max_rss_bytes=0),
                    dict(gc_max_rss_bytes=True), dict(gc_max_window_s=float('inf')),
                    dict(gc_max_window_s='600'), dict(gc_max_window_s=True)]:
            with self.assertRaises(ValueError): StorageGcWindow(cfg)

    def test_service_refuses_busy_begin_and_restores_busy_end(self):
        from sim.stack.storage_service import Service
        service = Service.__new__(Service)
        service.gc_window = self.window
        service.store = SimpleNamespace(pending={'r': 1})
        service._advance_to_wall = lambda: None
        with self.assertRaisesRegex(RuntimeError, 'pending storage'):
            service.op_gc_control('begin')
        self.assertTrue(self.enabled)
        self.window.begin()
        with self.assertRaisesRegex(RuntimeError, 'not drained'):
            service.op_gc_control('end')
        self.assertTrue(self.enabled)
        self.assertFalse(self.window.active)


if __name__ == '__main__':
    unittest.main()
