"""Native/legacy admission addresses and monotonic clock domains."""
from functools import lru_cache
from pathlib import Path
import re
from urllib.parse import urlsplit

MAX_MESSAGE_BYTES = 256 * 1024


def parse_endpoint(endpoint, *, server=False):
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError('admission endpoint is required')
    if endpoint.startswith('dyn://'):
        name = endpoint[len('dyn://'):]
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', name):
            raise ValueError('expected dyn://namespace.component.endpoint')
        return 'dynamo', name
    if endpoint.startswith('tcp://'):
        value = urlsplit(endpoint)
        if (not value.hostname or value.port is None or value.username is not None
                or value.password is not None or value.path or value.query or value.fragment
                or (value.port == 0 and not server)):
            raise ValueError('expected tcp://host:port (port 0 only for server binding)')
        return 'tcp', (value.hostname, value.port)
    if endpoint.startswith('unix://'):
        endpoint = endpoint[len('unix://'):]
    elif '://' in endpoint:
        raise ValueError('unsupported admission RPC transport')
    if not endpoint.startswith('/'):
        raise ValueError('Unix socket path must be absolute')
    return 'unix', endpoint


def tcp_endpoint(host, port):
    return f'tcp://[{host}]:{port}' if ':' in host else f'tcp://{host}:{port}'


@lru_cache(maxsize=1)
def clock_id():
    # Linux workers on the same host/boot normally share CLOCK_MONOTONIC.
    # No guessed identity on other platforms: use receiver observation time.
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        namespace = Path('/proc/self/timens_offsets')
        offsets = namespace.read_text().strip() if namespace.exists() else ''
        return f'linux:{boot}:{offsets}' if boot else None
    except OSError:
        return None
