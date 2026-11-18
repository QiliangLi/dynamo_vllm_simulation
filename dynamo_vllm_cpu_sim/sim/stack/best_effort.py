"""Request-local availability policy for the real native ingress deployment.

Releasing accounting is not evidence that backend execution or IO has ended.
All ledger mutations run on the central event loop. Transport recovery is local
to the worker client and never replays an ambiguous acquire.
"""
import logging
import math
import os
import time

LOG = logging.getLogger(__name__)


class AvailabilityPolicy:
    def __init__(self, state):
        self.state = state
        self.closed = {}  # request tombstones prevent a late acquire reopening quota
        self.ttl = float(os.environ.get('TFS_PERMIT_TTL_S', '120'))
        self.retention = float(os.environ.get('TFS_TERMINAL_RETENTION_S', '600'))
        if not all(math.isfinite(x) and x > 0 for x in (self.ttl, self.retention)):
            raise ValueError('positive finite TFS permit TTL and retention required')
        self.original_control = state.control
        self.original_admit = state.admit
        state.control = self.control
        state.admit = self.admit
        state.availability = self

    def release(self, rid, reason):
        state, controller = self.state, self.state.controller
        self.closed.setdefault(rid, (time.monotonic(), reason))
        rec = controller.records.get(rid)
        before = controller.held
        if rec is not None:
            controller.pending.discard(rid)
            rec.lease_open = False
            if rec.lease_release_reason is None:
                rec.lease_release_reason = reason
        released = []
        for token, permit in controller.permits.items():
            if permit['id'] == rid and permit['state'] in ('reserved', 'submitted'):
                permit.update(state='released', released_s=state.now(), release_reason=reason)
                released.append(token)
                state.log_event('admission_released', id=rid, permit_id=token,
                                reason=reason, held_before=before, held_after=controller.held,
                                execution_confirmed=False)
        # No first_token/finish timestamps or successful IO are fabricated.
        if released and state.routing_score is not None:
            try:
                state.routing_score.exchange('degrade', state.uuid_by_id[rid])
            except Exception as exc:
                state.log_event('routing_score_degraded', id=rid, error=str(exc))
        state.wakeup.set()

    def control(self, raw, transport='unix'):
        from sim.stack.admission_service import UUID_RE
        import uuid
        match = UUID_RE.search(raw.get('request_id', ''))
        key = uuid.UUID(match.group()).hex if match else None
        rid = self.state.aliases.get(key)
        if rid in self.closed:
            worker = raw.get('worker_id')
            if type(worker) is not int or not 0 <= worker < self.state.config['workers']:
                raise ValueError('invalid worker ID')
            if raw.get('op') in ('acquire', 'force_acquire'):
                self.state.log_event('admission_bypassed', id=rid, reason=self.closed[rid][1])
                return dict(ok=True, granted=False, bypass=True, permit_id=None,
                            reason='request_accounting_closed')
            if raw.get('op') == 'rollback':
                return dict(ok=True, released=True, reason='already_released')
            if raw.get('op') == 'routing_ready':
                return dict(ok=True, ready=True, degraded=True)
        return self.original_control(raw, transport=transport)

    def admit(self):
        now = self.state.now()
        score = self.state.routing_score
        if score is not None and hasattr(score, 'drain_observations'):
            for row in score.drain_observations():
                self.state.log_event(row.pop('event'), **row)
        for permit in tuple(self.state.controller.permits.values()):
            if (permit['state'] in ('reserved', 'submitted')
                    and self.state.clock.to_wall(now - permit['registered_s']) >= self.ttl):
                self.release(permit['id'], 'permit_expired')
        api = getattr(self.state, 'native_ingress_control', None)
        if api is not None:
            api.prune()
        self.original_admit()


class RecoveringControlClient:
    """Best-effort wrapper; an ambiguous RPC is never automatically replayed."""
    def __init__(self, factory, observe=None, retry_s=1.0):
        import concurrent.futures
        self.factory, self.observe, self.retry_s = factory, observe, retry_s
        self.client = None
        self.next_retry = 0.0
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                        thread_name_prefix='tfs-reconnect')
        self.recovery = None
        self.idle_enabled = False
        try:
            self.client = factory()
        except Exception as exc:
            self._failed(exc)

    def _recover(self):
        if self.recovery is not None and self.recovery.done():
            try:
                self.client = self.recovery.result()
                if self.idle_enabled:
                    self.client.enable_idle_bypass()
                LOG.info('TFS control connection recovered')
            except Exception as exc:
                LOG.warning('TFS control reconnect failed: %s', exc)
                self.client = None
                self.next_retry = time.monotonic() + self.retry_s
            self.recovery = None
        if self.client is None and self.recovery is None and time.monotonic() >= self.next_retry:
            self.recovery = self.pool.submit(self.factory)

    def _failed(self, exc):
        LOG.warning('TFS control unavailable; allow request without quota: %s', exc)
        old, self.client = self.client, None
        if old is not None:
            self.pool.submit(old.close)
        self.next_retry = time.monotonic() + self.retry_s

    def check(self):
        self._recover()
        if self.client is not None:
            try:
                self.client.check()
                if self.idle_enabled:
                    self.client.outbox.check()
            except Exception as exc:
                self._failed(exc)

    def enable_idle_bypass(self):
        self.idle_enabled = True
        if self.client is not None:
            self.client.enable_idle_bypass()

    @property
    def outbox(self):
        return self

    def force_admit_async(self, request_id, hint, proof):
        self.check()
        if self.client is None:
            return None, time.monotonic()
        try:
            return self.client.force_admit_async(request_id, hint, proof)
        except Exception as exc:
            self._failed(exc)
            return None, time.monotonic()

    def control(self, op, request_id, **fields):
        self.check()
        if self.client is not None:
            try:
                reply = self.client.control(op, request_id, **fields)
                if not isinstance(reply, dict):
                    raise ValueError('invalid control reply')
                if op == 'acquire' and not reply.get('bypass'):
                    if reply.get('granted') is True:
                        if not isinstance(reply.get('permit_id'), str) or not reply['permit_id']:
                            raise ValueError('grant lacks a permit identity')
                    elif reply.get('granted') is not False or reply.get('reason') != 'full':
                        raise ValueError('unexpected acquire result')
                return reply
            except Exception as exc:
                self._failed(exc)
        return dict(granted=False, bypass=True, permit_id=None, released=False,
                    ready=False, reason='control_unavailable')

    def close(self):
        if self.client is not None:
            self.pool.submit(self.client.close)
        if self.recovery is not None:
            def close_recovered(future):
                try:
                    future.result().close()
                except Exception:
                    pass
            self.recovery.add_done_callback(close_recovered)
        self.pool.shutdown(wait=False)
