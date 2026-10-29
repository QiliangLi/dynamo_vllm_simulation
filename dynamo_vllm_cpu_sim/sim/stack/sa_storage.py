"""Wall-clock adapter for the existing SA aggregate batch/layer allocator.

The inherited registration validates real block hashes. These hashes carry
the hit contract only; they do not create per-block bandwidth claimants.
"""
import math

from sim.sa_model import LayerStorage
from sim.stack.layer_storage import LayerStorageState


class SAStackStorage(LayerStorageState):
    def __init__(self,service,config):
        super().__init__(service,config)
        self.pool=LayerStorage(config["storage"]["shared_link_Bps"],config["storage"]["q_max_Bps"])
        self.flow_history=[]
        self.flow_rows={}
        self.members={}
        self.events=[]
        self.intervals=[]
        self.peak_rate=0.

    def _harvest(self):
        for event in self.pool.events:
            if event["event"]=="storage_interval":
                self.intervals.append(event)
                self.peak_rate=max(self.peak_rate,event["actual_Bps"])
            self.events.append(event)
            if event["event"]=="layer_read_complete":
                key=tuple(event["key"])
                self.flow_rows[key]["completed_s"]=event["t"]
                self.service.last_completion_s=event["t"]
                for rid in self.members[key]:
                    row=self.reads[(key[0],rid,key[2])]
                    row["completed_s"]=event["t"]
                    load=self.loads[rid]
                    load["completed_layers"].append(key[2])
                    load["completed_bytes"]+=row["bytes"]
        self.pool.events.clear()

    def advance_to(self,now):
        if now < self.pool.now:
            raise ValueError("SA clock moved backwards")
        for _ in range(100000):
            self.pool.allocate()
            nxt=self.pool.next_time()
            if nxt > now:break
            self.pool.advance(nxt);self._harvest()
        else:raise RuntimeError("SA advance made no bounded progress")
        if now>self.pool.now:
            self.pool.advance(now);self._harvest()
        self.pool.allocate()
        self.service.store.now=self.pool.now

    def submit(self,worker,rids,layer,step=None,compute_s=None,due_mono=None):
        self.service._advance_to_wall()
        if (type(step) is not int or step<1 or type(layer) is not int
                or not 0<=layer<self.spec["layers"] or not rids or len(set(rids))!=len(rids)
                or compute_s is None or not math.isfinite(compute_s) or compute_s<=0):
            raise ValueError("invalid SA batch/layer/compute duration")
        key=(worker,step,layer)
        if key in self.flow_rows:raise ValueError("duplicate SA batch layer")
        for rid in rids:
            load=self.loads[rid]
            if (load["worker"]!=worker or layer!=len(load["submitted_layers"])
                    or (layer and layer-1 not in load["completed_layers"])):
                raise ValueError("wrong worker, duplicate or out-of-order SA layer")
        if layer and (due_mono is None or not math.isfinite(due_mono)):
            raise ValueError("SA prefetch needs a compute deadline")
        size=sum(self.loads[r]["layer_bytes"] for r in rids)
        due=None if layer==0 else self.service.clock.to_model(due_mono-self.service.t0)
        q=self.pool.qmax if layer==0 else min(self.pool.qmax,size/compute_s)
        self.pool.submit(key,size,q,due)
        # The previous compute's planned end can precede a delayed RPC. Such
        # a blocked prefetch must be boosted immediately, not after completion.
        if due is not None and due<=self.pool.now:
            flow=self.pool.flows[key];flow.q=self.pool.qmax;flow.boosted=True
            self.pool.events.append(dict(t=self.pool.now,event="layer_read_boosted",key=list(key),q_Bps=flow.q))
        self.members[key]=list(rids)
        self.flow_rows[key]=dict(worker=worker,step=step,layer=layer,rids=list(rids),bytes=size,
            submitted_s=self.pool.now,completed_s=None,initial_q_Bps=q,due_s=due,compute_s=compute_s)
        self.flow_history.append(self.flow_rows[key])
        for rid in rids:
            load=self.loads[rid]
            row=dict(worker=worker,rid=rid,layer=layer,step=step,submitted_s=self.pool.now,
                completed_s=None,bytes=load["layer_bytes"],disk_bytes=[load["layer_bytes"]],
                read_latency_s=0.,aggregate_flow=list(key))
            self.history.append(row);self.reads[(worker,rid,layer)]=row
            load["submitted_layers"].append(layer);load["submitted_bytes"]+=row["bytes"]
        s=self.service;s.bytes_submitted+=size
        if s.first_submit_s is None:s.first_submit_s=self.pool.now
        s.peak_pending_requests=max(s.peak_pending_requests,len(self.pool.flows))
        self.pool.allocate();self._harvest()
        return dict(ok=True)

    def snapshot(self):
        data=super().snapshot()
        data.update(placement="sa_aggregate_batch_layer",storage_semantics="sa_max_min",
            pending_reads=len(self.pool.flows),bytes_submitted=self.service.bytes_submitted,
            bytes_transferred=self.pool.bytes_transferred,
            disk_bytes_submitted=[self.service.bytes_submitted],disk_bytes_transferred=[self.pool.bytes_transferred],
            disk_peak_Bps=[self.peak_rate],disk_rates_Bps=[sum(f.rate for f in self.pool.flows.values())],
            bytes_remaining=sum(f.remaining for f in self.pool.flows.values()),
            sa_flow_history=self.flow_history,sa_storage_events=self.events,storage_intervals=self.intervals)
        return data
