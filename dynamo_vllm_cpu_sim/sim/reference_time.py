"""参考服务时间接口：模型独立估算计算，协议决定读算重叠。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import math

from sim.compute_time import get_compute_time_model


@dataclass(frozen=True)
class ReferenceTime:
    compute_s: float
    read_s: float
    service_s: float
    read_bytes: int
    pressure_Bps: float
    model: str


class ReferenceTimeEstimator(ABC):
    name = None

    def __init__(self, config):
        self.config = config
        self.compute = get_compute_time_model(config['compute'])

    @abstractmethod
    def effective_bandwidth(self):
        """参考带宽，不读取当前存储服务内部状态。"""

    def pressure(self, read_bytes, compute_s):
        return read_bytes/compute_s

    def estimate(self, prompt_tokens, cached_tokens, *, remote_read_tokens=None):
        if not 0 <= cached_tokens < prompt_tokens:
            raise ValueError('reference prefix must leave at least one new token')
        c=self.config
        budget=c['request_profile']['profile_token_budget']
        if type(budget) is not int or budget<=0:
            raise ValueError('reference chunk budget must be positive')
        chunks=[];h=cached_tokens
        while h<prompt_tokens:
            u=min(budget,prompt_tokens-h)
            chunks.append(self.compute.estimate_step([(h,u)]))
            h+=u
        compute_s=math.fsum(t['duration_s'] for t in chunks)
        if remote_read_tokens is None:
            remote_read_tokens = cached_tokens
        if type(remote_read_tokens) is not int or not 0 <= remote_read_tokens <= cached_tokens:
            raise ValueError('remote read tokens must be a subset of cached tokens')
        read_bytes=remote_read_tokens*c['storage']['kv_bytes_per_token']
        bandwidth=self.effective_bandwidth()
        if not math.isfinite(bandwidth) or bandwidth<=0:
            raise ValueError('reference bandwidth must be positive and finite')
        from sim.stack.layerwise import transfer_mode, layer_spec, layer_durations
        mode=transfer_mode(c)
        if not read_bytes:
            read_s=0.
            service_s=compute_s
        elif mode=='bulk':
            read_s=(c['storage']['read_latency_s']+read_bytes/bandwidth) if read_bytes else 0.
            service_s=read_s+compute_s
        else:
            spec=layer_spec(c)
            durations=layer_durations(chunks[0],c)
            layers=len(durations)
            startup=c['storage']['read_latency_s'] if read_bytes else 0.
            reads=[read_bytes/layers/bandwidth]*layers
            for i in range(layers):
                if i==0 or spec['startup']=='per_layer':reads[i]+=startup
            read_s=math.fsum(reads)
            if mode=='layer_serial':
                first_service=read_s+chunks[0]['duration_s']
            else:
                first_service=(reads[0]+math.fsum(max(reads[i],durations[i-1])
                    for i in range(1,layers))+durations[-1])
            # 外部前缀只读一次；首个 chunk 后执行器已将其标记为驻留。
            service_s=first_service+math.fsum(t['duration_s'] for t in chunks[1:])
        if not math.isfinite(service_s) or min(compute_s,service_s)<=0:
            raise ValueError('reference time must be positive and finite')
        return ReferenceTime(compute_s,read_s,service_s,read_bytes,
                             self.pressure(read_bytes,compute_s),self.name)


class SAReferenceTimeEstimator(ReferenceTimeEstimator):
    name='sa_reference_v2'

    def effective_bandwidth(self):
        return min(self.config['admission']['reference_bandwidth_Bps'],
                   self.config['storage']['q_max_Bps'])

    def pressure(self, read_bytes, compute_s):
        # SA 原口径为每层字节/每层计算；参考分块后使用每层累计计算。
        return (read_bytes/self.config['compute']['profile']['L']) / (
            compute_s/self.config['compute']['profile']['L'])


class GLM5ReferenceTimeEstimator(ReferenceTimeEstimator):
    name='glm5_reference_v3'

    def effective_bandwidth(self):
        return self.config['admission']['reference_bandwidth_Bps']


class LinearReferenceTimeEstimator(GLM5ReferenceTimeEstimator):
    name='linear_reference_v2'


def get_reference_time_estimator(config):
    mode=config['compute'].get('mode','linear')
    factory={'sa_e26f':SAReferenceTimeEstimator,'glm5_d950pr':GLM5ReferenceTimeEstimator,
             'linear':LinearReferenceTimeEstimator}
    if mode not in factory:
        raise ValueError('unknown reference time model: '+str(mode))
    return factory[mode](config)
