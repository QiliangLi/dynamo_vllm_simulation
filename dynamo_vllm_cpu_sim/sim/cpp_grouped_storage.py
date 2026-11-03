"""Optional native counted-layer kernel; placement and request metadata stay Python.

Only independent disks and the existing poll/locked service path are enabled.
Compilation is once per source/compiler hash, before serving any requests.
"""
import ctypes as C
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import weakref

from sim.storage import Storage

_LIB = None
_BUILD = None


def native_library():
    global _LIB, _BUILD
    if _LIB is not None:
        return _LIB
    if platform.system() != 'Linux':
        raise RuntimeError('cpp storage backend currently supports Linux only')
    source = Path(__file__).parent/'native/grouped_storage.cpp'
    compiler = shutil.which(os.environ.get('CXX', 'g++'))
    if compiler is None:
        raise RuntimeError('cpp storage requires g++; select backend=python to use the reference')
    version = subprocess.check_output([compiler, '--version'], text=True).splitlines()[0]
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    flags = ['-O3', '-DNDEBUG', '-ffp-contract=off', '-std=c++17', '-shared', '-fPIC']
    signature = hashlib.sha256(json.dumps([source_sha, compiler, version, flags, platform.machine()]).encode()).hexdigest()
    cache = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache')))/'dv-storage-native'/signature
    cache.mkdir(parents=True, exist_ok=True)
    target = cache/'grouped_storage.so'
    if not target.exists():
        fd, temp = tempfile.mkstemp(prefix='build-', suffix='.so', dir=cache)
        os.close(fd)
        try:
            subprocess.run([compiler, *flags, str(source), '-o', temp], check=True, capture_output=True, text=True)
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
    lib = C.CDLL(str(target))
    ptr, u64p = C.c_void_p, C.POINTER(C.c_uint64)
    signatures = {
        'gs_error': ([], C.c_char_p), 'gs_create': ([C.c_int,C.c_double],ptr),
        'gs_destroy': ([ptr],None),
        'gs_submit': ([ptr,C.c_double,C.c_uint64,u64p,C.c_double,C.c_double,C.c_double],C.c_int),
        'gs_next': ([ptr,C.POINTER(C.c_double)],C.c_int),
        'gs_advance': ([ptr,C.c_double,u64p,C.c_size_t],C.c_int),
        'gs_snapshot': ([ptr],C.c_char_p), 'gs_bytes': ([ptr],C.c_double),
    }
    for name,(args,result) in signatures.items():
        fn=getattr(lib,name);fn.argtypes=args;fn.restype=result
    _BUILD=dict(source_sha256=source_sha,compiler=version,flags=flags,library_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    _LIB=lib
    return lib


class CppGroupedStorage:
    def __init__(self, config, block_size):
        if config.get('bandwidth_policy') != 'max_min_fair' or config.get('shared_link_Bps') is not None:
            raise ValueError('cpp storage requires max_min_fair with independent disks (shared_link_Bps=null)')
        if type(config.get('disks')) is not int:
            raise ValueError('cpp storage disks must be an integer')
        self._metadata=Storage(config,block_size)
        self.c=config;self.link_Bps=None;self.policy='max_min_fair'
        self.present=self._metadata.present;self.block_bytes=self._metadata.block_bytes
        self.disk_bytes_submitted=[0]*config['disks']
        self.pending={};self._ids={};self._keys={};self._paths={};self._sequence=0
        self.path_seq=self._metadata.path_seq;self.now=0.
        self._lib=native_library();self._handle=self._lib.gs_create(config['disks'],config['disk_Bps'])
        if not self._handle:self._raise()
        self._finalizer=weakref.finalize(self,self._lib.gs_destroy,self._handle)
        self._buffer=(C.c_uint64*64)();self._next=C.c_double()

    def _raise(self):
        raise RuntimeError('native storage: '+self._lib.gs_error().decode())

    def disk_of(self, block):
        return self._metadata.disk_of(block)

    @property
    def bytes_transferred(self):
        return self._lib.gs_bytes(self._handle)

    def submit_counted(self, now, key, counts, block_bytes, demand, latency):
        if (now!=self.now or key in self.pending or not counts
                or not all(math.isfinite(x) for x in (now,block_bytes,demand,latency))
                or block_bytes<=0 or demand<=0 or latency<0
                or not all(type(d) is int and 0<=d<self.c['disks'] and type(n) is int and 0<n<2**63 for d,n in counts.items())):
            raise ValueError('invalid counted layer read')
        ident=self._sequence
        packed=(C.c_uint64*self.c['disks'])(*(counts.get(d,0) for d in range(self.c['disks'])))
        if self._lib.gs_submit(self._handle,now,ident,packed,block_bytes,demand,latency)<0:self._raise()
        self._sequence+=1;self.pending[key]=sum(counts.values());self._ids[ident]=key;self._keys[key]=ident
        self._paths[ident]={}
        for d,n in sorted(counts.items()):
            self._paths[ident][d]=self.path_seq[d];self.path_seq[d]+=n
            self.disk_bytes_submitted[d]+=n*block_bytes

    def next_time(self):
        if self._lib.gs_next(self._handle,C.byref(self._next))<0:self._raise()
        return self._next.value

    def advance(self,target):
        if len(self.pending)>len(self._buffer):self._buffer=(C.c_uint64*(2*len(self.pending)))()
        count=self._lib.gs_advance(self._handle,target,self._buffer,len(self._buffer))
        if count<0:self._raise()
        self.now=target;done=[]
        for i in range(count):
            ident=self._buffer[i];key=self._ids.pop(ident)
            del self._keys[key],self._paths[ident],self.pending[key]
            done.append(key)
        return done

    def _state(self):
        encoded=self._lib.gs_snapshot(self._handle)
        if encoded is None:self._raise()
        return json.loads(encoded)

    @property
    def disk_bytes_transferred(self):
        return self._state()['disk_bytes']

    @property
    def disk_peak_Bps(self):
        return self._state()['peaks']

    def snapshot(self):
        state=self._state();rates=[0.]*self.c['disks'];paths={};queued=active=0;remaining=0.
        for ident,d,n,left,ready,demand,rate in state['groups']:
            queued+=n;remaining+=left*n
            if rate>0:
                rates[d]+=rate*n;active+=n;start=self._paths[ident][d]
                paths.update({f'{d}:{p}':rate for p in range(start,start+n)})
        return dict(placement='hrw_sha256_content_hash',disks=self.c['disks'],disk_Bps=self.c['disk_Bps'],
            shared_link_Bps=None,disk_rates_Bps=rates,disk_peak_Bps=state['peaks'],
            disk_bytes_submitted=self.disk_bytes_submitted.copy(),disk_bytes_transferred=state['disk_bytes'],
            timestamp_s=self.now,queued_reads=queued,active_paths=active,bytes_remaining=remaining,
            path_rates_Bps=paths,storage_backend='cpp',native_build=_BUILD,
            native_rate_rebuilds=state['rate_rebuilds'],native_waterfill_calls=state['waterfill_calls'])


def storage_class(config, grouped, sa_mode):
    from sim.grouped_storage import GroupedStorage
    name=config['storage'].get('backend','python')
    if name=='python':return GroupedStorage if grouped else Storage
    if name!='cpp':raise ValueError('storage.backend must be python or cpp')
    from sim.stack.layerwise import transfer_mode
    if (not grouped or sa_mode or transfer_mode(config)=='bulk'
            or config['storage'].get('completion_mode','poll')!='poll'
            or config['storage'].get('service_mode','locked')!='locked'):
        raise ValueError('cpp storage currently requires grouped layerwise poll/locked mode')
    return CppGroupedStorage
