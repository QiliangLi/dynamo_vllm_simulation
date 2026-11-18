"""CPU contract tests for the actual sampling builders, without importing vLLM/CUDA.

Compile the functions from handlers.py; only SamplingParams is replaced. TFS
metadata handling uses the real module. Full engine execution is a server test.
"""
import ast
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class SamplingParams:
    def __init__(self, **kwargs):
        self.extra_args = None
        self.max_tokens = 1
        self.__dict__.update(kwargs)


def builders():
    names = {"build_sampling_params", "build_sampling_params_openai", "merge_kv_transfer_params"}
    tree = ast.parse((ROOT / "handlers.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(tree.body) == len(names)
    namespace = dict(SamplingParams=SamplingParams, Dict=dict, Any=object, os=os)
    exec(compile(tree, str(ROOT / "handlers.py"), "exec"), namespace)
    return namespace


class KvTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = builders()
        spec = importlib.util.spec_from_file_location("dynamo.vllm.tfs_metadata", ROOT / "tfs_metadata.py")
        cls.metadata = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.metadata)

    def build(self, kv=None, *, tfs=False, defaults=None):
        request = {"token_ids": [1, 2], "stop_conditions": {"max_tokens": 1}}
        if kv is not None:
            request["extra_args"] = {"kv_transfer_params": kv, "mm_hashes": ["image"]}
        if tfs:
            hint = dict(version=1, is_heavy_read=True, estimated_remote_read_bytes=65536, threshold_version=1)
            request["annotations"] = ["storage_hint_v1:" + base64.urlsafe_b64encode(json.dumps(hint).encode()).decode()]
            edf = dict(version=1, arrival_s=1, deadline_s=10, reference_service_s=1,
                       origin_monotonic_s=100, wall_time_scale=1)
            request["annotations"].append("edf_priority_v1:" + base64.urlsafe_b64encode(json.dumps(edf).encode()).decode())
        modules = {"dynamo": types.ModuleType("dynamo"), "dynamo.vllm": types.ModuleType("dynamo.vllm"),
                   "dynamo.vllm.tfs_metadata": self.metadata}
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"DYN_TFS_VLLM": "1" if tfs else "0"}):
            return self.functions["build_sampling_params"](request, defaults or {}, 98000)

    def test_true_false_in_both_tfs_modes(self):
        for tfs in (True, False):
            for flag in (True, False):
                with self.subTest(tfs=tfs, flag=flag):
                    params = self.build({"enable_phase2_dump": flag}, tfs=tfs)
                    self.assertIs(params.extra_args["kv_transfer_params"]["enable_phase2_dump"], flag)
                    self.assertEqual("storage_hint" in params.extra_args, tfs)
                    self.assertEqual("edf_priority" in params.extra_args, tfs)

    def test_missing_does_not_inject_connector_default(self):
        self.assertIsNone(self.build().extra_args)
        self.assertNotIn("kv_transfer_params", self.build(tfs=True).extra_args)

    def test_merge_preserves_other_options_and_shared_defaults(self):
        extra = {"other": "keep", "kv_transfer_params": {"remote_port": 123, "enable_phase2_dump": True}}
        params = self.build({"enable_phase2_dump": False}, defaults={"extra_args": extra})
        self.assertEqual(params.extra_args["other"], "keep")
        self.assertEqual(params.extra_args["kv_transfer_params"]["remote_port"], 123)
        self.assertIs(params.extra_args["kv_transfer_params"]["enable_phase2_dump"], False)
        self.assertIs(extra["kv_transfer_params"]["enable_phase2_dump"], True)
        self.assertIs(self.build(defaults={"extra_args": extra}).extra_args["kv_transfer_params"]["enable_phase2_dump"], True)

    def test_handoff_and_prefill_merge_keep_dump_option(self):
        params = self.build({"enable_phase2_dump": False})
        merge = self.functions["merge_kv_transfer_params"]
        merge(params, {"do_remote_decode": True})
        merge(params, {"remote_block_ids": [5], "remote_port": 123})
        self.assertEqual(params.extra_args["kv_transfer_params"], {
            "enable_phase2_dump": False, "do_remote_decode": True,
            "remote_block_ids": [5], "remote_port": 123})

    def test_text_mode_also_forwards_option(self):
        params = self.functions["build_sampling_params_openai"](
            {"kv_transfer_params": {"enable_phase2_dump": False}}, {})
        self.assertIs(params.extra_args["kv_transfer_params"]["enable_phase2_dump"], False)


if __name__ == "__main__":
    unittest.main()
