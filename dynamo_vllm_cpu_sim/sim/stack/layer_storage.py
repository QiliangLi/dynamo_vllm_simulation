"""Request-prefix registration and per-layer reads on the existing weighted pool."""
import math
from collections import Counter

from sim.stack.layerwise import layer_spec


class LayerStorageState:
    def __init__(self, service, config):
        self.service = service
        self.spec = layer_spec(config)
        self.loads = {}
        self.history = []
        self.reads = {}

    def register(self, worker, rid, blocks_hex, demand):
        s = self.service
        if type(worker) is not int or worker<0 or rid in self.loads:
            raise ValueError("invalid worker or repeated external load")
        blocks = [bytes.fromhex(h) for h in blocks_hex]
        if not blocks or not math.isfinite(demand) or demand<=0:
            raise ValueError("invalid registered layer read")
        if s.first_seen and blocks != s._matched.get(rid):
            raise ValueError("layer read differs from first_seen hit contract")
        if not all(b in s.store.present for b in blocks):
            raise ValueError("layer read contains absent blocks")
        counts = dict(Counter(s.store.disk_of(b) for b in blocks))
        size = len(blocks)*s.block_size*self.spec["bytes_per_token"]
        self.loads[rid] = dict(worker=worker,blocks_hex=blocks_hex,counts=counts,demand=demand,
            layer_bytes=size,total_bytes=size*self.spec["layers"],submitted_layers=[],completed_layers=[],
            submitted_bytes=0,completed_bytes=0)
        return dict(ok=True,layer_bytes=size,layers=self.spec["layers"])

    def submit(self, worker, rids, layer):
        s = self.service
        s._advance_to_wall()
        if type(layer) is not int or not 0<=layer<self.spec["layers"] or len(set(rids))!=len(rids):
            raise ValueError("invalid layer/batch")
        # Validate the whole batch before mutating any state.
        for rid in rids:
            load = self.loads[rid]
            if load["worker"]!=worker or layer!=len(load["submitted_layers"]):
                raise ValueError("wrong worker, duplicate or out-of-order layer")
            if layer and layer-1 not in load["completed_layers"]:
                raise ValueError("lookahead exceeds one layer")
        for rid in rids:
            load = self.loads[rid]
            latency = s.store.c["read_latency_s"] if layer==0 or self.spec["startup"]=="per_layer" else 0.
            key = (worker,rid,layer)
            block_bytes = s.block_size*self.spec["bytes_per_token"]
            s.store.submit_counted(s.store.now,key,load["counts"],block_bytes,load["demand"],latency)
            row = dict(worker=worker,rid=rid,layer=layer,submitted_s=s.store.now,completed_s=None,
                bytes=load["layer_bytes"],disk_bytes=[load["counts"].get(d,0)*block_bytes
                    for d in range(s.store.c["disks"])],read_latency_s=latency)
            self.history.append(row);self.reads[key]=row
            load["submitted_layers"].append(layer)
            load["submitted_bytes"] += load["layer_bytes"]
            s.bytes_submitted += load["layer_bytes"]
            if s.first_submit_s is None:s.first_submit_s=s.store.now
            s.peak_pending_requests=max(s.peak_pending_requests,len(s.store.pending))
        return dict(ok=True)

    def complete(self, key):
        row = self.reads[key]
        row["completed_s"] = self.service.store.now
        load = self.loads[key[1]]
        load["completed_layers"].append(key[2])
        load["completed_bytes"] += row["bytes"]

    def status(self, worker, rids, layer):
        self.service._advance_to_wall()
        ready = []
        for rid in rids:
            if self.loads[rid]["worker"]!=worker:raise ValueError("wrong layer worker")
            if layer in self.loads[rid]["completed_layers"]:ready.append(rid)
        return dict(ok=True,ready=ready,completed_s={rid:self.reads[(worker,rid,layer)]["completed_s"] for rid in ready})

    def snapshot(self):
        return dict(layer_spec=self.spec,layer_loads=self.loads,layer_read_history=self.history,
                    t0_monotonic_s=self.service.t0)
