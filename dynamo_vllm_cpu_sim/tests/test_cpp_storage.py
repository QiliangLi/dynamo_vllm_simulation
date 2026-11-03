"""Native production adapter parity, invalid-input atomicity and cache isolation."""
import math
import random
import shutil
import sys
import unittest

from sim.cpp_grouped_storage import CppGroupedStorage,storage_class
from sim.grouped_storage import GroupedStorage


def config(disks=4):
    return dict(disks=disks,paths_per_disk=2,disk_Bps=25000000.,path_Bps=10000000.,
        shared_link_Bps=None,kv_bytes_per_token=100,read_latency_s=.001,bandwidth_policy='max_min_fair')


@unittest.skipUnless(sys.platform=='linux' and shutil.which('g++'),'Linux and native compiler are required')
class CppStorageTest(unittest.TestCase):
    def test_random_counted_parity(self):
        for disks in [1,4]:
            for seed in [17,20261103]:
                rng=random.Random(seed);rows=[]
                for i in range(120):
                    counts={d:rng.randint(1,25) for d in range(disks) if rng.random()<.7} or {0:1}
                    rows.append((i//3*.0001,i,counts,rng.choice([1024.,4096.,8192.]),
                                 rng.choice([1e4,1e6,1e8]),rng.choice([0.,.0002,.002])))
                def run(cls):
                    s=cls(config(disks),1);completed={}
                    def advance(t):
                        for key in s.advance(t):completed[key]=s.now
                    for t,key,counts,size,demand,latency in rows:
                        while s.next_time()<=t:advance(s.next_time())
                        advance(t);s.submit_counted(t,key,counts,size,demand,latency)
                    while s.pending:advance(s.next_time())
                    return s,completed
                py,expected=run(GroupedStorage);native,actual=run(CppGroupedStorage)
                self.assertEqual(set(expected),set(actual))
                for key in expected:self.assertAlmostEqual(expected[key],actual[key],places=9)
                for a,b in zip(py.disk_bytes_transferred,native.disk_bytes_transferred):self.assertAlmostEqual(a,b,places=5)
                self.assertAlmostEqual(py.bytes_transferred,native.bytes_transferred,places=5)
                self.assertEqual(native.next_time(),math.inf)
                self.assertEqual(native.snapshot()['queued_reads'],0)

    def test_snapshot_placement_latency_and_per_disk_cache(self):
        py=GroupedStorage(config(),1);native=CppGroupedStorage(config(),1)
        for s in [py,native]:
            s.submit_counted(0,'a',{0:2},10000,1000,0)
            s.submit_counted(0,'b',{3:1},10000,1000,.001)
        first=native.snapshot();count=first['native_waterfill_calls']
        for _ in range(3):native.next_time();native.snapshot()
        self.assertEqual(native.snapshot()['native_waterfill_calls'],count)
        self.assertEqual(py.snapshot()['disk_rates_Bps'],first['disk_rates_Bps'])
        for s in [py,native]:s.submit_counted(0,'c',{1:1},10000,1000,0)
        self.assertEqual(native.snapshot()['native_waterfill_calls'],count+1)
        self.assertEqual(py.snapshot()['path_rates_Bps'],native.snapshot()['path_rates_Bps'])
        self.assertEqual(py.disk_of(b'prefix-hash'),native.disk_of(b'prefix-hash'))

    def test_invalid_request_does_not_mutate(self):
        s=CppGroupedStorage(config(),1)
        s.submit_counted(0,'a',{0:1},100,100,0)
        for args in [(0,'a',{0:1},100,100,0),(0,'b',{4:1},100,100,0),
                     (0,'b',{0:0},100,100,0),(0,'b',{0:1},math.nan,100,0)]:
            with self.assertRaises(ValueError):s.submit_counted(*args)
        self.assertEqual(s.pending,{'a':1})
        with self.assertRaises(RuntimeError):s.advance(s.next_time()+1)
        self.assertEqual(s.now,0)
        self.assertEqual(s.advance(s.next_time()),['a'])
        self.assertEqual(s.bytes_transferred,100)

    def test_unsupported_modes_fail_explicitly(self):
        c=config();c['shared_link_Bps']=1e7
        with self.assertRaises(ValueError):CppGroupedStorage(c,1)
        c=dict(storage=dict(config(),backend='cpp',completion_mode='notify'),kv_transfer_mode='layer_pipeline')
        with self.assertRaises(ValueError):storage_class(c,True,False)
        c['storage']['completion_mode']='poll';c['kv_transfer_mode']='bulk'
        with self.assertRaises(ValueError):storage_class(c,True,False)

if __name__=='__main__':unittest.main()
