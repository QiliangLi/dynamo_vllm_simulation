"""Opt-in CPU adapter for the real vLLM LMCacheConnectorV1 / LMCache 0.3.11.

Real: scheduler metadata, worker hooks, chunk hashing, retrieve_layer generator,
StorageManager event-loop thread and layer Futures. Simulated: frozen lookup,
storage payload/latency, device tensors and CPU->GPU copy/synchronization.
No LMCache/vLLM source file is edited. Optional imports happen only on opt-in.
"""
import asyncio
from collections import OrderedDict
from contextvars import ContextVar
import functools
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

PIN = "f032152171102e23b62abc7b4448e20cdeb2845a"
_active_request = ContextVar("sim_lmcache_request")
_runtime = None


def enabled(config):
    implementation = config.get("kv_connector_implementation", "sim")
    if implementation not in ("sim", "lmcache"):
        raise ValueError("kv_connector_implementation must be sim or lmcache")
    return implementation == "lmcache"


def load_runtime(config=None):
    global _runtime
    if _runtime is not None:
        return _runtime
    if config is None:
        config = json.loads(Path(os.environ["SIM_STACK_CONFIG"]).read_text())
    options = config.get("lmcache_sim", {})
    cache = Path.home()/".local/share/state-aware-network"
    source = Path(options.get("source", cache/"sources/lmcache-v0.3.11"))
    dependencies = Path(options.get("dependencies", cache/"lmcache-prototype-deps"))
    if not source.is_dir():
        raise RuntimeError("LMCache source missing; run scripts/setup_lmcache_sim.py")
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != PIN or subprocess.call(["git", "-C", str(source), "diff", "--quiet", "HEAD"]):
        raise RuntimeError("LMCache simulation requires the clean pinned v0.3.11 source")
    sys.path[:0] = [str(source), str(dependencies)]
    os.environ["LMCACHE_TRACK_USAGE"] = "false"
    os.environ["LMCACHE_FORCE_SKIP_SAVE"] = "1"
    os.environ.setdefault("LMCACHE_LOG_LEVEL", "ERROR")
    import torch
    import lmcache
    from vllm.version import __version__ as vllm_version
    if vllm_version.split("+")[0] != "0.20.2" or not Path(lmcache.__file__).resolve().is_relative_to(source.resolve()):
        raise RuntimeError("LMCache prototype requires vLLM 0.20.2 and the selected pinned LMCache source")
    from vllm.distributed.kv_transfer.kv_connector.v1 import lmcache_connector
    from vllm.distributed.kv_transfer.kv_connector.v1.lmcache_integration import vllm_v1_adapter as adapter
    from lmcache.config import LMCacheEngineMetadata
    from lmcache.v1.cache_engine import LMCacheEngine
    from lmcache.v1.config import LMCacheEngineConfig
    from lmcache.v1.gpu_connector import VLLMPagedMemLayerwiseGPUConnector
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend
    from lmcache.v1.storage_backend import storage_manager as storage_module
    from lmcache.v1.token_database import ChunkedTokenDatabase
    _runtime = SimpleNamespace(**locals())
    return _runtime


def provenance(config):
    rt = load_runtime(config)
    modules = [rt.lmcache_connector, rt.adapter, sys.modules[rt.LMCacheEngine.__module__], rt.storage_module]
    return dict(lmcache_commit=PIN, files={str(Path(m.__file__).resolve()):
        hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in modules})


def benchmark_vllm_config(config):
    """Metadata-only initialization fixture for the isolated worker benchmark."""
    from sim.stack.layerwise import layer_spec
    extra = {}
    transfer = SimpleNamespace(kv_role="kv_both", kv_connector_extra_config=extra,
                               get_from_extra_config=lambda key, default=None:extra.get(key, default))
    return SimpleNamespace(kv_transfer_config=transfer,
        cache_config=SimpleNamespace(block_size=config["block_size"], prefix_caching_hash_algo="sha256"),
        parallel_config=SimpleNamespace(tensor_parallel_size=1, data_parallel_size_local=1,
                                        data_parallel_rank_local=0),
        model_config=SimpleNamespace(get_num_layers=lambda parallel:layer_spec(config)["layers"]))


