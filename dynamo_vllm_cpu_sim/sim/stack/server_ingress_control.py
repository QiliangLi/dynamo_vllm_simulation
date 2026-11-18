"""Metadata-only ingress protocol. No tokenizer, prompt, storage or device access.

This module adapts an existing AdmissionState; it does not replace its fitted
compute model, routing-score connection, controller or worker admission RPC.
All mutations must run on the same asyncio loop as AdmissionState.control.
"""
import hmac
import json
import math
import time
import uuid
from dataclasses import dataclass, field

MAX_BODY_BYTES = 16384
MAX_REGISTRATIONS = 100000
PROTOCOL = "dynamo-ingress-v1"
UNCERTAIN_POLICY = "release-permit-best-effort-v1"


class ProtocolError(ValueError):
    pass


class EpochMismatch(ProtocolError):
    pass


@dataclass
class Registration:
    begin: dict
    rid: str
    arrival_s: float
    snapshot: dict | None = None
    events: dict = field(default_factory=dict)
    terminal: bool = False
    uncertain: bool = False
    degraded: bool = False
    terminal_mono: float | None = None


class IngressControl:
    def __init__(self, state, clock_identity, *, monotonic=time.monotonic,
                 epoch=None, capacity=MAX_REGISTRATIONS):
        if not clock_identity:
            raise ProtocolError("a shared Linux monotonic clock is required")
        if state.config['admission'].get('profile_source') != 'dynamo_tier':
            raise ProtocolError("dynamo_tier profile source is required")
        if state.config['admission'].get('placement') != 'scheduler':
            raise ProtocolError("scheduler admission is required")
        self.state = state
        self.clock_identity = clock_identity
        self.monotonic = monotonic
        self.epoch = epoch or uuid.uuid4().hex
        self.capacity = capacity
        self.registrations = {}
        from sim.stack.best_effort import AvailabilityPolicy
        self.policy = getattr(state, 'availability', None) or AvailabilityPolicy(state)
        state.native_ingress_control = self

    def prune(self):
        """Bound terminal history. Live requests are never evicted for capacity.

        Classification uses retained history: the retention window is explicit
        and logged. The default 600 s exceeds the short comparison runs.
        """
        now = self.monotonic()
        delayed = {item[2].request_id for item in self.state.delayed}
        for key, reg in tuple(self.registrations.items()):
            if (reg.terminal_mono is None
                    and now - reg.begin['arrival_mono'] >= self.policy.ttl + self.policy.retention):
                self.policy.release(reg.rid, 'lifecycle_expired')
                reg.terminal_mono = now
                reg.degraded = True
                self.state.log_event('lifecycle_expired', id=reg.rid, execution_confirmed=False)
            if (reg.terminal_mono is None or now - reg.terminal_mono < self.policy.retention
                    or reg.rid in delayed):
                continue
            rid, state = reg.rid, self.state
            self.policy.release(rid, 'history_expired')
            for mapping in (state.aliases, state.uuid_by_id, state.tier_pending,
                            state.tier_snapshots, state.futures, state.controller.records,
                            state.controller.hints, self.policy.closed):
                mapping.pop(rid, None)
            tokens = {k for k, v in state.controller.permits.items() if v['id'] == rid}
            for token in tokens:
                state.controller.permits.pop(token)
            for identity, token in tuple(state.controller.permit_keys.items()):
                if token in tokens:
                    del state.controller.permit_keys[identity]
            prefix = f'native:{self.epoch}:{key}:'
            state.controller.seen_events.difference_update(
                {v for v in state.controller.seen_events if v.startswith(prefix)})
            del self.registrations[key]
            state.log_event('terminal_history_pruned', id=rid, retention_s=self.policy.retention)
        # Full events remain in the JSONL file; snapshots keep a bounded tail.
        if len(self.state.events) > 10000:
            del self.state.events[:-10000]

    def hello(self):
        self._healthy()
        return dict(protocol=PROTOCOL, epoch=self.epoch,
                    clock_id=self.clock_identity, uncertain_policy=UNCERTAIN_POLICY)

    def _healthy(self):
        if self.state.fatal:
            raise ProtocolError("central admission is stopped: " + self.state.fatal)

    def _fields(self, value, fields):
        if not isinstance(value, dict) or set(value) != set(fields):
            raise ProtocolError("unexpected or missing metadata fields")
        if value['epoch'] != self.epoch:
            raise EpochMismatch("central epoch mismatch")
        rid = value['request_id']
        try:
            if not isinstance(rid, str) or str(uuid.UUID(rid)) != rid:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ProtocolError("canonical request UUID is required") from None

    def _time(self, value, minimum):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ProtocolError("invalid monotonic timestamp")
        if value < minimum or value > self.monotonic():
            raise ProtocolError("timestamp outside request lifetime")
        return self.state.clock.to_model(value - self.state.origin)

    def begin(self, value):
        self.prune()
        self._fields(value, ('epoch', 'request_id', 'attempt_id', 'clock_id',
                             'arrival_mono', 'model'))
        if value['clock_id'] != self.clock_identity:
            raise ProtocolError("monotonic clock identity mismatch")
        try:
            if str(uuid.UUID(value['attempt_id'])) != value['attempt_id']:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ProtocolError("canonical attempt UUID is required") from None
        if not isinstance(value['model'], str) or not 0 < len(value['model']) <= 256:
            raise ProtocolError("invalid model")
        key = value['request_id']
        old = self.registrations.get(key)
        if old is not None:
            if old.begin != value:
                raise ProtocolError("request UUID reused by a different attempt")
            return dict(ok=True, epoch=self.epoch, request_id=key)
        self._healthy()
        arrival_s = self._time(value['arrival_mono'], self.state.origin)
        if self.monotonic() - value['arrival_mono'] > self.policy.retention:
            raise ProtocolError('registration arrival is outside the retained retry window')
        if len(self.registrations) >= self.capacity:
            raise ProtocolError("registration capacity reached; no unsafe eviction")
        identity = uuid.UUID(key)
        rid = identity.hex
        if rid in self.state.aliases or rid in self.state.uuid_by_id:
            raise ProtocolError("request collides with a legacy registration")
        self.registrations[key] = Registration(dict(value), rid, arrival_s)
        self.state.aliases[rid] = rid
        self.state.uuid_by_id[rid] = key
        # Token count is supplied only by the authenticated native tier callback.
        self.state.log_event('request_correlated', id=rid, engine_uuid=key,
                             source=PROTOCOL, arrival_s=arrival_s)
        self.state.wakeup.set()
        return dict(ok=True, epoch=self.epoch, request_id=key)

    def _registered(self, value):
        reg = self.registrations.get(value['request_id'])
        if reg is None:
            raise ProtocolError("unregistered frontend request")
        return reg

    def profile(self, value):
        self._fields(value, ('epoch', 'request_id', 'snapshot'))
        self._healthy()
        reg = self._registered(value)
        if reg.terminal:
            raise ProtocolError("profile received after terminal event")
        snapshot = value['snapshot']
        from sim.stack.tier_profile import validate_snapshot
        snapshot = validate_snapshot(snapshot, self.state.config)
        if snapshot['request_id'] != value['request_id']:
            raise ProtocolError("snapshot request identity mismatch")
        if reg.snapshot is not None and reg.snapshot != snapshot:
            raise ProtocolError("profile changed after classification")
        if reg.snapshot is None:
            self.state.tier_pending[reg.rid] = dict(
                arrival_s=reg.arrival_s, prompt_tokens=snapshot['prompt_tokens'])
        if reg.degraded:
            return self.fallback(reg)
        try:
            result = self.state.resolve_tier_profile(snapshot)
        except Exception as exc:
            reg.snapshot = dict(snapshot)
            reg.degraded = True
            self.policy.release(reg.rid, 'profile_failed')
            self.state.log_event('profile_degraded', id=reg.rid, error=str(exc))
            return self.fallback(reg)
        reg.snapshot = dict(snapshot)
        return dict(result, epoch=self.epoch)

    def fallback(self, reg):
        from sim.stack.storage_hint import attach_http_hint
        hint = dict(version=1, is_heavy_read=False, estimated_remote_read_bytes=0,
                    threshold_version=0)
        annotations = attach_http_hint({}, hint)['nvext']['annotations']
        # Keep a valid EDF envelope when the worker uses EDF. The timestamp
        # orders fallback requests by arrival, not a fabricated model estimate.
        import base64
        value = dict(version=1, arrival_s=reg.arrival_s, deadline_s=reg.arrival_s + 120,
                     reference_service_s=120, origin_monotonic_s=self.state.origin,
                     wall_time_scale=self.state.clock.factor)
        annotations += ['tfs_degraded_v1:profile_unavailable', 'edf_priority_v1:' +
                        base64.urlsafe_b64encode(json.dumps(value).encode()).decode()]
        return dict(epoch=self.epoch, annotations=annotations, is_heavy_read=False,
                    degraded=True)

    def event(self, value):
        fields = ('epoch', 'request_id', 'sequence', 'kind', 'occurred_mono', 'evidence')
        if isinstance(value, dict) and 'attempt_id' in value:
            fields += ('attempt_id',)
        self._fields(value, fields)
        reg = self._registered(value)
        if value.get('attempt_id', reg.begin['attempt_id']) != reg.begin['attempt_id']:
            raise ProtocolError('execution attempt mismatch')
        seq = value['sequence']
        if type(seq) is not int or not 1 <= seq <= 3:
            raise ProtocolError("invalid event sequence")
        if seq in reg.events:
            if reg.events[seq] != value:
                raise ProtocolError("conflicting event retry")
            return dict(ok=True, epoch=self.epoch, sequence=seq)
        if reg.terminal and value['kind'] != 'first_token':
            raise ProtocolError('post-terminal event')
        kind, evidence = value['kind'], value['evidence']
        allowed = {
            'first_token': 'nonempty_text',
            'request_finished': 'finish_reason',
            'uncertain': 'execution_not_confirmed',
        }
        if kind not in allowed or evidence != allowed[kind]:
            raise ProtocolError("invalid lifecycle evidence")
        if any(e['kind'] == kind for e in reg.events.values()):
            raise ProtocolError('lifecycle kind already reported')
        occurred_s = self._time(value['occurred_mono'], reg.begin['arrival_mono'])
        for other_seq, other in reg.events.items():
            if ((seq < other_seq and value['occurred_mono'] > other['occurred_mono'])
                    or (seq > other_seq and value['occurred_mono'] < other['occurred_mono'])):
                raise ProtocolError('lifecycle timestamp contradicts sequence')
        if kind == 'first_token' and seq != 1:
            raise ProtocolError('first token must be sequence 1')
        if kind != 'uncertain' and reg.snapshot is None:
            raise ProtocolError("execution event arrived before profile")
        if kind == 'uncertain':
            reg.uncertain = True
            self.policy.release(reg.rid, 'upstream_uncertain')
            self.state.log_event('upstream_uncertain', id=reg.rid,
                                 source=PROTOCOL, scope='request',
                                 error='native frontend execution state is uncertain',
                                 reconciliation_required=False,
                                 permit_policy='release_immediately')
            self.state.wakeup.set()
        else:
            from sim.control_events import ControlEvent
            record = self.state.controller.records.get(reg.rid)
            now = self.state.now()
            event = ControlEvent(
                f"native:{self.epoch}:{value['request_id']}:{seq}",
                kind, reg.rid, record.worker_id if record is not None else -1, occurred_s,
                now + self.state.config['admission']['feedback_delay_s'])
            try:
                if record is not None:
                    self.state.queue_event(event)
                else:
                    self.state.log_event('feedback_received', id=reg.rid, kind=kind,
                                         event_id=event.event_id, degraded=True)
            except Exception as exc:
                self.state.log_event('lifecycle_delivery_degraded', id=reg.rid, error=str(exc))
            if reg.rid not in self.policy.closed:
                self.policy.release(reg.rid, kind)
        reg.events[seq] = dict(value)
        if kind in ('request_finished', 'uncertain'):
            reg.terminal = True
            reg.terminal_mono = self.monotonic()
        return dict(ok=True, epoch=self.epoch, sequence=seq)


