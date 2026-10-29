"""Client for the opt-in native default-scorer waiting-load ledger.

One run, one frontend, one model, block=1/16, cache-blind aggregated routing.
The score-control connection never changes native capacity reservations.
"""
import json
import math
import os
import re
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit


def normalize(config):
    value = dict(enabled=False, waiting_factor=0.1, rpc_timeout_wall_s=2.0)
    value.update(config.get('routing_accounting', {}))
    if type(value['enabled']) is not bool:
        raise ValueError('routing_accounting.enabled must be boolean')
    factor = value['waiting_factor']
    if type(factor) not in (int, float) or not math.isfinite(factor) or not 0 <= factor <= 1:
        raise ValueError('routing_accounting.waiting_factor must be finite in [0,1]')
    timeout = value['rpc_timeout_wall_s']
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('routing score RPC timeout must be finite and positive')
    if value['enabled'] and (config.get('router_mode') != 'kv'
            or config['admission']['placement'] != 'scheduler'
            or config.get('block_size') not in (1, 16) or config.get('enable_prefix_caching', False)):
        raise ValueError('weighted accounting requires kv router, scheduler admission, block=1/16 and prefix caching off')
    config['routing_accounting'] = value


class ScoreClient:
    def __init__(self, identity_path, factor, timeout):
        self.path = Path(identity_path)
        self.factor = factor
        self.timeout = timeout
        self.lock = threading.Lock()
        self.sock = self.reader = None
        self.identity = None
        self.pid = os.getpid()
        self.failed = False

    def _connect(self):
        identity = json.loads(self.path.read_text())
        if identity.get('protocol') != 'waiting-score-v1' or identity.get('factor') != self.factor:
            raise ValueError('native score feature or configured factor mismatch')
        address = urlsplit(identity['endpoint'])
        if address.scheme != 'tcp' or address.hostname != '127.0.0.1' or not address.port:
            raise ValueError('native score control must use its run-local loopback endpoint')
        self.sock = socket.create_connection((address.hostname,address.port),self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        self.reader = self.sock.makefile('rb')
        self.identity = identity

    def exchange(self, op, request_id=None, *, allow_closed=False, **fields):
        with self.lock:
            if self.failed or self.pid != os.getpid():
                raise ConnectionError('closed or inherited score client')
            started = time.monotonic_ns()
            try:
                if self.sock is None:
                    self._connect()
                message = dict(op=op,epoch=self.identity['epoch'],**fields)
                if request_id is not None:
                    message['request_id'] = request_id
                def roundtrip(body):
                    self.sock.sendall((json.dumps(body)+'\n').encode())
                    limit = 128*1024*1024 # bounded final diagnostic ledger
                    line = self.reader.readline(limit+1)
                    if not line.endswith(b'\n') or len(line)>limit:
                        raise ConnectionError('invalid score reply frame')
                    return json.loads(line)
                result = roundtrip(message)
                if (allow_closed and op == 'activate' and result.get('ok') is False
                        and result.get('error') == 'stale activation or request is not waiting'):
                    # A fast request can finish before its async notice reaches
                    # the scorer. Read its terminal tombstone; never reactivate
                    # it, retry a mutation, or ignore another kind of failure.
                    snap = roundtrip(dict(op='snapshot',epoch=self.identity['epoch']))
                    canonical = re.search(r'[0-9a-f]{32}',request_id.replace('-','').lower()).group()
                    events = [e for e in snap.get('data',{}).get('events',[]) if e['id']==canonical]
                    if (snap.get('ok') is True and events and events[-1]['event']=='closed'
                            and snap['data']['epoch']==self.identity['epoch']):
                        result = dict(ok=True,already_closed=True,data=dict(phase='Closed'))
                if result.get('ok') is not True:
                    raise RuntimeError(f'native score operation rejected: {result}')
                result['duration_wall_ns'] = time.monotonic_ns()-started
                return result
            except BaseException:
                self.failed = True
                self.close()
                raise

    def snapshot(self):
        if self.failed:
            return dict(connected=False,failed=True)
        if self.identity is None and not self.path.exists():
            return dict(connected=False)
        return dict(connected=True,**self.exchange('snapshot')['data'])

    def close(self):
        if self.reader is not None:
            self.reader.close()
        if self.sock is not None:
            self.sock.close()
        self.reader = self.sock = None