def native_hash_record(request, block_size, hit_tokens, algorithm):
    """Snapshot compatible, already-computed vLLM prefix hashes; never rehash."""
    if (algorithm != "sha256" or hit_tokens % block_size or
            getattr(request, "mm_features", None) or getattr(request, "lora_request", None) or
            getattr(request, "cache_salt", None) or getattr(request, "prompt_embeds", None) is not None):
        return None
    hashes = tuple(request.block_hashes[:hit_tokens//block_size])
    if len(hashes) != hit_tokens//block_size or any(not isinstance(h, bytes) or len(h) != 32 for h in hashes):
        return None
    from vllm.v1.core import kv_cache_utils
    return dict(hashes=hashes, block_size=block_size, algorithm=algorithm,
                initial_hash=kv_cache_utils.NONE_HASH, plain_text=True)


class CPUSlots:
    """Device-boundary replacement for slot_mapping.cuda(), preserving slots."""
    def __init__(self, tensor, rid):
        self.tensor, self.rid = tensor, rid

    def cuda(self):
        return self

    def __len__(self):
        return len(self.tensor)

    def __getitem__(self, item):
        return CPUSlots(self.tensor[item], self.rid)


class Payload:
    """Metadata-only payload; no KV tensor bytes are allocated."""
    def __init__(self):
        self.references = 0

    def ref_count_down(self):
        self.references -= 1
        if self.references < 0:
            raise RuntimeError("LMCache mock payload reference underflow")


def create_engine(runtime, lmconfig, config, client, worker, owner):
    from sim.stack.layerwise import layer_spec
    spec = layer_spec(config)
    torch = runtime.torch

    class DeviceBoundary(runtime.VLLMPagedMemLayerwiseGPUConnector):
        def __init__(self):
            self.num_layers = spec["layers"]
            self.ready = set()

        def initialize_kvcaches_ptr(self, **kwargs):
            self.kvcaches = kwargs["kvcaches"]

        def batched_to_gpu(self, starts, ends, **kwargs):
            rid = kwargs["slot_mapping"].rid
            for layer in range(self.num_layers):
                payloads = yield
                if len(payloads) != len(starts):
                    raise RuntimeError("LMCache payload/chunk count mismatch")
                self.ready.add((rid, layer))
            yield
            yield

    class StorageBoundary(runtime.LocalCPUBackend):
        def __init__(self):
            self.dst_device = "cpu"
            self.payloads = {}

        def contains(self, key, pin=False):
            rid = _active_request.get()
            if rid not in owner.loads:
                raise RuntimeError("unregistered LMCache simulated prefix")
            return True  # Only the already-validated frozen hit range reaches this backend.

        async def batched_get_non_blocking(self, lookup_id, keys, transfer_spec=None):
            rid = _active_request.get()
            layers = {key.layer_id for key in keys}
            if len(layers) != 1:
                raise RuntimeError("mixed layer keys")
            layer = layers.pop()
            expected = owner.loads[rid]["layer_bytes"]//spec["bytes_per_token"]
            if len(keys)*lmconfig.chunk_size != expected:
                raise RuntimeError("LMCache hit chunks differ from frozen read bytes")
            token = client.prepare_layers(worker, [rid], layer)
            try:
                client.layer_submit(worker, [rid], layer, subscription=token)
            except Exception as exc:
                client.fail_layers(exc)
                raise
            owner.issued(rid, layer)
            event = await asyncio.wrap_future(client._notifications.pending[(rid, layer)])
            owner.completed[(rid, layer)] = event["completed_s"]
            payload = self.payloads.setdefault(rid, Payload())
            payload.references += len(keys)
            return [payload]*len(keys)

        def close(self):
            if any(p.references for p in self.payloads.values()):
                raise RuntimeError("LMCache simulated payload still referenced")

    boundary = StorageBoundary()
    metadata = runtime.LMCacheEngineMetadata("sim-read-only", 1, 0, "vllm", torch.float32,
                                            (spec["layers"], 2, lmconfig.chunk_size, 1, 1))
    class HashInputDatabase(runtime.ChunkedTokenDatabase):
        def __init__(self, *args):
            super().__init__(*args)
            self.hash_stats = dict(computed_chunks=0, reused_chunks=0, reused_requests=0, fallback_requests=0)

        def _hash_tokens(self, *args, **kwargs):
            self.hash_stats["computed_chunks"] += 1
            return super()._hash_tokens(*args, **kwargs)

        def process_tokens(self, tokens=None, hashes=None, offsets=None, mask=None,
                           make_key=True, request_configs=None):
            rid = _active_request.get(None)
            record = owner.prefix_hashes.get(rid) if rid is not None else None
            reuse = config.get("lmcache_sim", {}).get("reuse_vllm_hashes", False)
            compatible = (reuse and tokens is not None and record is not None and
                record.get("plain_text") is True and record.get("algorithm") == "sha256" and
                record.get("block_size") == self.chunk_size and
                record.get("initial_hash") == self._get_init_hash())
            if not compatible:
                self.hash_stats["fallback_requests"] += 1
                yield from super().process_tokens(tokens=tokens, hashes=hashes, offsets=offsets,
                    mask=mask, make_key=make_key, request_configs=request_configs)
                return
            supplied = record["hashes"]
            if len(tokens) != len(supplied)*self.chunk_size:
                raise RuntimeError("native hash count differs from loaded token range")
            skipped = 0 if mask is None else mask.numel()-mask.long().sum().item()
            if skipped % self.chunk_size:
                raise ValueError("native hash mask must align to chunks")
            self.hash_stats["reused_chunks"] += len(supplied)
            self.hash_stats["reused_requests"] += 1
            # The original hashes branch does not apply token-mask offsets.
            # Preserve the original token path's skipped-prefix semantics here.
            for start, end, key in super().process_tokens(hashes=supplied,
                    offsets=[self.chunk_size]*len(supplied), make_key=make_key,
                    request_configs=request_configs):
                if start >= skipped:
                    yield start, end, key

    database = HashInputDatabase(lmconfig, metadata)
    engine = runtime.LMCacheEngine.__new__(runtime.LMCacheEngine)
    try:
        with patch.object(runtime.storage_module, "CreateStorageBackends",
                          return_value=OrderedDict(LocalCPUBackend=boundary)):
            runtime.LMCacheEngine.__init__(engine, lmconfig, metadata, database, DeviceBoundary(),
                                           lambda *a, **kw: None, lambda *a, **kw: None)
    except BaseException:
        manager = getattr(engine, "storage_manager", None)
        if manager is not None:
            manager.close()
        raise
    original = engine.retrieve_layer

    @functools.wraps(original)
    def with_request(tokens, mask=None, **kwargs):
        generator = original(tokens, mask, **kwargs)
        while True:
            token = _active_request.set(kwargs["slot_mapping"].rid)
            try:
                value = next(generator)
            except StopIteration:
                return
            finally:
                _active_request.reset(token)
            yield value
    engine.retrieve_layer = with_request
    engine.sim_storage_boundary = boundary
    original_unpin = engine.lookup_unpin
    # vLLM 0.20.2 passes a step's list; LMCache 0.3.11 accepts one ID.
    def unpin_step(lookup_ids):
        for lookup_id in lookup_ids if isinstance(lookup_ids, list) else [lookup_ids]:
            original_unpin(lookup_id)
    engine.lookup_unpin = unpin_step
    return engine


_connector_class = None


def connector_class(config=None):
    global _connector_class
    if _connector_class is not None:
        return _connector_class
    rt = load_runtime(config)
    from vllm.distributed.kv_transfer.kv_connector.v1.base import KVConnectorRole
    from sim.stack.connector_rpc import SimConnectorRpc
    from sim.stack.executor import load_stack_config
    from sim.stack.layerwise import layer_spec

    class SimLMCacheConnector(rt.lmcache_connector.LMCacheConnectorV1):
        def __init__(self, vllm_config, role, kv_cache_config=None, *, owner=None, client=None, sim_config=None):
            self.cfg = load_stack_config() if sim_config is None else sim_config
            if self.cfg.get("kv_transfer_mode") != "layer_pipeline" or self.cfg["storage"].get("completion_mode") != "notify":
                raise ValueError("LMCache prototype requires layer_pipeline and notify")
            self.sim = SimConnectorRpc(vllm_config, role, kv_cache_config) if role == KVConnectorRole.SCHEDULER else None
            self.current_lookup = None
            self.owner = owner
            self.client = client
            self._native_hashes = {}
            lmconfig = rt.LMCacheEngineConfig.from_defaults(
                chunk_size=vllm_config.cache_config.block_size, use_layerwise=True,
                enable_async_loading=False, enable_controller=False, enable_kv_events=False,
                internal_api_server_enabled=False, pre_caching_hash_algorithm="sha256",
                local_cpu=True, max_local_cpu_size=.001, py_enable_gc=True)
            vllm_config.kv_transfer_config.kv_connector_extra_config.update(use_native=True, use_layerwise=True)
            outer = self

            class FrozenLookup:
                def lookup(self, tokens, **kwargs):
                    request, computed = outer.current_lookup
                    matched, _ = outer.sim.get_num_new_matched_tokens(request, computed)
                    return computed + matched

                def clear_lookup_status(self, request_id):
                    pass

            with patch.object(rt.adapter, "lmcache_get_or_create_config", return_value=lmconfig), \
                 patch.object(rt.adapter, "_init_lmcache_engine", side_effect=lambda c, v: create_engine(
                     rt, c, self.cfg, client, owner.worker, owner)), \
                 patch.object(rt.adapter.LookupClientFactory, "create_lookup_client", return_value=FrozenLookup()), \
                 patch.object(rt.adapter.LookupClientFactory, "create_lookup_server", return_value=None), \
                 patch.object(rt.adapter, "ZMQOffloadServer", return_value=None), \
                 patch.object(rt.adapter, "get_tensor_model_parallel_rank", return_value=0):
                super().__init__(vllm_config, role, kv_cache_config)
            self._lmcache_engine.num_layers = layer_spec(self.cfg)["layers"]

        def get_num_new_matched_tokens(self, request, num_computed_tokens):
            self.current_lookup = (request, num_computed_tokens)
            try:
                return super().get_num_new_matched_tokens(request, num_computed_tokens)
            finally:
                self.current_lookup = None

        def update_state_after_alloc(self, request, blocks, num_external_tokens):
            super().update_state_after_alloc(request, blocks, num_external_tokens)
            load = self._lmcache_engine.load_specs.get(request.request_id)
            if (self.cfg.get("lmcache_sim", {}).get("reuse_vllm_hashes", False) and
                    num_external_tokens and load is not None and load.vllm_cached_tokens == 0):
                record = native_hash_record(request, self.cfg["block_size"], num_external_tokens,
                    getattr(self._vllm_config.cache_config, "prefix_caching_hash_algo", None))
                if record is not None:
                    self._native_hashes[request.request_id] = record
            self.sim.update_state_after_alloc(request, blocks, num_external_tokens)

        def build_connector_meta(self, scheduler_output):
            metadata = super().build_connector_meta(scheduler_output)
            metadata.loads = self.sim.build_connector_meta(scheduler_output).loads
            metadata.native_hashes, self._native_hashes = self._native_hashes, {}
            return metadata

        def close(self):
            engine = self._lmcache_engine.lmcache_engine
            if engine is not None:
                referenced = sum(p.references for p in engine.sim_storage_boundary.payloads.values())
                engine.close()
                if engine.storage_manager.thread.is_alive():
                    raise RuntimeError("LMCache storage thread did not stop")
                if not engine.storage_manager.loop.is_closed():
                    engine.storage_manager.loop.close()
                if referenced:
                    raise RuntimeError("LMCache payload references not released")

    _connector_class = SimLMCacheConnector
    return _connector_class


def __getattr__(name):
    if name == "SimLMCacheConnector":
        return connector_class()
    raise AttributeError(name)


class LMCacheRunner:
    """Simulated forward invokes the original Connector's worker hooks."""
    def __init__(self, config, storage, worker, clock, observer, emit, vllm_config):
        from sim.stack.layerwise import layer_spec
        from vllm.distributed.kv_transfer.kv_connector.v1.base import KVConnectorRole
        self.config, self.storage, self.worker = config, storage, worker
        self.clock, self.observer, self.emit = clock, observer, emit
        self.spec = layer_spec(config)
        self.loads, self.completed, self.resident = {}, {}, set()
        self.prefix_hashes = {}
        self.metadata = None
        self.connector = connector_class(config)(vllm_config, KVConnectorRole.WORKER,
                                                   owner=self, client=storage, sim_config=config)
        rt = load_runtime(config)
        self.kv = {f"sim.layer.{i}":rt.torch.empty(0) for i in range(self.spec["layers"])}
        self.connector.register_kv_caches(self.kv)

    def install(self, metadata):
        if metadata is None:
            metadata = load_runtime(self.config).adapter.LMCacheConnectorMetadata()
            metadata.loads = {}
        self.metadata = metadata
        self.prefix_hashes.update(getattr(metadata, "native_hashes", {}))
        for rid, load in metadata.loads.items():
            if rid in self.loads:
                raise RuntimeError("LMCache repeated external load is unsupported")
            self.loads[rid] = load
            if self.observer:
                self.observer.submitted(rid, 0, layer_count=self.spec["layers"],
                                        expected_read_bytes=load["layer_bytes"]*self.spec["layers"])
        for request in metadata.requests:
            request.slot_mapping = CPUSlots(request.slot_mapping, request.req_id)

    def issued(self, rid, layer):
        size = self.loads[rid]["layer_bytes"]
        if self.observer:
            self.observer.layer_submitted(rid, layer, size)
        self.emit("layer_read_issued", rid=rid, layer=layer, bytes=size)

    def run(self, ids, timing, step):
        import time
        from sim.stack.layerwise import layer_durations
        started = time.perf_counter()
        if self.observer:
            for rid in ids:
                self.observer.starting(rid)
        self.connector.bind_connector_metadata(self.metadata)
        context = SimpleNamespace(attn_metadata=object(), no_compile_layers={})
        self.connector.start_load_kv(context)
        for layer, duration in enumerate(layer_durations(timing, self.config)):
            name = f"sim.layer.{layer}"
            self.connector.wait_for_layer_load(name)
            for rid in ids:
                if rid in self.loads and rid not in self.resident:
                    if ((rid, layer) not in self.completed or (rid, layer) not in
                            self.connector._lmcache_engine.lmcache_engine.gpu_connector.ready):
                        raise RuntimeError("LMCache layer hook returned before simulated data completion")
                    self.emit("layer_read_observed", layer=layer, rid=rid,
                              completed_model_s=self.completed[(rid, layer)])
                    if layer == self.spec["layers"]-1:
                        self.resident.add(rid)
                        if self.observer:
                            self.observer.completed(rid)
            self.emit("layer_compute_start", step=step, layer=layer, req_ids=ids,
                      duration_model_s=duration, occurred_mono=time.monotonic())
            time.sleep(self.clock.to_wall(duration))
            self.emit("layer_compute_complete", step=step, layer=layer, req_ids=ids)
            self.connector.save_kv_layer(name, self.kv[name], context.attn_metadata)
        self.connector.wait_for_save()
        self.connector.clear_connector_metadata()
        return time.perf_counter()-started

    def forget(self, rid):
        self.loads.pop(rid, None)
        self.prefix_hashes.pop(rid, None)
        self.resident.discard(rid)
        self.completed = {k:v for k,v in self.completed.items() if k[0] != rid}
        engine = self.connector._lmcache_engine.lmcache_engine
        for layer in range(self.spec["layers"]):
            engine.gpu_connector.ready.discard((rid, layer))
        payload = engine.sim_storage_boundary.payloads.pop(rid, None)
        if payload is not None and payload.references:
            raise RuntimeError("request finished with referenced LMCache payload")
        self.storage.forget_layers(rid)

    def close(self):
        self.connector.close()

    def hash_stats(self):
        return dict(self.connector._lmcache_engine.lmcache_engine.token_database.hash_stats)
