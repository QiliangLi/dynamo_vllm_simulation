"""Versioned metadata on the native Dynamo annotations wire, then extra_args.

Annotations are an explicit test transport adapter. No prompt/cache-salt changes,
side-channel profile lookup, or replacement of the native router is involved.
"""
import base64
import json

PREFIX = "storage_hint_v1:"


def validate_hint(value):
    if not isinstance(value, dict) or set(value) != {
        "version", "is_heavy_read", "estimated_remote_read_bytes", "threshold_version"
    }:
        raise ValueError("invalid storage_hint fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unsupported storage_hint version")
    if type(value["is_heavy_read"]) is not bool:
        raise ValueError("is_heavy_read must be boolean")
    for key in ("estimated_remote_read_bytes", "threshold_version"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    return dict(value)


def validate_http_metadata(body):
    nvext = body.get("nvext")
    if nvext is not None and not isinstance(nvext, dict):
        raise ValueError("nvext must be an object")
    annotations = (nvext or {}).get("annotations")
    if annotations is not None and not isinstance(annotations, list):
        raise ValueError("annotations must be a list")
    annotations = annotations or []
    if any(not isinstance(a, str) or a.startswith(PREFIX) for a in annotations):
        raise ValueError("caller cannot supply the gateway storage hint")


def attach_http_hint(body, hint):
    validate_http_metadata(body)
    result = dict(body)
    nvext = dict(result.get("nvext") or {})
    annotations = list(nvext.get("annotations") or [])
    data = json.dumps(validate_hint(hint), separators=(",", ":")).encode()
    annotations.append(PREFIX + base64.urlsafe_b64encode(data).decode())
    nvext["annotations"] = annotations
    result["nvext"] = nvext
    return result


def hint_from_request(request):
    values = [a[len(PREFIX):] for a in request.get("annotations", [])
              if isinstance(a, str) and a.startswith(PREFIX)]
    if len(values) != 1 or len(values[0]) > 2048:
        raise ValueError("exactly one storage_hint annotation is required")
    return validate_hint(json.loads(base64.b64decode(values[0], altchars=b"-_", validate=True)))


def install_dynamo_adapter():
    from dynamo.vllm import handlers
    original = handlers.build_sampling_params
    if getattr(original, "_storage_hint_adapter", False):
        return

    def build_sampling_params(request, default_sampling_params, model_max_len=None, enable_rl=False):
        hint = hint_from_request(request)
        # Restore the intended internal metadata shape without mutating callers.
        adapted = dict(request)
        adapted["extra_args"] = {**(request.get("extra_args") or {}), "storage_hint": hint}
        params = original(adapted, default_sampling_params, model_max_len, enable_rl)
        params.extra_args = {**(params.extra_args or {}), "storage_hint": dict(hint)}
        return params

    build_sampling_params._storage_hint_adapter = True
    handlers.build_sampling_params = build_sampling_params