def install_routes(app, state, secret):
    """Attach to the existing app; no modification to its legacy routes."""
    from fastapi import HTTPException, Request
    from sim.stack.admission_rpc import clock_id
    if not isinstance(secret, str) or not secret:
        raise ProtocolError("internal authentication token is required")
    control = IngressControl(state, clock_id())
    app.state.native_ingress = control

    def authenticate(request):
        supplied = request.headers.get('x-tier-profile-token', '')
        if not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(403, 'invalid internal ingress identity')

    async def read_body(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BODY_BYTES:
                raise HTTPException(413, 'metadata message too large')
        try:
            return json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'invalid metadata JSON') from None

    @app.get('/internal/dynamo-ingress/hello')
    async def hello(request: Request):
        authenticate(request)
        try:
            return control.hello()
        except ProtocolError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post('/internal/dynamo-ingress/{operation}')
    async def operation(operation: str, request: Request):
        authenticate(request)
        functions = dict(begin=control.begin, profile=control.profile,
                         event=control.event)
        if operation not in functions:
            raise HTTPException(404, 'unknown ingress operation')
        value = await read_body(request)
        try:
            return functions[operation](value)
        except EpochMismatch as exc:
            raise HTTPException(409, dict(code='epoch_mismatch', message=str(exc))) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return control
