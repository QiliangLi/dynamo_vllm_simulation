"""TFS metadata transport for Dynamo 1.2.0's native sampling-parameter API.

This module does not replace model execution, KV allocation, or the scheduler.
Worker launchers must install the matching ReadGate before enabling this adapter.
"""

import base64
import json
import math

STORAGE_PREFIX = "storage_hint_v1:"
EDF_PREFIX = "edf_priority_v1:"


def _decode(annotations, prefix, limit):
    values = [a[len(prefix):] for a in annotations if a.startswith(prefix)]
    if len(values) != 1 or len(values[0]) > limit:
        raise ValueError(f"exactly one bounded {prefix} annotation is required")
    value = json.loads(base64.b64decode(values[0], altchars=b"-_", validate=True))
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("invalid TFS metadata version")
    return value


def storage_hint(annotations):
    value = _decode(annotations, STORAGE_PREFIX, 2048)
    if set(value) != {"version", "is_heavy_read", "estimated_remote_read_bytes", "threshold_version"}:
        raise ValueError("invalid storage hint fields")
    if type(value["is_heavy_read"]) is not bool:
        raise ValueError("invalid heavy-read classification")
    for key in ("estimated_remote_read_bytes", "threshold_version"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError(f"invalid {key}")
    return value


def edf_priority(annotations):
    value = _decode(annotations, EDF_PREFIX, 4096)
    fields = {"version", "arrival_s", "deadline_s", "reference_service_s",
              "origin_monotonic_s", "wall_time_scale"}
    if set(value) != fields:
        raise ValueError("invalid EDF fields")
    for key in fields - {"version"}:
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]):
            raise ValueError(f"invalid EDF number: {key}")
    if (value["arrival_s"] < 0 or value["deadline_s"] <= value["arrival_s"]
            or value["reference_service_s"] <= 0 or value["origin_monotonic_s"] <= 0
            or value["wall_time_scale"] < 1):
        raise ValueError("invalid EDF bounds")
    return value


def apply_tfs_metadata(params, request, *, enabled):
    annotations = request.get("annotations") or []
    if not isinstance(annotations, list) or any(not isinstance(a, str) for a in annotations):
        raise ValueError("annotations must be a string list")
    has_tfs = any(a.startswith((STORAGE_PREFIX, EDF_PREFIX)) for a in annotations)
    if not enabled:
        if has_tfs:
            raise RuntimeError("TFS metadata received by a worker without TFS enabled")
        return params
    hint = storage_hint(annotations)
    extra = dict(params.extra_args or {})
    if "storage_hint" in extra or "edf_priority" in extra:
        raise ValueError("reserved TFS extra_args already exist")
    extra["storage_hint"] = hint
    if any(a.startswith(EDF_PREFIX) for a in annotations):
        extra["edf_priority"] = edf_priority(annotations)
    params.extra_args = extra
    return params
