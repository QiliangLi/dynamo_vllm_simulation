"""Fixed absolute EDF deadline ordering through vLLM's native priority heap.

The gateway's arrived-request profile and monotonic origin are carried on the
existing annotations/extra_args path. Every schedule call verifies/rebuilds the same fixed key for waiting, skipped
and running requests. No remaining-time subtraction, deadline rejection, or
alternate worker picker is used.
"""
import base64
import functools
import heapq
import json
import math
import os
import time
from sim.priority import normalize_ordering, edf_priority as deadline_key

PREFIX = 'edf_priority_v1:'
FIELDS = {'version', 'arrival_s', 'deadline_s', 'reference_service_s',
          'origin_monotonic_s', 'wall_time_scale'}


def enabled(config):
    return normalize_ordering(dict(config)) == 'edf'


def validate(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('invalid EDF metadata fields')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('invalid EDF version')
    for name in FIELDS - {'version'}:
        if type(value[name]) not in (int, float) or not math.isfinite(value[name]):
            raise ValueError('invalid EDF number: ' + name)
    if (value['arrival_s'] < 0 or value['reference_service_s'] <= 0
            or value['deadline_s'] <= value['arrival_s']
            or value['origin_monotonic_s'] <= 0 or value['wall_time_scale'] < 1):
        raise ValueError('invalid EDF bounds')
    return dict(value)


def attach_http(body, profile, origin, scale):
    result = dict(body)
    nvext = dict(result.get('nvext') or {})
    annotations = list(nvext.get('annotations') or [])
    if any(not isinstance(a, str) or a.startswith((PREFIX,'budget_priority_v1:')) for a in annotations):
        raise ValueError('caller cannot supply EDF metadata')
    value = validate(dict(version=1, arrival_s=profile.arrival_s,
        deadline_s=profile.deadline_s, reference_service_s=profile.reference_service_s,
        origin_monotonic_s=origin, wall_time_scale=scale))
    annotations.append(PREFIX + base64.urlsafe_b64encode(
        json.dumps(value, separators=(',', ':')).encode()).decode())
    nvext['annotations'] = annotations
    result['nvext'] = nvext
    return result, value


def from_annotations(request):
    values = [a[len(PREFIX):] for a in request.get('annotations', [])
              if isinstance(a, str) and a.startswith(PREFIX)]
    if len(values) != 1 or len(values[0]) > 4096:
        raise ValueError('exactly one EDF annotation required')
    return validate(json.loads(base64.b64decode(values[0], altchars=b'-_', validate=True)))


def calculate(value, now_wall_s):
    """时钟仅用于观测；排序只使用冻结的绝对 deadline。"""
    value=validate(value)
    now_s=(now_wall_s-value['origin_monotonic_s'])/value['wall_time_scale']
    return now_s,value['deadline_s'],deadline_key(value['deadline_s'])


def metadata(request):
    return validate((request.sampling_params.extra_args or {}).get('edf_priority'))


def refresh(scheduler, now_wall_s):
    from vllm.v1.core.sched.request_queue import PriorityRequestQueue
    queues = [scheduler.waiting, scheduler.skipped_waiting]
    if any(not isinstance(q, PriorityRequestQueue) for q in queues):
        raise RuntimeError('EDF ordering requires real vLLM PriorityRequestQueue')
    rows, seen = [], set()
    for source, requests in [('running', scheduler.running),
                             ('waiting', queues[0]._heap),
                             ('skipped_waiting', queues[1]._heap)]:
        for request in requests:
            if request.request_id in seen:
                raise RuntimeError('request appears in multiple scheduler queues')
            seen.add(request.request_id)
            value = metadata(request)
            now_s, sort_deadline_s, priority = calculate(value, now_wall_s)
            request.priority = priority
            rows.append(dict(request_id=request.request_id, queue=source,
                now_s=now_s, deadline_s=value['deadline_s'],
                T0_s=value['reference_service_s'], sort_deadline_s=sort_deadline_s,
                priority=priority, arrival_s=value['arrival_s']))
    for queue in queues:
        # Keep the native heap valid when restoring keys after queue transitions.
        heapq.heapify(queue._heap)
    heads = {name: q.peek_request().request_id if q else None
             for name, q in zip(('waiting', 'skipped_waiting'), queues)}
    return rows, heads


def emit(event, **values):
    if os.environ.get('SIM_FINE_TRACE_DIR'):
        from sim.stack.fine_trace import emit as trace
        trace(event, **values)


def install():
    from dynamo.vllm import handlers
    from vllm.v1.core.sched.scheduler import Scheduler
    if getattr(Scheduler.schedule, '_edf_priority', False):
        return
    original_params = handlers.build_sampling_params

    @functools.wraps(original_params)
    def build_sampling_params(request, *args, **kwargs):
        value = from_annotations(request)
        params = original_params(request, *args, **kwargs)
        params.extra_args = {**(params.extra_args or {}), 'edf_priority': value}
        return params

    original_init, original_add, original_schedule = (
        Scheduler.__init__, Scheduler.add_request, Scheduler.schedule)

    @functools.wraps(original_init)
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if getattr(self.policy, 'value', self.policy) != 'priority':
            raise RuntimeError('native vLLM scheduler policy is not priority')
        self._edf_priority_round = 0
        emit('edf_priority_policy', policy='priority',
             queue_type=type(self.waiting).__name__, formula='deadline',
             priority_unit='model_nanosecond', refresh='every_schedule')

    @functools.wraps(original_add)
    def add_request(self, request):
        value = metadata(request)
        now_s, sort_deadline_s, priority = calculate(value, time.monotonic())
        request.priority = priority
        emit('edf_priority_received', rid=request.request_id,
             metadata=value, now_s=now_s, sort_deadline_s=sort_deadline_s, priority=priority)
        return original_add(self, request)

    @functools.wraps(original_schedule)
    def schedule(self):
        now = time.monotonic()  # One sampled wall clock for the whole round.
        rows, heads = refresh(self, now)
        self._edf_priority_round += 1
        if rows:
            emit('edf_priority_refresh', round=self._edf_priority_round,
                 now_monotonic_s=now, rows=rows, heads=heads)
        return original_schedule(self)

    schedule._edf_priority = True
    handlers.build_sampling_params = build_sampling_params
    Scheduler.__init__, Scheduler.add_request, Scheduler.schedule = initialize, add_request, schedule
