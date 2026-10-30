"""Local execution observations, joined only after replay; no ledger connection."""
import atexit
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time
import uuid


class LoadObserver:
    def __init__(self, path, worker):
        self.stream = Path(path).open('a', buffering=1)
        self.worker = worker
        self.attempts = {}
        self.started = set()

    def emit(self, kind, rid, **payload):
        self.stream.write(json.dumps(dict(kind=kind, request_id=rid, worker_id=self.worker,
            occurred_mono=time.monotonic(), payload=payload), separators=(',', ':'))+'\n')

    def submitted(self, rid, byte_count, **metadata):
        attempt = self.attempts.get(rid, 0)+1
        self.attempts[rid] = attempt
        self.emit('load_submitted', rid, attempt_id=attempt,
                  submitted_read_bytes=byte_count, **metadata)

    def layer_submitted(self, rid, layer, byte_count):
        self.emit('layer_load_submitted', rid, attempt_id=self.attempts[rid],
                  layer=layer, submitted_read_bytes=byte_count)

    def completed(self, rid):
        self.emit('load_completed', rid, attempt_id=self.attempts[rid])

    def starting(self, rid):
        if rid not in self.started:
            self.started.add(rid)
            if rid not in self.attempts:
                self.emit('no_external_load', rid)

    def forget(self, rid):
        self.attempts.pop(rid, None)
        self.started.discard(rid)

    def close(self):
        self.stream.close()


_observers = {}


def observer_from_env():
    step_log = os.environ.get('SIM_STACK_STEP_LOG')
    if not step_log:
        return None
    path = Path(step_log).with_suffix('.loads.jsonl')
    key = (os.getpid(), str(path))
    if key not in _observers:
        _observers[key] = LoadObserver(path, int(os.environ.get('SIM_STACK_WORKER_ID', '0')))
        atexit.register(_observers[key].close)
    return _observers[key]


def with_local_observations(status, paths):
    """Return a report copy. Never feed local observations into online admission.

    Old frozen reports are passed through unchanged. New reports retain the raw
    runtime snapshot separately and label the source of their execution fields.
    """
    if status.get('execution_feedback') != 'disabled' or status.get('load_observation_source') == 'offline_worker_logs':
        return status
    result = deepcopy(status)
    aliases = {uuid.UUID(e['engine_uuid']).hex: e['id'] for e in status['events']
               if e['event'] == 'request_correlated'}
    origin, scale = status['origin_monotonic_s'], status['wall_time_scale']
    seen = set()
    for path in paths:
        attempts, layers = {}, set()
        for line in Path(path).read_text().splitlines():
            row = json.loads(line)
            match = re.search(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}', row['request_id'])
            if match is None:
                raise ValueError('local load observation has no engine UUID')
            rid = aliases[uuid.UUID(match.group()).hex]
            rec = result['records'][rid]
            worker = row['worker_id']
            if rec['worker_id'] not in (-1, worker):
                raise ValueError('local observation disagrees with permit worker')
            rec['worker_id'] = worker
            seen.add(rid)
            occurred = (row['occurred_mono']-origin)/scale
            payload, kind = row['payload'], row['kind']
            attempt = (rid, payload.get('attempt_id'))
            if kind == 'load_submitted':
                if attempt in attempts or rec['load_attempts']:
                    raise ValueError('repeated external load in local observations')
                attempts[attempt] = payload
                rec['load_attempts'] += 1
                rec['load_submit_s'] = occurred
                rec['submitted_read_bytes'] += payload['submitted_read_bytes']
            elif kind == 'layer_load_submitted':
                spec = attempts[attempt]
                layer = payload['layer']
                key = (*attempt, layer)
                if key in layers or not 0 <= layer < spec['layer_count']:
                    raise ValueError('duplicate or invalid local layer submission')
                if payload['submitted_read_bytes']*spec['layer_count'] != spec['expected_read_bytes']:
                    raise ValueError('local layer byte count mismatch')
                layers.add(key)
                rec['submitted_read_bytes'] += payload['submitted_read_bytes']
            elif kind == 'load_completed':
                spec = attempts[attempt]
                if ('layer_count' in spec and sum(k[:2] == attempt for k in layers) != spec['layer_count']):
                    raise ValueError('local completion before all layers were submitted')
                if rec['load_done_s'] is not None:
                    raise ValueError('duplicate local load completion')
                rec['load_done_s'] = occurred
            elif kind == 'no_external_load':
                if rec['load_attempts'] or rec['no_external_load']:
                    raise ValueError('invalid local no-load observation')
                rec['no_external_load'] = True
            else:
                raise ValueError('unknown local load observation')
    if seen != set(result['records']):
        raise ValueError('missing local execution observations')
    result['load_observation_source'] = 'offline_worker_logs'
    return result
