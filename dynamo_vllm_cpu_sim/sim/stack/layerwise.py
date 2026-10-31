"""Layerwise contracts and execution; native SchedulerOutput remains authoritative."""
import math
import time

from sim.stack import lease_events

MODES = ("bulk", "layer_serial", "layer_pipeline")


def transfer_mode(config):
    mode = config.get("kv_transfer_mode", "bulk")
    if mode not in MODES:
        raise ValueError("unknown kv_transfer_mode")
    return mode


def layer_spec(config):
    mode = transfer_mode(config)
    sa = config["compute"].get("mode") == "sa_e26f"
    if mode == "bulk":
        if sa:
            raise ValueError("SA stack requires layer_pipeline")
        return None
    if sa:
        from fractions import Fraction
        p = config["compute"]["profile"]
        sc = config["storage"]
        if (mode != "layer_pipeline" or config.get("enable_prefix_caching",False)
                or sc["bandwidth_policy"] != "sa_max_min" or sc["disks"] != 1
                or sc.get("read_latency_s",0) != 0
                or sc.get("shared_link_Bps") != sc["disk_Bps"]
                or not math.isfinite(sc["q_max_Bps"]) or sc["q_max_Bps"] <= 0):
            raise ValueError("SA requires pipeline, prefix cache off, one aggregate sa_max_min pool, zero startup")
        per_token = Fraction(p["kappa_gb_per_token_layer"])*10**9
        if per_token.denominator != 1 or sc["kv_bytes_per_token"] != per_token*p["L"]:
            raise ValueError("SA KV bytes must match profile layers and kappa")
        return dict(layers=p["L"],bytes_per_token=int(per_token),startup="once")
    if (config.get("enable_prefix_caching", False)
            or config["storage"].get("bandwidth_policy") != "max_min_fair"
            or config["storage"].get("max_min_read_representation", "grouped") != "grouped"):
        raise ValueError("layerwise requires prefix cache off and grouped max_min_fair")
    options = config.get("layerwise", {})
    glm = config["compute"].get("mode") == "glm5_d950pr"
    layers = options.get("layers", 78 if glm else 1)
    if type(layers) is not int or layers < 1 or (glm and layers != 78):
        raise ValueError("bad layer count; GLM5 has 78 layers")
    total = config["storage"]["kv_bytes_per_token"]
    if total != int(total) or int(total) % layers:
        raise ValueError("whole-prefix bytes/token must divide exactly into layers")
    startup = options.get("startup", "once")
    if startup not in ("once", "per_layer"):
        raise ValueError("layerwise.startup must be once or per_layer")
    return dict(layers=layers, bytes_per_token=int(total)//layers, startup=startup)


def layer_durations(timing, config):
    spec = layer_spec(config)
    count = spec["layers"]
    if timing["mode"] == "sa_e26f":
        result = [timing["layer_compute_s"]]*count
    elif timing["mode"] == "glm5_d950pr":
        dense = timing["dense_layers"]
        if "layer_durations_us" in timing:
            if len(timing["layer_durations_us"]) != count:
                raise ValueError("GLM layer timing count differs from layer spec")
            result = [t*1e-6 for t in timing["layer_durations_us"]]
        else:
            result = ([timing["dense_layer_us"]*1e-6]*dense
                      + [timing["moe_layer_us"]*1e-6]*(count-dense))
        result[0] += timing["engine_overhead_s"]
    else:
        overhead = config["compute"]["step_overhead_s"]
        result = [(timing["duration_s"]-overhead)/count]*count
        result[0] += overhead
    assert math.isclose(math.fsum(result), timing["duration_s"], rel_tol=1e-12, abs_tol=1e-12)
    return result


class LayerRunner:
    """One fixed native batch; one-layer lookahead; persistent request KV residency."""
    def __init__(self, config, storage, worker, clock, observer, emit):
        self.config, self.storage, self.worker = config, storage, worker
        self.clock, self.observer, self.emit = clock, observer, emit
        self.spec = layer_spec(config)
        self.loads = {}
        self.resident = set()
        self.submitted = set()
        self.ready = set()

    def install(self, metadata):
        for rid, load in getattr(metadata, "loads", {}).items():
            if rid in self.loads:
                raise RuntimeError("repeated layerwise external load/preemption unsupported")
            self.loads[rid] = load
            if self.observer:
                # Parent attempt starts once. Bytes are counted only as layers
                # are actually submitted, not all up front as already-issued IO.
                self.observer.submitted(rid,0,layer_count=self.spec["layers"],
                    expected_read_bytes=load["layer_bytes"]*self.spec["layers"])

    def _submit(self, ids, layer, step=None, compute_s=None, due_mono=None):
        pending = [rid for rid in ids if rid in self.loads and rid not in self.resident
                   and (rid,layer) not in self.submitted]
        if not pending:
            return
        if self.config["compute"].get("mode") == "sa_e26f":
            self.storage.layer_submit(self.worker,pending,layer,step=step,
                                      compute_s=compute_s,due_mono=due_mono)
        else:
            self.storage.layer_submit(self.worker, pending, layer)
        for rid in pending:
            self.submitted.add((rid,layer))
            size = self.loads[rid]["layer_bytes"]
            if self.observer:
                self.observer.layer_submitted(rid,layer,size)
            self.emit("layer_read_issued",layer=layer,rid=rid,bytes=size)

    def _wait(self, ids, layer):
        waiting = [rid for rid in ids if rid in self.loads and rid not in self.resident
                   and (rid,layer) not in self.ready]
        while waiting:
            result = self.storage.layer_status(self.worker,waiting,layer)
            for rid in result["ready"]:
                self.ready.add((rid,layer))
                self.emit("layer_read_observed",layer=layer,rid=rid,
                          completed_model_s=result["completed_s"][rid])
                if layer == self.spec["layers"]-1:
                    self.resident.add(rid)
                    # 末层读毕：先落离线观测时间戳，再发租约减账事件，
                    # 保证审计口径 load_done_s ≤ released_s。
                    if self.observer:
                        self.observer.completed(rid)
                    lease_events.notify_completed(rid)
            waiting = [rid for rid in waiting if (rid,layer) not in self.ready]
            if waiting:
                time.sleep(.0005)  # Actual implementation polling, not modeled IO latency.

    def run(self, ids, timing, step):
        durations = layer_durations(timing,self.config)
        started = time.perf_counter()
        if self.observer:
            for rid in ids:
                self.observer.starting(rid)
        self._submit(ids,0,step=step,compute_s=durations[0])
        # 首层读实际提交 = 读开始；只对持租约的重发事件，轻读零消息。
        for rid in ids:
            lease_events.notify_started(rid)
        for layer,duration in enumerate(durations):
            self._wait(ids,layer)
            begin = time.monotonic()
            self.emit("layer_compute_start",occurred_mono=begin,step=step,layer=layer,req_ids=ids,
                      duration_model_s=duration)
            if self.config["kv_transfer_mode"] == "layer_pipeline" and layer+1<len(durations):
                self._submit(ids,layer+1,step=step,compute_s=duration,
                             due_mono=begin+self.clock.to_wall(duration))
            # Prefetch RPC processing is inside the layer's elapsed interval.
            # It must not be followed by another full compute sleep.
            time.sleep(max(0.,self.clock.to_wall(duration)-(time.monotonic()-begin)))
            self.emit("layer_compute_complete",step=step,layer=layer,req_ids=ids)
            if self.config["kv_transfer_mode"] == "layer_serial" and layer+1<len(durations):
                self._submit(ids,layer+1)
        return time.perf_counter()-started

    def forget(self,rid):
        self.loads.pop(rid,None)
        self.resident.discard(rid)
        self.ready = {key for key in self.ready if key[0]!=rid}
        self.submitted = {key for key in self.submitted if key[0]!=rid}
        lease_events.forget(rid)
