"""真实 vLLM 同步 layerwise KVConnector + SA 八层执行模型。"""

from dataclasses import dataclass, field
import math

from sim.connector import SimConnector
from sim.engine import Engine, KVConnectorFactory, ModelRunnerOutput, Request, SamplingParams
from sim.sa_model import layer_compute


class SALayerConnector(SimConnector):
    """外部前缀同步进入本步；执行层逐层等待，不伪造 finished_recving。"""

    def get_num_new_matched_tokens(self, request, num_computed_tokens):
        return max(0, self.remote[request.request_id]-num_computed_tokens), False

    def update_state_after_alloc(self, request, blocks, num_external_tokens):
        if num_external_tokens:
            self.submissions.append((request.request_id, num_external_tokens))
            self.external_tokens[request.request_id] = num_external_tokens


KVConnectorFactory.register_connector("SALayerConnector", "sim.sa_layer_engine", "SALayerConnector")


def make_sa_request(row, policy='fcfs'):
    """block=1 精确保留原始 h/u；SA 无本地前缀复用，不计算无用的前缀哈希。"""
    from sim.priority import edf_priority
    priority=edf_priority(row['deadline_s']) if policy=='priority' else 0
    return Request(request_id=row["id"], prompt_token_ids=row["prompt_token_ids"],
                   sampling_params=SamplingParams(max_tokens=1, ignore_eos=True, temperature=0),
                   pooling_params=None, arrival_time=row["arrival_s"], block_hasher=None,
                   priority=priority)


@dataclass
class LayerStep:
    out: object
    model: object
    start: float
    c: float
    v: float
    remote: dict
    prefill: int
    step_id: int
    layer: int = 0
    phase: str = "STALL"
    compute_end: float = math.inf
    ready: dict = field(default_factory=dict)


class SALayerEngine(Engine):
    def __init__(self, worker_id, config, store, rows):
        super().__init__(worker_id, config, store)
        self.scheduler.connector.remote = {r["id"]: r["h_tokens"] for r in rows}
        self.runtime = None
        self.peak_kv_blocks = 0
        self.peak_memory_bytes = 0
        self.output_events = []

    def schedule_layer_step(self, now):
        if self.runtime is not None or not self.scheduler.has_requests():
            return
        out = self.scheduler.schedule()
        if not out.num_scheduled_tokens:
            if self.scheduler.get_num_unfinished_requests() == 0:
                self.scheduler.update_from_output(out, ModelRunnerOutput(req_ids=[],req_id_to_index={},sampled_token_ids=[]))
                return
            raise RuntimeError(f"layerwise scheduler made no progress at {now}; "
                               f"requests={[(r.request_id,r.status.name) for r in self.scheduler.requests.values()]}")
        ids = list(out.num_scheduled_tokens)
        shapes, sampled = [], []
        for rid, count in out.num_scheduled_tokens.items():
            req = self.scheduler.requests[rid]
            before = req.num_computed_tokens-count
            if before+count > req.num_prompt_tokens:
                raise RuntimeError("SA experiment has no decode; expected prefill only")
            shapes.append((before, count))
            sampled.append([100] if req.num_computed_tokens >= req.num_tokens else [])
        remote = dict(self.scheduler.connector.submissions)
        self.scheduler.connector.submissions.clear()
        self.steps += 1
        c = layer_compute(shapes, self.c["sa_profile"])
        kappa = float(self.c["storage"]["layer_bytes_per_token"])
        v = sum(remote.values())*kappa
        self.runtime = LayerStep(out, ModelRunnerOutput(req_ids=ids,
                                      req_id_to_index={r:i for i,r in enumerate(ids)},
                                      sampled_token_ids=sampled), now, c, v, remote,
                                 sum(out.num_scheduled_tokens.values()), self.steps)
        self.runtime.ready[0] = not bool(v)
        self.output_events.append({"t": now, "event": "step_started", "worker": self.id,
                                   "step_id": self.steps, "num_requests": len(ids),
                                   "num_scheduled_tokens": dict(out.num_scheduled_tokens),
                                   "shapes": shapes, "layer_compute_s": c, "layer_read_bytes": v})
        for rid, h in remote.items():
            self.output_events.append({"t": now, "event": "load_submitted", "id": rid,
                                       "worker": self.id, "submitted_read_bytes": h*kappa*self.c["sa_profile"]["L"]})
        if v:
            self.store.submit((self.id, self.steps, 0), v, self.store.qmax)
        used = self.c["num_blocks"]-self.scheduler.kv_cache_manager.block_pool.free_block_queue.num_free_blocks
        self.peak_kv_blocks = max(self.peak_kv_blocks, used)
        memory = self.c["memory"]["base_bytes"] + (used-1)*self.c["memory"]["kv_bytes_per_token"]
        memory += self.c["memory"]["workspace_bytes_per_token"]*self.runtime.prefill
        self.peak_memory_bytes = max(self.peak_memory_bytes, memory)
        if memory > self.c["memory"]["total_bytes"]+1e-3:
            raise RuntimeError("logical memory budget exceeded")

    def close_layers(self, now):
        """每事件点处理本层完成，读取就绪后启动下一层，并预取一层。"""
        s = self.runtime
        if s is None:
            return None
        if s.phase == "COMPUTE" and s.compute_end <= now:
            self.output_events.append({"t": now, "event": "layer_compute_complete",
                                       "worker": self.id, "step_id": s.step_id, "layer": s.layer})
            s.layer += 1
            s.phase, s.compute_end = "STALL", math.inf
            if s.layer == self.c["sa_profile"]["L"]:
                self.output_events.append({"t": now, "event": "step_scheduled", "worker": self.id,
                                           "step_id": s.step_id, "start_s": s.start,
                                           "num_requests": len(s.out.num_scheduled_tokens),
                                           "num_scheduled_tokens": dict(s.out.num_scheduled_tokens),
                                           "prefill_tokens": s.prefill, "decode_tokens": 0,
                                           "duration_s": self.c["sa_profile"]["L"]*s.c,
                                           "elapsed_s": now-s.start})
                self.runtime = None
                return s.out, s.model, now
        if s.phase == "STALL" and s.ready.get(s.layer):
            s.phase, s.compute_end = "COMPUTE", now+s.c
            self.output_events.append({"t": now, "event": "layer_compute_started",
                                       "worker": self.id, "step_id": s.step_id, "layer": s.layer,
                                       "end_s": s.compute_end, "num_requests": len(s.out.num_scheduled_tokens)})
            if s.layer+1 < self.c["sa_profile"]["L"]:
                if s.v:
                    self.store.submit((self.id, s.step_id, s.layer+1), s.v,
                                      min(self.store.qmax, s.v/s.c), s.compute_end)
                else:
                    s.ready[s.layer+1] = True
        return None

    def account(self, dt):
        if self.runtime is not None and self.runtime.phase == "COMPUTE":
            self.compute_s += dt
        elif self.scheduler.has_requests():
            self.stall_s += dt
        else:
            self.idle_s += dt
