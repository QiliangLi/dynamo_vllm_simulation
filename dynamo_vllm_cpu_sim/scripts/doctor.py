#!/usr/bin/env python3
"""Baseline provenance gate. After deliberate source edits, use --allow-edited."""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
p = argparse.ArgumentParser()
p.add_argument("--allow-edited", action="store_true")
args = p.parse_args()
expected = {"vllm": "0.20.2", "ai-dynamo-runtime": "1.5.0"}
for package, version in expected.items():
    actual = importlib.metadata.version(package)
    if actual.split("+")[0] != version:
        raise SystemExit(f"{package}: expected {version}; got {actual}")
import torch

if torch.version.cuda is not None or torch.__version__ != "2.11.0+cpu":
    raise SystemExit(f"Expected pinned CPU torch, got {torch.__version__}")
changed = []
for name, expected_hash in json.loads(
    (root / "source-fingerprints.json").read_text()
).items():
    path = root / name
    if (
        not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash
    ):
        changed.append(name)
if changed and not args.allow_edited:
    raise SystemExit("Source provenance mismatch: " + ", ".join(changed))
from sim.engine import scheduler_class
from dynamo.llm import SelectionService
import vllm

if not Path(vllm.__file__).resolve().is_relative_to((root / "upstream/vllm").resolve()):
    raise SystemExit("Imported vLLM is not this project's editable source tree")
print(
    json.dumps(
        {
            "status": "ready",
            "vllm": vllm.__version__,
            "torch": torch.__version__,
            "ascend_class": scheduler_class("ascend_default").__name__,
            "dynamo_class": SelectionService.__name__,
            "edited_sources": changed,
        },
        indent=2,
    )
)
