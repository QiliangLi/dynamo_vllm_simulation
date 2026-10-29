"""Frozen arrival classification and atomic, idempotent late read permits.

All calls run on the admission service's single asyncio event-loop thread.
No await occurs inside a ledger operation. This is not a distributed database.
"""
import uuid

from sim.admission import AdmissionController
from sim.sa_admission import SAAdmission


class FrozenClassification:
    def __init__(self, config):
        self.hints = {}
        self.permits = {}
        self.active_permit_by_request = {}
        self.permit_keys = {}
        self.placement = config.get("placement", "gateway")
        super().__init__(config)

    def is_heavy(self, profile):
        hint = self.hints.get(profile.request_id)
        return hint["is_heavy_read"] if hint is not None else super().is_heavy(profile)

    def on_arrivals(self, profiles, now_s):
        profiles = list(profiles)
        super().on_arrivals(profiles, now_s)
        for profile in profiles:
            hint = dict(version=1,
                        is_heavy_read=bool(profile.estimated_read_bytes > 0 and super().is_heavy(profile)),
                        estimated_remote_read_bytes=profile.estimated_read_bytes,
                        threshold_version=self.threshold_version)
            self.hints[profile.request_id] = hint
            self._log("storage_hint_created", now_s, id=profile.request_id, hint=hint)

    def dispatch_unreserved(self, rid, now_s):
        rec = self.records[rid]
        if rec.state != "QUEUED":
            raise ValueError("request already dispatched")
        self.pending.remove(rid)
        rec.state, rec.worker_id, rec.submitted_s = "SUBMITTED", -1, now_s
        self._log("request_submitted", now_s, id=rid, worker=-1, reserved=False)

    def acquire(self, rid, worker, key, attempt, hint, now_s, *, force=False,
                permit_id=None, decision_s=None, proof=None):
        if self.placement != "scheduler":
            raise ValueError("late permits are disabled")
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
            if force and (not permit.get('forced') or permit['permit_id'] != permit_id):
                raise ValueError('forced replay identity changed')
            return dict(granted=permit["state"] in (("reserved", "submitted", "released")
                                                  if force else ("reserved", "submitted")),
                        permit_id=permit["permit_id"], reason=permit["state"], replay=True)
        if force:
            if not self.c.get('idle_bypass'):
                raise ValueError('idle bypass is disabled')
            if (not isinstance(permit_id, str) or len(permit_id) != 32
                    or uuid.UUID(hex=permit_id).hex != permit_id or permit_id in self.permits):
                raise ValueError('invalid forced permit identity')
            if any(p.get('forced') and p['worker'] == worker
                   and p['state'] in ('reserved', 'submitted') for p in self.permits.values()):
                raise ValueError('worker already holds a forced permit')
            if (proof is None or proof.get('running_before') != 0
                    or proof.get('scheduled_tokens_before') != 0
                    or proof.get('nonheavy_candidates') != 0
                    or proof.get('allocated') is not True
                    or proof.get('external_tokens', 0) <= 0
                    or proof.get('heavy_candidates', 0) < 1
                    or decision_s is None or not rec.profile.arrival_s <= decision_s <= now_s + .001):
                raise ValueError('invalid allocated idle-bypass decision')
        if rec.state != "SUBMITTED" or rec.load_attempts or rec.lease_open:
            raise ValueError("request cannot acquire another permit")
        if rec.worker_id == -1:
            rec.worker_id = worker
            self._log("worker_assigned", now_s, id=rid, worker=worker)
        if not force and self.cap is not None and self.held >= self.cap:
            self._log("read_permit_denied", now_s, id=rid, worker=worker,
                      reason="full", held=self.held, cap=self.cap)
            return dict(granted=False, permit_id=None, reason="full")
        before = self.held
        token = permit_id if force else uuid.uuid4().hex
        grant_time = decision_s if force else now_s
        permit = dict(permit_id=token, id=rid, worker=worker, attempt=attempt,
                      acquisition_key=key, state="reserved", granted_s=grant_time,
                      registered_s=now_s, forced=force)
        if rid in self.active_permit_by_request:
            raise RuntimeError("request already has an active permit index entry")
        self.permits[token] = permit
        self.active_permit_by_request[rid] = token
        self.permit_keys[identity] = token
        rec.reserved_s, rec.lease_open, rec.lease_release_reason = grant_time, True, None
        self._log("admission_reserved", now_s, id=rid, worker=worker, permit_id=token,
                  heavy=True, held_before=before, held_after=self.held, cap=self.cap,
                  theta=self.theta, threshold_version=self.threshold_version,
                  mode=self.c["mode"], reason="idle_bypass" if force else "normal", placement="scheduler",
                  decision_s=grant_time, proof=proof, over_cap=self.cap is not None and self.held > self.cap,
                  release_on=self.c["release_on"])
        return dict(granted=True, permit_id=token, reason="granted", replay=False)

    def rollback(self, rid, worker, token, now_s):
        permit = self.permits[token]
        if permit["id"] != rid or permit["worker"] != worker:
            raise ValueError("wrong permit owner")
        if permit["state"] == "rolled_back":
            return
        if permit["state"] != "reserved":
            raise ValueError("cannot roll back submitted or released IO")
        rec = self.records[rid]
        before = self.held
        permit["state"], permit["released_s"] = "rolled_back", now_s
        if self.active_permit_by_request.pop(rid, None) != token:
            raise RuntimeError("permit index disagrees with rollback owner")
        rec.lease_open = False
        rec.lease_release_reason = "allocation_failed"
        self._log("admission_released", now_s, id=rid, permit_id=token,
                  reason="allocation_failed", held_before=before, held_after=self.held)

    def on_event(self, event, now_s):
        if event.event_id in self.seen_events:
            return
        rec = self.records[event.request_id]
        token = self.active_permit_by_request.get(event.request_id)
        active = [self.permits[token]] if token is not None else []
        if self.placement == "scheduler" and event.kind == "load_submitted" and self.is_heavy(rec.profile):
            if len(active) != 1 or not rec.lease_open or event.occurred_s < active[0]["granted_s"]:
                raise ValueError("heavy read submitted without a valid permit")
        super().on_event(event, now_s)
        for permit in active:
            if event.kind == "load_submitted":
                permit["state"] = "submitted"
            if not rec.lease_open:
                permit.update(state="released", released_s=now_s, release_reason=event.kind)
                if self.active_permit_by_request.pop(event.request_id, None) != permit["permit_id"]:
                    raise RuntimeError("permit index disagrees with release owner")


class FrozenAdmission(FrozenClassification, AdmissionController):
    pass


class FrozenSAAdmission(FrozenClassification, SAAdmission):
    pass
