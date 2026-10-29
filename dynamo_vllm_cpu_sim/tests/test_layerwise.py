"""Analytic pipeline, fixed-batch barriers, residency and weighted layer IO."""
from copy import deepcopy
from types import SimpleNamespace
import math
import unittest
from unittest.mock import patch,Mock

from sim.stack.layerwise import LayerRunner,layer_durations,layer_spec
from sim.stack.clock import TimeScale
from sim.stack.storage_service import Service


def config(mode="layer_pipeline"):
    return dict(block_size=1,enable_prefix_caching=False,kv_transfer_mode=mode,
        layerwise=dict(layers=3,startup="once"),compute=dict(mode="linear",
            step_overhead_s=0.,prefill_token_s=.001,decode_token_s=.001),
        storage=dict(disks=1,paths_per_disk=4,disk_Bps=10000.,path_Bps=10000.,
            shared_link_Bps=None,kv_bytes_per_token=300,read_latency_s=.002,
            bandwidth_policy="max_min_fair",record_reads=True))


class Clock:
    def __init__(self):self.now=100.
    def sleep(self,dt):self.now+=dt


class LayerwiseTest(unittest.TestCase):
    def exercise(self,mode,c=.005,factor=1,chunk_twice=False,zero=False):
        cfg=config(mode);cfg["wall_time_scale"]=factor
        clock=Clock();events=[];feedback=Mock()
        with patch("time.monotonic",side_effect=lambda:clock.now),patch("time.perf_counter",side_effect=lambda:clock.now),patch("time.sleep",side_effect=clock.sleep):
            service=Service(cfg,[]);service.store.present.add(b"a")
            state=service.layer_state
            load=state.register(0,"r",[b"a".hex()],100.) if not zero else None
            storage=SimpleNamespace(layer_submit=state.submit,layer_status=state.status)
            runner=LayerRunner(cfg,storage,0,TimeScale(factor),feedback,
                lambda event,**kw:events.append(dict(event=event,t=clock.now,**kw)))
            runner.install(SimpleNamespace(loads={"r":load} if load else {}))
            timing=dict(mode="linear",duration_s=3*c)
            start=clock.now
            runner.run(["r","zero"],timing,1)
            elapsed=(clock.now-start)/factor
            if chunk_twice:
                before=len(state.history)
                start=clock.now
                runner.run(["r","zero"],timing,2)
                self.assertAlmostEqual((clock.now-start)/factor,3*c)
                self.assertEqual(len(state.history),before)
            for row in events:
                if row["event"]=="layer_compute_start":
                    self.assertEqual(row["req_ids"],["r","zero"])
                    if not zero:
                        done=state.reads[(0,"r",row["layer"])]["completed_s"]
                        self.assertGreaterEqual(row["t"]+1e-10,service.t0+factor*done)
            self.assertEqual(service.bytes_submitted,0 if zero else 300)
            self.assertAlmostEqual(service.store.bytes_transferred,0 if zero else 300)
            self.assertEqual(len(service.store.pending),0)
            self.assertEqual(feedback.completed.call_count,0 if zero else 1)
            return elapsed,state,events

    def test_pipeline_matches_fast_and_slow_io_analytic_completion(self):
        for c in (.005,.02):
            for k in (1,10):
                elapsed,_,_=self.exercise("layer_pipeline",c,k)
                self.assertAlmostEqual(elapsed,.002+.01+2*max(.01,c)+c,places=8)
                serial,_,_=self.exercise("layer_serial",c,k)
                self.assertAlmostEqual(serial,.002+3*(.01+c),places=8)

    def test_mixed_batch_barrier_and_chunk_prefix_residency(self):
        self.exercise("layer_pipeline",chunk_twice=True)
        elapsed,_,_=self.exercise("layer_pipeline",zero=True)
        self.assertAlmostEqual(elapsed,.015)

    def test_weighted_competition_and_reject_duplicate_or_skipped_layers(self):
        cfg=config();clock=Clock()
        with patch("time.monotonic",side_effect=lambda:clock.now):
            service=Service(cfg,[]);service.store.present.update([b"a",b"b"])
            state=service.layer_state
            state.register(0,"a",[b"a".hex()],100.)
            state.register(1,"b",[b"a".hex(),b"b".hex()],100.)
            state.submit(0,["a"],0);state.submit(1,["b"],0)
            with self.assertRaises(ValueError):state.submit(0,["a"],0)
            with self.assertRaises(ValueError):state.submit(0,["a"],1)
            clock.now+=.031
            self.assertFalse(state.status(0,["a"],0)["ready"])
            clock.now+=.002
            self.assertEqual(state.status(0,["a"],0)["ready"],["a"])
            self.assertEqual(state.status(1,["b"],0)["ready"],["b"])
            self.assertAlmostEqual(service.store.bytes_transferred,300)
            self.assertAlmostEqual(state.reads[(0,"a",0)]["completed_s"],.032)
            with self.assertRaises(ValueError):state.submit(0,["a"],2)

    def test_layer_parameters_and_glm_compute_sum(self):
        from sim.compute_time import estimate_step
        cfg=config();cfg["compute"]=dict(mode="glm5_d950pr",engine_overhead_s=.003)
        cfg.pop("layerwise");cfg["storage"]["kv_bytes_per_token"]=109824
        timing=estimate_step(cfg["compute"],[(512,256),(0,32)])
        times=layer_durations(timing,cfg)
        self.assertEqual(len(times),78)
        self.assertEqual(layer_spec(cfg)["bytes_per_token"],1408)
        self.assertAlmostEqual(math.fsum(times),timing["duration_s"])
        self.assertAlmostEqual(times[0],times[1]+.003)
        self.assertNotEqual(times[1],times[3])
        bad=deepcopy(cfg);bad["storage"]["kv_bytes_per_token"]=100
        with self.assertRaises(ValueError):layer_spec(bad)
        bad=deepcopy(cfg);bad["enable_prefix_caching"]=True
        with self.assertRaises(ValueError):layer_spec(bad)

    def test_counted_layer_reads_do_not_discard_weighted_roundoff_tail(self):
        from sim.grouped_storage import GroupedStorage
        cfg=config()["storage"];cfg.update(disk_Bps=120e9,read_latency_s=0.)
        store=GroupedStorage(cfg,1)
        # Large time origin makes rounding to just before the true event
        # observable. A per-block remainder <1e-5 still has to transfer.
        store.now=200.
        total=0
        for layer in range(78):
            store.submit_counted(store.now,(0,"r",layer),{0:65537},1408,100.,0.)
            total+=65537*1408
            for _ in range(10):
                done=store.advance(store.next_time())
                if done:break
            else:self.fail("layer tail made no bounded progress")
            self.assertFalse(store.pending)
        self.assertLess(abs(store.bytes_transferred-total),.01)
        self.assertLess(abs(store.disk_bytes_transferred[0]-total),.01)

    def test_layer_feedback_counts_one_attempt_and_actual_bytes(self):
        from sim.admission import AdmissionController
        from sim.request_profile import RequestProfile
        from sim.control_events import ControlEvent
        ctrl=AdmissionController(dict(mode="immediate",quantile=.75,reference_bandwidth_Bps=100.,
            release_on="load_done",aging_enabled=False,aging_wait_s=1.,aging_cooldown_s=1.))
        profile=RequestProfile("r",0.,"a",2,1,300,.1,3000.,.2,1.)
        ctrl.on_arrivals([profile],0.);ctrl.reserve(ctrl.pick(0.),0.);ctrl.mark_submitted("r",0,0.)
        def emit(i,kind,**payload):ctrl.on_event(ControlEvent(str(i),kind,"r",0,.01,.01,payload),.01)
        emit(0,"load_submitted",attempt_id=1,submitted_read_bytes=0,layer_count=3,expected_read_bytes=300)
        emit(1,"layer_load_submitted",attempt_id=1,layer=0,submitted_read_bytes=100)
        with self.assertRaises(ValueError):emit(2,"load_completed",attempt_id=1)
        self.assertTrue(ctrl.records["r"].lease_open)
        emit(3,"layer_load_submitted",attempt_id=1,layer=0,submitted_read_bytes=100)
        emit(4,"layer_load_submitted",attempt_id=1,layer=1,submitted_read_bytes=100)
        emit(5,"layer_load_submitted",attempt_id=1,layer=2,submitted_read_bytes=100)
        emit(6,"load_completed",attempt_id=1)
        record=ctrl.records["r"]
        self.assertEqual(record.load_attempts,1)
        self.assertEqual(record.submitted_read_bytes,300)
        self.assertFalse(record.lease_open)
        self.assertFalse(ctrl.invalid_reasons)


if __name__=="__main__":unittest.main()
