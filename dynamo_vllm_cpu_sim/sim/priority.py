"""EDF 的配置和整数排序键；不包含执行估时或运行时钟。"""
import math
import warnings


def normalize_ordering(config):
    policy=config.get('policy','fcfs')
    mode=config.get('request_ordering','edf' if policy=='priority' else 'fcfs')
    if mode=='budget_priority':
        warnings.warn('budget_priority now means EDF; use request_ordering=edf. '
                      'Historical slack runs require their frozen runtime.', FutureWarning,stacklevel=2)
        mode='edf'
    if mode not in ('fcfs','edf') or policy not in ('fcfs','priority'):
        raise ValueError('ordering must be fcfs or edf, policy must be fcfs or priority')
    if (policy=='priority') != (mode=='edf'):
        raise ValueError('policy=priority requires EDF; policy=fcfs requires FCFS')
    config['request_ordering']=mode
    return mode


def edf_priority(deadline_s):
    """绝对模型秒转整数纳秒，使用原生 Python/vLLM 的64位传输范围。"""
    if type(deadline_s) not in (int,float) or not math.isfinite(deadline_s) or deadline_s<0:
        raise ValueError('EDF deadline must be finite and nonnegative')
    value=round(deadline_s*1_000_000_000)
    if not 0<=value<2**63:
        raise ValueError('EDF deadline outside signed 64-bit nanosecond range')
    return value
