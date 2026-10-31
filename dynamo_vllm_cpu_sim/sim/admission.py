"""纯请求级准入：只消费已到达画像和可见反馈，不导入引擎或路由代码。"""

from dataclasses import dataclass, asdict
import math

from sim.request_profile import RequestProfile


@dataclass
class AdmissionRecord:
    profile: RequestProfile
    state: str = "QUEUED"
    worker_id: int | None = None
    reserved_s: float | None = None
    submitted_s: float | None = None
    first_token_s: float | None = None
    finish_s: float | None = None
    lease_open: bool = False
    lease_release_reason: str | None = None
    no_external_load: bool = False
    load_submit_s: float | None = None
    load_done_s: float | None = None
    submitted_read_bytes: int = 0
    load_attempts: int = 0


@dataclass(frozen=True)
class AdmissionSnapshot:
    now_s: float
    threshold_version: int
    heavy_threshold_Bps: float | None
    heavy_cap: int | None
    heavy_held: int
    pending_ids: tuple[str, ...]


@dataclass(frozen=True)
class AdmissionDecision:
    request_id: str
    reason: str
    threshold_version: int


class AdmissionController:
    def __init__(self, config):
        self.c = dict(config)
        self.records = {}
        self.pending = set()
        self.threshold_version = 0
        self.theta = self.cap = None
        self.last_exemption_s = -math.inf
        self.logs = []
        self.seen_events = set()
        self.seen_attempts = set()
        self.seen_layers = set()
        self.layer_attempts = {}
        self.exemptions = 0
        self.invalid_reasons = set()

    def _log(self, event, now_s, **values):
        self.logs.append({"t": now_s, "event": event, **values})

    def is_heavy(self, profile):
        return self.theta is not None and profile.pressure_Bps > 0 and profile.pressure_Bps >= self.theta

    @property
    def held(self):
        return sum(r.lease_open and self.is_heavy(r.profile) for r in self.records.values())

    def on_arrivals(self, profiles, now_s):
        """同刻全部到达作为一次原子更新；历史含已完成请求，不含未来请求。"""
        profiles = list(profiles)
        if not profiles:
            return
        if len({p.request_id for p in profiles}) != len(profiles):
            raise ValueError("duplicate arrivals")
        if any(p.request_id in self.records or p.arrival_s > now_s for p in profiles):
            raise ValueError("duplicate or future arrival")
        old_held = self.held
        for p in profiles:
            self.records[p.request_id] = AdmissionRecord(p)
            self.pending.add(p.request_id)
            self._log("request_arrived", now_s, id=p.request_id, profile=asdict(p))
        xs = sorted(r.profile.pressure_Bps for r in self.records.values())
        positive = [x for x in xs if x > 0]
        raw = xs[math.floor(self.c["quantile"] * (len(xs) - 1))]
        self.theta = (raw if raw > 0 else min(positive)) if positive else None
        self.cap = max(1, math.floor(self.c["reference_bandwidth_Bps"] / self.theta)) if self.theta else None
        self.threshold_version += 1
        self._log("threshold_updated", now_s, threshold_version=self.threshold_version,
                  theta=self.theta, raw_quantile=raw, cap=self.cap, held=self.held,
                  held_before=old_held, history_size=len(xs),
                  reclassified_over_cap=self.cap is not None and self.held > self.cap,
                  zero_rule="minimum_positive_if_quantile_zero")

    def _key(self, rid, now_s):
        p = self.records[rid].profile
        if self.c["mode"] in ("immediate", "quota_fcfs"):
            return p.arrival_s, rid
        return p.deadline_s - now_s - p.reference_service_s, p.deadline_s, rid

    def pick(self, now_s):
        limited = self.c["mode"] in ("quota_fcfs", "tfs")
        for rid in sorted(self.pending, key=lambda rid: self._key(rid, now_s)):
            if not limited or not self.is_heavy(self.records[rid].profile) or self.held < self.cap:
                return AdmissionDecision(rid, "normal", self.threshold_version)
        if (self.pending and limited and self.c["aging_enabled"]
                and now_s >= self.last_exemption_s + self.c["aging_cooldown_s"] - 1e-12):
            # 到此说明没有未派发的非重请求；每次只批准一个最老重请求。
            rid = min(self.pending, key=lambda r: (self.records[r].profile.arrival_s, r))
            if now_s >= self.records[rid].profile.arrival_s + self.c["aging_wait_s"] - 1e-12:
                return AdmissionDecision(rid, "aging", self.threshold_version)
        return None

    def reserve(self, decision, now_s):
        if decision.threshold_version != self.threshold_version or self.pick(now_s) != decision:
            raise ValueError("stale or infeasible admission decision")
        rec = self.records[decision.request_id]
        before = self.held
        self.pending.remove(decision.request_id)
        rec.state, rec.reserved_s, rec.lease_open = "RESERVED", now_s, True
        if decision.reason == "aging":
            self.last_exemption_s = now_s
            self.exemptions += 1
            self._log("aging_exempted", now_s, id=decision.request_id)
        self._log("admission_reserved", now_s, id=decision.request_id,
                  d_i=rec.profile.pressure_Bps, theta=self.theta, cap=self.cap,
                  heavy=self.is_heavy(rec.profile), held_before=before, held_after=self.held,
                  release_on=self.c["release_on"], reason=decision.reason,
                  threshold_version=self.threshold_version, mode=self.c["mode"])

    def mark_submitted(self, request_id, worker_id, now_s):
        r = self.records[request_id]
        if r.state != "RESERVED":
            raise ValueError("request must be reserved exactly once")
        r.state, r.worker_id, r.submitted_s = "SUBMITTED", worker_id, now_s
        self._log("request_submitted", now_s, id=request_id, worker=worker_id)

    def on_event(self, event, now_s):
        if event.visible_s > now_s:
            raise ValueError("control event is not visible yet")
        if event.event_id in self.seen_events:
            return
        r = self.records[event.request_id]
        if r.state not in ("SUBMITTED", "FINISHED") or event.worker_id != r.worker_id:
            raise ValueError("control event before submission or wrong worker")
        self.seen_events.add(event.event_id)
        kind, rid = event.kind, event.request_id
        if kind == "load_submitted":
            attempt = (rid, event.payload["attempt_id"])
            if "layer_count" in event.payload:
                count,size = event.payload["layer_count"],event.payload["expected_read_bytes"]
                if (type(count) is not int or count<1 or type(size) is not int
                        or size<=0 or size%count or event.payload["submitted_read_bytes"]!=0):
                    raise ValueError("invalid parent layer load")
                self.layer_attempts[attempt] = (count,size)
            if attempt not in self.seen_attempts:
                self.seen_attempts.add(attempt)
                r.load_attempts += 1
                r.submitted_read_bytes += event.payload["submitted_read_bytes"]
                if r.load_submit_s is None:
                    r.load_submit_s = event.occurred_s
                if r.load_attempts > 1 or r.load_done_s is not None:
                    self.invalid_reasons.add("repeated_external_load")
        elif kind == "layer_load_submitted":
            attempt = (rid,event.payload["attempt_id"])
            key = (*attempt,event.payload["layer"])
            spec=self.layer_attempts.get(attempt)
            if (attempt not in self.seen_attempts or spec is None or type(key[2]) is not int
                    or not 0<=key[2]<spec[0]):
                raise ValueError("layer submission without a valid parent attempt")
            if key not in self.seen_layers:
                size = event.payload["submitted_read_bytes"]
                if type(size) is not int or size!=spec[1]//spec[0]:
                    raise ValueError("invalid layer byte count")
                self.seen_layers.add(key)
                r.submitted_read_bytes += size
        elif kind == "load_completed":
            if (rid, event.payload["attempt_id"]) not in self.seen_attempts:
                raise ValueError("load completion without submission")
            attempt=(rid,event.payload["attempt_id"])
            if attempt in self.layer_attempts and sum(key[:2]==attempt for key in self.seen_layers)!=self.layer_attempts[attempt][0]:
                raise ValueError("layer load completion before all layers submitted")
            if r.load_done_s is None:
                r.load_done_s = event.occurred_s
        elif kind == "no_external_load":
            if r.load_attempts:
                raise ValueError("no_external_load after external submission")
            r.no_external_load = True
        elif kind == "first_token":
            if r.first_token_s is None:
                r.first_token_s = event.occurred_s
        elif kind == "request_finished":
            r.state = "FINISHED"
            if r.finish_s is None:
                r.finish_s = event.occurred_s
        # lease_events 模式：first_token 只记录不释放；完成事件兜底释放。
        release = kind == "request_finished" or (
            kind == "first_token" and self.c["release_on"] != "lease_events") or (
            self.c["release_on"] == "load_done" and kind in ("load_completed", "no_external_load"))
        if release and r.lease_open:
            before = self.held
            r.lease_open = False
            r.lease_release_reason = kind
            self._log("admission_released", now_s, id=rid, reason=kind,
                      occurred_s=event.occurred_s, held_before=before, held_after=self.held,
                      threshold_version=self.threshold_version)

    def next_wakeup(self, now_s):
        if (not self.pending or self.c["mode"] not in ("tfs", "quota_fcfs")
                or not self.c["aging_enabled"]):
            return None
        if any(not self.is_heavy(self.records[r].profile) for r in self.pending):
            return None  # 正常 pick 会消费这些请求，无需计时器。
        oldest = min(self.records[r].profile.arrival_s for r in self.pending)
        target = max(oldest + self.c["aging_wait_s"], self.last_exemption_s + self.c["aging_cooldown_s"])
        return target if target > now_s + 1e-12 else None

    def snapshot(self, now_s):
        return AdmissionSnapshot(now_s, self.threshold_version, self.theta, self.cap,
                                 self.held, tuple(sorted(self.pending)))
