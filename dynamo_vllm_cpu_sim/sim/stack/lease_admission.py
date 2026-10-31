"""带宽派生轻重线 + 每盘租约账本的调度前准入控制器（tfs-lease 第四臂）。

差异于 v1 冻结分类（P75）：
- 轻重线只由盘带宽与容差系数决定，与负载统计无关，到达时冻结进 hint v2。
- 许可判定按 B/s 份额走 LeaseLedger 五步放行，不再用条数 cap。
- 租约在 lease_completed（末层读毕）/回滚/TTL/完成兜底时释放；
  first_token 只记录、不释放（release_on=lease_events）。
三臂（direct/tfs-a01/tfs-a1）不经过本类，行为不变。
"""
import math
import uuid

from sim.admission import AdmissionController
from sim.stack.read_admission import FrozenClassification
from sim.stack.lease_ledger import LeaseEntry, LeaseLedger


class FrozenLeaseAdmission(FrozenClassification, AdmissionController):
    def __init__(self, config):
        super().__init__(config)
        lease = dict(config["lease"])
        self.block_size = config["block_size"]
        self.ledger = LeaseLedger(lease, config["disk_Bps"], lease["disks"])
        self.lease_events_seen = set()

    # ------------------------------------------------------------------
    # 到达：v2 hint（带宽派生线）
    # ------------------------------------------------------------------
    def on_arrivals(self, profiles, now_s):
        profiles = list(profiles)
        # 跳过 v1 hint 构建，直接用 AdmissionController（含预热排除补丁）登记历史。
        super(FrozenClassification, self).on_arrivals(profiles, now_s)
        for profile in profiles:
            pressure = profile.pressure_Bps if profile.estimated_read_bytes > 0 else 0.0
            heavy = pressure > self.ledger.heavy_line_Bps
            blocks = profile.estimated_remote_tokens // self.block_size if heavy else 0
            hint = dict(version=2,
                        is_heavy_read=bool(heavy),
                        estimated_remote_read_bytes=profile.estimated_read_bytes,
                        threshold_version=self.threshold_version,
                        demand_Bps=float(pressure),
                        remote_blocks=int(blocks),
                        per_disk_shares=[{"disk": 0, "share": 1.0}])
            self.hints[profile.request_id] = hint
            self._log("storage_hint_created", now_s, id=profile.request_id, hint=hint)

    # ------------------------------------------------------------------
    # 等带宽未决（条件 b 的紧迫集）
    # ------------------------------------------------------------------
    def urgent_pending(self, exclude_rid, now_s):
        delta = self.ledger.delta_s()
        if delta is None:
            return []
        urgents = []
        for rid, rec in self.records.items():
            if rid == exclude_rid or rec.state not in ("SUBMITTED",):
                continue
            hint = self.hints.get(rid)
            if hint is None or not hint.get("is_heavy_read"):
                continue
            if any(p["id"] == rid and p["state"] != "rolled_back" for p in self.permits.values()):
                continue
            profile = rec.profile
            if profile.deadline_s - now_s - profile.reference_service_s >= delta:
                continue
            urgents.append(dict(rid=rid, share_Bps=hint["demand_Bps"],
                                deadline_s=profile.deadline_s,
                                service_s=profile.reference_service_s))
        return urgents

    # ------------------------------------------------------------------
    # 申请：五步放行
    # ------------------------------------------------------------------
    def acquire(self, rid, worker, key, attempt, hint, now_s, *, force=False,
                permit_id=None, decision_s=None, proof=None):
        if force:
            raise ValueError("lease admission does not support forced permits")
        rec = self.records[rid]
        if type(worker) is not int or worker < 0 or attempt != 1 or type(attempt) is not int:
            raise ValueError("unsupported worker or load attempt")
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("invalid acquisition key")
        if hint != self.hints[rid] or not hint["is_heavy_read"]:
            raise ValueError("storage_hint mismatch or unnecessary light request RPC")
        if rec.worker_id not in (-1, worker):
            raise ValueError("request worker changed")
        identity = (rid, worker, attempt, key)
        if identity in self.permit_keys:
            permit = self.permits[self.permit_keys[identity]]
            return dict(granted=permit["state"] in ("reserved", "submitted"),
                        permit_id=permit["permit_id"], reason=permit["state"],
                        replay=True, ttl_s=permit.get("ttl_s"), path=permit.get("path"))
        terminal = rec.first_token_s if rec.first_token_s is not None else rec.finish_s
        if (rec.state != "SUBMITTED" or rec.lease_open or terminal is not None
                or any(p['id'] == rid and p['state'] != 'rolled_back' for p in self.permits.values())):
            raise ValueError("request cannot acquire another permit")
        if rec.worker_id == -1:
            rec.worker_id = worker
            self._log("worker_assigned", now_s, id=rid, worker=worker)
        profile = rec.profile
        entry = LeaseEntry(rid, worker, None, 0, hint["demand_Bps"],
                           max(1, hint["remote_blocks"]), profile.estimated_read_bytes,
                           profile.standalone_compute_s, profile.reference_service_s,
                           profile.deadline_s, now_s, math.inf)
        decision = self.ledger.evaluate(entry, self.urgent_pending(rid, now_s), now_s)
        if not decision["granted"]:
            self._log("read_permit_denied", now_s, id=rid, worker=worker,
                      reason=decision["reason"], detail=decision.get("detail"),
                      sum_after=decision.get("sum_after"), k=decision.get("k"),
                      heavy_sum=self.ledger.heavy_sum(),
                      light_rate=self.ledger.light_rate_Bps)
            return dict(granted=False, permit_id=None, reason=decision["reason"],
                        detail=decision.get("detail"))
        token = uuid.uuid4().hex
        entry.permit_id = token
        self.ledger.admit(entry)
        permit = dict(permit_id=token, id=rid, worker=worker, attempt=attempt,
                      acquisition_key=key, state="reserved", granted_s=now_s,
                      registered_s=now_s, forced=False, path=entry.path,
                      ttl_s=decision.get("ttl_s"), share_Bps=entry.share_Bps,
                      started_s=None, completed_s=None)
        self.permits[token] = permit
        self.permit_keys[identity] = token
        rec.reserved_s, rec.lease_open, rec.lease_release_reason = now_s, True, None
        self._log("admission_reserved", now_s, id=rid, worker=worker, permit_id=token,
                  heavy=True, held_before=self.ledger.heavy_sum() - entry.share_Bps,
                  held_after=self.ledger.heavy_sum(), cap=None,
                  theta=self.theta, threshold_version=self.threshold_version,
                  mode=self.c["mode"], reason="lease", placement="scheduler",
                  release_on=self.c["release_on"], lease_path=entry.path,
                  lease_sum_after=decision.get("sum_after"), lease_k=decision.get("k"),
                  lease_ttl_s=decision.get("ttl_s"))
        return dict(granted=True, permit_id=token, reason="granted", replay=False,
                    ttl_s=decision.get("ttl_s"), path=entry.path)

    # ------------------------------------------------------------------
    # 事件：读开始 / 末层读毕
    # ------------------------------------------------------------------
    def _permit_of(self, rid, worker, permit_id):
        permit = self.permits.get(permit_id)
        if permit is None or permit["id"] != rid or permit["worker"] != worker:
            raise ValueError("lease event does not match an owned permit")
        return permit

    def lease_started(self, rid, worker, permit_id, occurred_s, now_s, event_id):
        if event_id in self.lease_events_seen:
            return dict(started=True, replay=True)
        self.lease_events_seen.add(event_id)
        permit = self._permit_of(rid, worker, permit_id)
        if permit["state"] not in ("reserved", "submitted"):
            # 请求已完成兜底释放或 TTL 回收后，迟到的读开始事件只记录不报错。
            self._log("lease_started_late", now_s, id=rid, worker=worker,
                      permit_id=permit_id, permit_state=permit["state"])
            return dict(started=True, replay=False, late=True)
        entry = self.ledger.open.get(rid)
        if entry is None:
            raise ValueError("lease start without an open ledger entry")
        permit["state"], permit["started_s"] = "submitted", occurred_s
        entry.started_s = occurred_s
        self.ledger.counters["started"] += 1
        self._log("lease_started", now_s, id=rid, worker=worker, permit_id=permit_id,
                  occurred_s=occurred_s, grant_to_start_s=occurred_s - permit["granted_s"],
                  heavy_sum=self.ledger.heavy_sum())
        return dict(started=True, replay=False)

    def _release(self, rid, permit, reason, now_s, occurred_s=None, rolled=False):
        entry = self.ledger.close(rid)
        rec = self.records[rid]
        before = self.ledger.heavy_sum() + (entry.share_Bps if entry else 0.0)
        permit.update(state="rolled_back" if rolled else "released",
                      released_s=now_s, release_reason=reason)
        rec.lease_open = False
        rec.lease_release_reason = reason
        self._log("admission_released", now_s, id=rid, permit_id=permit["permit_id"],
                  reason=reason, occurred_s=occurred_s if occurred_s is not None else now_s,
                  held_before=before, held_after=self.ledger.heavy_sum(),
                  started_s=permit.get("started_s"))

    def lease_completed(self, rid, worker, permit_id, occurred_s, now_s, event_id):
        if event_id in self.lease_events_seen:
            return dict(completed=True, replay=True)
        self.lease_events_seen.add(event_id)
        permit = self._permit_of(rid, worker, permit_id)
        if permit["state"] not in ("reserved", "submitted"):
            # 请求已完成兜底释放或 TTL 回收后，迟到的读毕事件只记录，不重复减账。
            self._log("lease_completed_late", now_s, id=rid, worker=worker,
                      permit_id=permit_id, permit_state=permit["state"],
                      occurred_s=occurred_s)
            self.ledger.counters["completed"] += 1
            return dict(completed=True, replay=False, late=True)
        permit["completed_s"] = occurred_s
        self.ledger.counters["completed"] += 1
        self._release(rid, permit, "lease_completed", now_s, occurred_s)
        self._log("lease_completed", now_s, id=rid, worker=worker, permit_id=permit_id,
                  occurred_s=occurred_s, heavy_sum=self.ledger.heavy_sum())
        return dict(completed=True, replay=False)

    def rollback(self, rid, worker, token, now_s):
        permit = self.permits[token]
        if permit["id"] != rid or permit["worker"] != worker:
            raise ValueError("wrong permit owner")
        if permit["state"] == "rolled_back":
            return
        if permit["state"] != "reserved":
            raise ValueError("cannot roll back submitted or released IO")
        self._release(rid, permit, "allocation_failed", now_s, rolled=True)

    # ------------------------------------------------------------------
    # 兜底：TTL 与完成事件
    # ------------------------------------------------------------------
    def on_event(self, event, now_s):
        if event.event_id in self.seen_events:
            return
        active = [p for p in self.permits.values() if p["id"] == event.request_id
                  and p["state"] in ("reserved", "submitted")]
        super().on_event(event, now_s)
        rec = self.records[event.request_id]
        # release_on=lease_events 下基类只在 request_finished 释放租约；
        # 这里同步关闭账本行（close 幂等，重复事件无害）。
        if active and not rec.lease_open:
            self.ledger.close(event.request_id)
            self._log("lease_closed_by_event", now_s, id=event.request_id,
                      permit_id=active[0]["permit_id"],
                      reason=rec.lease_release_reason, kind=event.kind)

    def lease_sweep(self, now_s):
        expired = self.ledger.sweep_ttl(now_s)
        for rid in expired:
            permit = next(p for p in self.permits.values()
                          if p["id"] == rid and p["state"] in ("reserved", "submitted"))
            self._release(rid, permit, "lease_expired", now_s)
            self._log("lease_expired", now_s, id=rid, permit_id=permit["permit_id"],
                      expires_s=permit.get("ttl_s"))
        return expired

    def lease_reconcile(self, rate_Bps, now_s):
        row = self.ledger.reconcile(rate_Bps, now_s)
        self._log("lease_reconcile", now_s, **row)
        return row

    def lease_next_deadline(self, now_s):
        return self.ledger.next_deadline(now_s)

    def lease_snapshot(self):
        return self.ledger.snapshot()
