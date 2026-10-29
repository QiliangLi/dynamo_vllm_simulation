"""Experiment-only warm phase: ample permits; exclude warm rows from TFS history."""
import json
import os
from pathlib import Path
import runpy
import sys

root = Path(os.environ['SIM_STACK_CONFIG']).parents[3]
sys.path.insert(0, str(root))
from sim.admission import AdmissionController
import sim.stack.admission_service as service

original_arrivals = AdmissionController.on_arrivals
excluded = set()

def arrivals(self, profiles, now_s):
    saved = {rid: self.records.pop(rid) for rid in excluded if rid in self.records}
    try:
        return original_arrivals(self, profiles, now_s)
    finally:
        self.records.update(saved)

AdmissionController.on_arrivals = arrivals
original_create = service.create_app

def create(*args, **kwargs):
    app = original_create(*args, **kwargs)
    state = app.state.admission
    nominal = state.controller.c['reference_bandwidth_Bps']
    state.controller.c['reference_bandwidth_Bps'] = nominal * 1e6
    done = False

    @app.post('/diagnostic/warmup/finish')
    async def finish():
        nonlocal done
        assert not done and len(state.controller.records) == 72
        assert not state.fatal and not state.controller.pending and not state.delayed
        assert all(r.state == 'FINISHED' and not r.lease_open for r in state.controller.records.values())
        score = state.routing_score.snapshot() if state.routing_score is not None else None
        if score is not None:
            assert score['registered'] == score['waiting'] == score['active'] == 0
        excluded.update(state.controller.records)
        assert all(rid.startswith('warmup-') for rid in excluded)
        state.controller.c['reference_bandwidth_Bps'] = nominal
        state.controller.theta = state.controller.cap = None
        # Keep completed rows, hints, permits and journal for the full-run audit.
        # The next real arrival derives threshold/cap only from measured history.
        state.log_event('warmup_finished', excluded_history_ids=sorted(excluded),
                        reference_bandwidth_Bps=nominal, native_score_drained=score is not None)
        done = True
        result = dict(status='pass', excluded_history_ids=sorted(excluded),
                      reference_bandwidth_Bps=nominal, pending=0, held=0, score_drained=score is not None)
        (Path(os.environ['SIM_STACK_CONFIG']).parent/'warmup.history.check.json').write_text(json.dumps(result,indent=2)+'\n')
        return result

    return app

service.create_app = create
target, *args = sys.argv[1:]
sys.argv = [target, *args]
runpy.run_path(target, run_name='__main__')
