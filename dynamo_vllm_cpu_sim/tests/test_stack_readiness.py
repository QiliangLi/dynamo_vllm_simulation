"""Prevent a listed model/partial discovery from accepting a burst too early."""
from copy import deepcopy
import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from run_tfs_stack import frontend_has_workers


class FrontendReadinessTest(unittest.TestCase):
    def test_full_engine_membership_required(self):
        snapshot=dict(model="sim-opt",ready=True,namespaces={"dynamo":dict(
            ready=True,worker_types={"aggregated":dict(workers=8)})})
        self.assertTrue(frontend_has_workers(snapshot,8))
        for n in (0,1,6,7,9):
            changed=deepcopy(snapshot)
            changed["namespaces"]["dynamo"]["worker_types"]["aggregated"]["workers"]=n
            self.assertFalse(frontend_has_workers(changed,8))
        self.assertFalse(frontend_has_workers(dict(data=[dict(id="sim-opt")]),8))
        changed=deepcopy(snapshot);changed["ready"]=False
        self.assertFalse(frontend_has_workers(changed,8))
        changed=deepcopy(snapshot);changed["namespaces"]["dynamo"]["ready"]=False
        self.assertFalse(frontend_has_workers(changed,8))
        changed=deepcopy(snapshot);changed["model"]="other"
        self.assertFalse(frontend_has_workers(changed,8))


if __name__=="__main__":unittest.main()
