"""One request profile from Dynamo's common shared-storage prefix count."""

from sim.request_profile import RequestProfile
from sim.reference_time import get_reference_time_estimator


def enabled(config):
    return config.get('admission', {}).get('profile_source', 'trace') == 'dynamo_tier'


def validate_snapshot(value, config):
    fields = {'version', 'request_id', 'prompt_tokens', 'block_size', 'disk_blocks'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid shared-storage tier snapshot fields')
    for key in fields - {'request_id'}:
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError('invalid tier integer: ' + key)
    if value['version'] != 1 or value['block_size'] != config['block_size']:
        raise ValueError('Dynamo tier protocol or block size mismatch')
    n, b = value['prompt_tokens'], value['block_size']
    if not 0 < n < config['max_model_len']:
        raise ValueError('invalid tier prompt length')
    if not isinstance(value['request_id'], str) or not value['request_id']:
        raise ValueError('invalid tier request ID')
    if value['disk_blocks'] > n // b:
        raise ValueError('invalid shared-storage prefix')
    return dict(value)


def build_tier_profile(snapshot, config, rid, arrival_s):
    snapshot = validate_snapshot(snapshot, config)
    n, b = snapshot['prompt_tokens'], snapshot['block_size']
    # All equivalent instances read the same shared prefix. vLLM still
    # computes the last prompt token, even when that token is cached.
    cached = min(snapshot['disk_blocks'], (n - 1) // b) * b
    remote = cached
    timing = get_reference_time_estimator(config).estimate(n, cached, remote_read_tokens=remote)
    return RequestProfile(request_id=rid,arrival_s=arrival_s,class_id='unlabelled',
        prompt_tokens=n,estimated_remote_tokens=remote,estimated_read_bytes=timing.read_bytes,
        standalone_compute_s=timing.compute_s,
        pressure_Bps=timing.pressure_Bps, reference_service_s=timing.service_s,
        deadline_s=arrival_s + config['request_profile']['slo_multiplier'] * timing.service_s)
