"""Quiescent-run audit of background response drains; no hot-path disk writes."""
import json
import os
from pathlib import Path
import signal
import threading
import time

_clients = []


def write_status(*_):
    folder = Path(os.environ['SIM_STACK_ADMISSION_STATUS_DIR'])
    statuses = []
    for client in _clients:
        try:
            status = client.settle()
        except BaseException as error:
            status = dict(client.streams.snapshot(), status='failed', error=repr(error))
        statuses.append(dict(instance_id=client.instance_id, **status))
    result = dict(pid=os.getpid(), status='passed' if all(s['status']=='passed' for s in statuses) else 'failed',
                  clients=statuses)
    temporary = folder/f'client-{os.getpid()}.tmp'
    temporary.write_text(json.dumps(result)+'\n')
    temporary.replace(folder/f'client-{os.getpid()}.json')


def register(client):
    directory = os.environ.get('SIM_STACK_ADMISSION_STATUS_DIR')
    if not directory:
        return
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('admission audit signal must be installed on the owning main thread')
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    if not _clients:
        if signal.getsignal(signal.SIGUSR2) not in (signal.SIG_DFL, write_status):
            raise RuntimeError('SIGUSR2 already has a handler; cannot install admission drain audit')
        signal.signal(signal.SIGUSR2, write_status)
    _clients.append(client)
    (folder/f'ready-{os.getpid()}.json').write_text(json.dumps(dict(pid=os.getpid(),
        worker=os.environ.get('SIM_STACK_WORKER_ID')))+'\n')


def collect(folder, timeout_s, *, expected_workers=None):
    folder = Path(folder)
    records = [json.loads(p.read_text()) for p in folder.glob('ready-*.json')]
    if expected_workers is not None:
        workers = sorted(int(r['worker']) for r in records)
        if workers != list(range(expected_workers)):
            raise RuntimeError(f'admission drain audit clients missing or duplicated: {workers}')
    for record in records:
        (folder/f'client-{record["pid"]}.json').unlink(missing_ok=True)
        os.kill(record['pid'], signal.SIGUSR2)
    deadline = time.monotonic()+timeout_s
    while True:
        paths = [folder/f'client-{r["pid"]}.json' for r in records]
        if all(p.is_file() for p in paths):
            results = [json.loads(p.read_text()) for p in paths]
            for result in results:
                if result['status'] != 'passed':
                    raise RuntimeError(f'background admission RPC audit failed: {result}')
                for client in result['clients']:
                    if (client['pending_streams'] or client['failed_streams']
                            or client['final_replies'] != client['drained_streams']):
                        raise RuntimeError(f'admission RPC streams did not drain: {client}')
            return dict(status='passed', processes=len(results), clients=results)
        if time.monotonic()>=deadline:
            raise TimeoutError('admission response drain audit did not finish')
        time.sleep(.01)
