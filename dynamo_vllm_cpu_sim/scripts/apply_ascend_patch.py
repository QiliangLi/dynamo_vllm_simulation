#!/usr/bin/env python3
"""Generate an opt-in patched copy, preserving the upstream file untouched."""

import difflib
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
relative = "upstream/ascend/vllm_ascend/patch/platform/patch_balance_schedule.py"
source = root / relative
expected = json.loads((root / "source-fingerprints.json").read_text())[relative]
if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
    raise SystemExit(
        "Source changed: review the patch against your modified Ascend tree"
    )
old = source.read_text()
needle = "                    request.status = RequestStatus.WAITING_FOR_REMOTE_KVS\n                    continue\n"
if old.count(needle) != 1:
    raise SystemExit("Expected exactly one asynchronous load branch")
new = old.replace(
    needle,
    needle.replace(
        "                    continue\n",
        "                    request.num_computed_tokens = num_computed_tokens\n                    continue\n",
    ),
)
destination = root / "patched"
destination.mkdir(exist_ok=True)
(destination / "ascend_balance_schedule.py").write_text(new)
(destination / "ascend_balance.patch").write_text(
    "".join(
        difflib.unified_diff(
            old.splitlines(True),
            new.splitlines(True),
            fromfile=relative,
            tofile="patched/ascend_balance_schedule.py",
        )
    )
)
print(
    "Created patched/ascend_balance_schedule.py; upstream is unchanged. CPU experiment only, no NPU validation."
)
