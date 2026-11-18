"""Optional real LMCache/vLLM hook tests; no CUDA extensions or weights."""
import inspect
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from sim.stack.clock import TimeScale
from sim.stack.lmcache_bridge import (LMCacheRunner, benchmark_vllm_config, load_runtime,
                                     enabled, native_hash_record, _active_request)
import test_storage_notifications as notification_tests


@unittest.skipUnless((Path.home()/".local/share/state-aware-network/sources/lmcache-v0.3.11").exists(),
                     "optional LMCache source not prepared")
class LMCacheTest(unittest.TestCase):
    def setup_runner(self, reuse=False):
        harness = notification_tests.NotificationTest()
        self.addCleanup(harness.doCleanups)
        config, service, make_client = harness.start(bandwidth=10000., latency=0.)
        config["kv_connector_implementation"] = "lmcache"
        config["lmcache_sim"] = dict(reuse_vllm_hashes=reuse)
        client = make_client()
        client.layer_status = Mock(side_effect=AssertionError("LMCache polled status"))
        rt = load_runtime(config)
        events = []
        runner = LMCacheRunner(config, client, 0, TimeScale(), None,
                              lambda event, **kw: events.append(dict(event=event, **kw)),
                              benchmark_vllm_config(config))
        self.addCleanup(runner.close)
        return config, service, client, rt, runner, events

    def metadata(self, client, rt, rids):
        metadata = rt.adapter.LMCacheConnectorMetadata()
        metadata.loads = {}
        metadata.native_hashes = {}
        from vllm.utils.hashing import sha256
        from vllm.v1.core.kv_cache_utils import hash_block_tokens
        for index, rid in enumerate(rids):
            metadata.loads[rid] = client.layer_register(0, rid, [b"a"], 10000.)
            metadata.requests.append(rt.adapter.ReqMeta(rid, [index+11, index+21], rt.torch.arange(2),
                load_spec=rt.adapter.LoadSpec(0, 1, True), save_spec=rt.adapter.SaveSpec(1, False)))
            request = SimpleNamespace(block_hashes=[hash_block_tokens(sha256, None, [index+11])])
            metadata.native_hashes[rid] = native_hash_record(request, 1, 1, "sha256")
        return metadata

    def test_original_connector_generator_and_background_manager_execute(self):
        _, service, client, rt, runner, events = self.setup_runner()
        engine = runner.connector._lmcache_engine.lmcache_engine
        self.assertIs(type(engine), rt.LMCacheEngine)
        self.assertIs(type(engine.storage_manager), rt.storage_module.StorageManager)
        self.assertTrue(engine.storage_manager.thread.is_alive())
        self.assertIn("lmcache_connector.py", inspect.getsourcefile(runner.connector.start_load_kv))
        called = set()
        def trace(frame, event, arg):
            if event == "call" and frame.f_code.co_name in (
                    "retrieve_layer", "layerwise_batched_get", "start_load_kv", "wait_for_layer_load"):
                called.add((frame.f_code.co_name, frame.f_code.co_filename))
        runner.install(self.metadata(client, rt, ["a", "b"]))
        try:
            sys.setprofile(trace)
            runner.run(["a", "b", "light"], dict(mode="linear", duration_s=.009), 1)
        finally:
            sys.setprofile(None)
        self.assertTrue(any(name == "retrieve_layer" and "lmcache/v1/cache_engine.py" in path for name,path in called))
        self.assertTrue(any(name == "layerwise_batched_get" and "lmcache/v1/storage_backend/storage_manager.py" in path for name,path in called))
        self.assertEqual(len(service.layer_state.history), 6)
        self.assertEqual(len([e for e in events if e["event"] == "layer_read_observed"]), 6)
        self.assertEqual(runner.connector._lmcache_engine.current_layer, 3)
        self.assertFalse(any(p.references for p in engine.sim_storage_boundary.payloads.values()))

    def test_chunk_continuation_does_not_reload_prefix(self):
        _, service, client, rt, runner, _ = self.setup_runner()
        runner.install(self.metadata(client, rt, ["a"]))
        runner.run(["a"], dict(mode="linear", duration_s=.003), 1)
        metadata = rt.adapter.LMCacheConnectorMetadata()
        metadata.loads = {}
        runner.install(metadata)
        runner.run(["a"], dict(mode="linear", duration_s=.003), 2)
        self.assertEqual(len(service.layer_state.history), 3)
        runner.forget("a")
        self.assertFalse(client._notifications.pending)
        engine = runner.connector._lmcache_engine.lmcache_engine
        self.assertFalse(engine.gpu_connector.ready)
        self.assertFalse(engine.sim_storage_boundary.payloads)

    def test_background_storage_failure_reaches_original_future_wait(self):
        _, service, client, rt, runner, _ = self.setup_runner()
        runner.install(self.metadata(client, rt, ["a"]))
        original = client.layer_submit
        def fail_progress(*args, **kwargs):
            result = original(*args, **kwargs)
            with service.progress.changed:
                service.progress.error = "injected failure"
                service.progress._end_receivers(dict(ok=False, error="injected failure"))
            return result
        client.layer_submit = fail_progress
        with self.assertRaisesRegex((ConnectionError, RuntimeError), "failure"):
            runner.run(["a"], dict(mode="linear", duration_s=.003), 1)

    def test_reused_hashes_preserve_keys_and_skipped_prefix(self):
        _, _, _, rt, runner, _ = self.setup_runner(reuse=True)
        from vllm.utils.hashing import sha256
        from vllm.v1.core.kv_cache_utils import hash_block_tokens
        tokens = list(range(32))
        hashes, parent = [], None
        for value in tokens:
            parent = hash_block_tokens(sha256, parent, [value])
            hashes.append(parent)
        db = runner.connector._lmcache_engine.lmcache_engine.token_database
        mask = rt.torch.tensor([False]*8+[True]*24)
        expected = list(db.process_tokens(tokens=tokens, mask=mask))
        runner.prefix_hashes["probe"] = native_hash_record(SimpleNamespace(block_hashes=hashes), 1, 32, "sha256")
        token = _active_request.set("probe")
        try:
            original = db._hash_tokens
            db._hash_tokens = Mock(side_effect=AssertionError("rehashed native tokens"))
            actual = list(db.process_tokens(tokens=tokens, mask=mask))
            self.assertEqual(actual, expected)
            self.assertEqual([key.split_layers(3) for _,_,key in actual],
                             [key.split_layers(3) for _,_,key in expected])
            self.assertEqual(db.hash_stats["reused_chunks"], 32)
            self.assertEqual(db.hash_stats["computed_chunks"], 32)
        finally:
            db._hash_tokens = original
            _active_request.reset(token)

    def test_reuse_drives_original_layer_pipeline_without_hashing(self):
        _, service, client, rt, runner, _ = self.setup_runner(reuse=True)
        runner.install(self.metadata(client, rt, ["a", "b"]))
        runner.run(["a", "b"], dict(mode="linear", duration_s=.003), 1)
        self.assertEqual(runner.hash_stats()["computed_chunks"], 0)
        self.assertEqual(runner.hash_stats()["reused_chunks"], 2)
        self.assertEqual(len(service.layer_state.history), 6)

    def test_hash_guard_and_fallback(self):
        _, _, _, _, runner, _ = self.setup_runner(reuse=True)
        request = SimpleNamespace(block_hashes=[b"x"*32], cache_salt="salt")
        self.assertIsNone(native_hash_record(request, 1, 1, "sha256"))
        request.cache_salt = None
        self.assertIsNone(native_hash_record(request, 1, 1, "xxhash"))
        request.prompt_embeds = object()
        self.assertIsNone(native_hash_record(request, 1, 1, "sha256"))
        request.prompt_embeds = None
        record = native_hash_record(request, 1, 1, "sha256")
        record["block_size"] = 16
        runner.prefix_hashes["probe"] = record
        token = _active_request.set("probe")
        try:
            db = runner.connector._lmcache_engine.lmcache_engine.token_database
            self.assertEqual(len(list(db.process_tokens(tokens=[11]))), 1)
            self.assertEqual(db.hash_stats["computed_chunks"], 1)
            self.assertEqual(db.hash_stats["reused_chunks"], 0)
        finally:
            _active_request.reset(token)


class LMCacheSelectionTest(unittest.TestCase):
    def test_default_and_invalid_selection(self):
        self.assertFalse(enabled({}))
        self.assertTrue(enabled(dict(kv_connector_implementation="lmcache")))
        with self.assertRaises(ValueError):
            enabled(dict(kv_connector_implementation="unknown"))
