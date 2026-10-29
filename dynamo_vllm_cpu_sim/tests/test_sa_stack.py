"""SA model reuse, aggregate batch IO, due boosts and frozen request profiles."""
from copy import deepcopy
from fractions import Fraction as F
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sim.compute_time import estimate_step,reference_compute_s
from sim.request_profile import normalize_config,build_profile
from sim.stack.layerwise import layer_durations,layer_spec
from sim.stack.storage_service import Service
from sim.sa_admission import SAAdmission

ROOT=Path(__file__).resolve().parents[1]


def config():
    return json.loads((ROOT/"configs/sa_abc_stack.json").read_text())


class SAStackTests(unittest.TestCase):
    def test_batch_compute_matches_independent_fraction_and_not_padding(self):
        c=normalize_config(config());p=c["compute"]["profile"]
        for shapes in ([(42883,256)],[(9399,4096)],[(0,2657)],[(42883,128),(9399,2048),(0,400)]):
            n=sum(u for h,u in shapes);a=sum(h*u+u*(u+1)//2 for h,u in shapes)
            eta=min(F(1),max(F(p["eta_floor"]),F(n,p["N_sat"])))
            expected=F(p["t_launch_s"])+F(p["a_s_per_token"])*n/eta+F(p["b_s_per_pair"])*a
            timing=estimate_step(c["compute"],shapes)
            self.assertAlmostEqual(timing["duration_s"],8*float(expected),places=14)
            self.assertEqual(timing["real_prefill_tokens"],timing["padded_prefill_tokens"])
            self.assertEqual(layer_durations(timing,c),[timing["layer_compute_s"]]*8)
        with self.assertRaises(ValueError):estimate_step(c["compute"],[(0,1)],1)

    def test_reference_profiles_preserve_sa_overlap_and_deadline(self):
        c=normalize_config(config())
        for h,u in ((42883,256),(9399,4096),(0,2657)):
            row=dict(id="r",arrival_s=2.,class_id="A",prompt_token_ids=[995]*(h+u),remote_prefix_tokens=h)
            p=build_profile(row,c)
            t=estimate_step(c["compute"],[(h,u)]);layer=t["layer_compute_s"]
            r=h*4096/120e9
            expected=r+8*layer+7*max(0.,r-layer)
            self.assertAlmostEqual(p.reference_service_s,expected)
            self.assertAlmostEqual(p.deadline_s,2+4*expected)
            self.assertEqual(p.estimated_read_bytes,h*32768)
            self.assertAlmostEqual(p.pressure_Bps,h*4096/layer)
        chunks=reference_compute_s(c["compute"],1032,32,128)
        self.assertAlmostEqual(chunks,sum(estimate_step(c["compute"],[(h,min(128,1032-h))])["duration_s"] for h in range(32,1032,128)))

    def test_sa_config_rejects_mixed_glm_bytes_and_storage(self):
        for field,value in (("kv_bytes_per_token",109824),("bandwidth_policy","max_min_fair"),("read_latency_s",.0001)):
            c=config();c["storage"][field]=value
            with self.assertRaises(ValueError):normalize_config(c)
        c=config();c["kv_transfer_mode"]="bulk"
        with self.assertRaises(ValueError):normalize_config(c)
        self.assertEqual(layer_spec(config())["layers"],8)

    def test_aggregate_io_is_one_flow_per_batch_and_waits_all_members(self):
        c=config();c.pop("cache_model");c["wall_time_scale"]=1
        c["storage"].update(disk_Bps=100000.,shared_link_Bps=100000.,q_max_Bps=200000.)
        now=[100.]
        with patch("time.monotonic",side_effect=lambda:now[0]):
            s=Service(c,[]);st=s.layer_state
            s.store.present.update((b"a",b"b"))
            for worker,rid,blocks in ((0,"a",[b"a"]),(0,"b",[b"b"]),(1,"c",[b"a"])):
                st.register(worker,rid,[b.hex() for b in blocks],200000.)
            st.submit(0,["a","b"],0,step=1,compute_s=.01)
            st.submit(1,["c"],0,step=1,compute_s=.01)
            self.assertEqual(len(st.pool.flows),2)
            now[0]=100+.08192
            self.assertEqual(st.status(1,["c"],0)["ready"],["c"])
            self.assertEqual(st.status(0,["a","b"],0)["ready"],[])
            now[0]=100+.12288
            self.assertEqual(st.status(0,["a","b"],0)["ready"],["a","b"])
            self.assertAlmostEqual(st.pool.bytes_transferred,12288)
            self.assertEqual(s.op_stats()["pending_reads"],0)
            self.assertEqual(s.op_stats()["bytes_submitted"],12288)
            with self.assertRaises(ValueError):st.submit(0,["a","b"],0,step=1,compute_s=.01)

    def test_prefetch_rate_and_overdue_boost_reuse_original_allocator(self):
        c=config();c.pop("cache_model");c["wall_time_scale"]=1
        c["storage"].update(disk_Bps=100000.,shared_link_Bps=100000.,q_max_Bps=200000.)
        now=[100.]
        with patch("time.monotonic",side_effect=lambda:now[0]):
            s=Service(c,[]);s.store.present.add(b"a");st=s.layer_state
            st.register(0,"a",[b"a".hex()],200000.)
            st.submit(0,["a"],0,step=1,compute_s=.5)
            now[0]+=.041;s._advance_to_wall()
            st.submit(0,["a"],1,step=1,compute_s=.5,due_mono=now[0]+.01)
            flow=st.pool.flows[(0,1,1)];self.assertEqual(flow.q,4096/.5)
            now[0]+=.011;s._advance_to_wall()
            self.assertTrue(flow.boosted);self.assertEqual(flow.q,200000.)
            now[0]+=.04;s._advance_to_wall()
            st.submit(0,["a"],2,step=1,compute_s=.5,due_mono=now[0]-.01)
            self.assertTrue(st.pool.flows[(0,1,2)].boosted)

    def test_zero_threshold_uses_existing_sa_unlimited_rule(self):
        c=normalize_config(config());ctrl=SAAdmission(c["admission"])
        p=build_profile(dict(id="c",arrival_s=0.,class_id="C",prompt_token_ids=[995]*2657,remote_prefix_tokens=0),c)
        ctrl.on_arrivals([p],0.)
        self.assertEqual(ctrl.theta,0.);self.assertEqual(ctrl.cap,1<<30)


if __name__=="__main__":unittest.main()
