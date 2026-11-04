"""Profiles from Dynamo's raw selected-worker tier snapshot, never HTTP hit hints.

The pinned selected-worker snapshot is cumulative. Its `disk_blocks` includes
Disk and External extensions. One worker has one DP rank in this experiment.
"""
from bisect import bisect_right
import math

from sim.request_profile import RequestProfile
from sim.reference_time import get_reference_time_estimator


def enabled(config):
    return config.get('admission', {}).get('profile_source', 'trace') == 'dynamo_tier'


def validate_snapshot(value, config):
    fields = {'version', 'request_id', 'prompt_tokens', 'block_size', 'worker_id',
              'dp_rank', 'dp_device_blocks', 'gpu_blocks', 'host_pinned_blocks', 'disk_blocks'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid Dynamo tier snapshot fields')
    for key in fields - {'request_id', 'dp_device_blocks'}:
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError('invalid tier integer: ' + key)
    if value['version'] != 1 or value['block_size'] != config['block_size']:
        raise ValueError('Dynamo tier protocol or block size mismatch')
    n, b = value['prompt_tokens'], value['block_size']
    if not 0 < n < config['max_model_len']:
        raise ValueError('invalid tier prompt length')
    if not isinstance(value['request_id'], str) or not value['request_id']:
        raise ValueError('invalid tier request ID')
    ranks = value['dp_device_blocks']
    if ranks != [[value['dp_rank'], value['gpu_blocks']]]:
        raise ValueError('tier admission currently requires exactly one DP rank per worker')
    if not 0 <= value['gpu_blocks'] <= value['host_pinned_blocks'] <= value['disk_blocks'] <= n // b:
        raise ValueError('invalid cumulative tier prefix')
    return dict(value)


def build_tier_profile(snapshot, config, rid, arrival_s):
    snapshot = validate_snapshot(snapshot, config)
    n, b = snapshot['prompt_tokens'], snapshot['block_size']
    # vLLM still computes the final prompt token. Remove faster-tier coverage
    # before calculating actual lower-tier read volume; never use weighted hits.
    limit = (n - 1) // b
    host = min(snapshot['host_pinned_blocks'], limit)
    cached = min(snapshot['disk_blocks'], limit) * b
    remote = (min(snapshot['disk_blocks'], limit) - host) * b
    timing = get_reference_time_estimator(config).estimate(n, cached, remote_read_tokens=remote)
    return RequestProfile(request_id=rid,arrival_s=arrival_s,class_id='unlabelled',
        prompt_tokens=n,estimated_remote_tokens=remote,estimated_read_bytes=timing.read_bytes,
        standalone_compute_s=timing.compute_s,
        pressure_Bps=timing.pressure_Bps, reference_service_s=timing.service_s,
        deadline_s=arrival_s + config['request_profile']['slo_multiplier'] * timing.service_s)


def preview_hint(profile, sorted_history, config, version):
    """Classify a candidate against arrived history plus this one request.

    Each worker is an alternative placement, not another historical request.
    No controller state or capacity is mutated before the picker selects one.
    """
    pressure = profile.pressure_Bps
    position = bisect_right(sorted_history, pressure)
    rank = math.floor(config['admission']['quantile'] * len(sorted_history))
    raw = (pressure if position == rank else
           sorted_history[rank if rank < position else rank - 1])
    if raw == 0 and config['compute'].get('mode') == 'sa_e26f':
        theta = 0.
    elif raw > 0:
        theta = raw
    else:
        positive_index = bisect_right(sorted_history, 0.)
        positive = sorted_history[positive_index] if positive_index < len(sorted_history) else math.inf
        theta = min(positive, pressure if pressure > 0 else math.inf)
        if not math.isfinite(theta):
            theta = None
    return dict(version=1,
        is_heavy_read=bool(profile.estimated_read_bytes > 0 and theta is not None and pressure >= theta),
        estimated_remote_read_bytes=profile.estimated_read_bytes, threshold_version=version), theta
