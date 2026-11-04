"""Profiles from Dynamo's raw selected-worker tier snapshot, never HTTP hit hints.

The pinned selected-worker snapshot is cumulative. Its `disk_blocks` includes
Disk and External extensions. One worker has one DP rank in this experiment.
"""
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
