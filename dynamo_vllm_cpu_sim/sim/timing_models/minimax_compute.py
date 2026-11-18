"""Adapt the fitted MiniMax model to one request's reference compute time."""

from functools import lru_cache

from minimax_prefill import PrefillTimeModel
from .compute_models import ComputeTimeModel


@lru_cache(maxsize=1)
def _model():
    return PrefillTimeModel()


class MiniMaxPrefillCompute(ComputeTimeModel):
    mode = 'prefill_fit'

    def _estimate_step(self, shapes, decode_tokens):
        if len(shapes) != 1 or decode_tokens:
            raise ValueError('MiniMax prefill_fit estimates one prefill request at a time')
        hit, miss = shapes[0]
        model = _model()
        estimate = model.estimate(hit, miss)
        return dict(mode=self.mode, duration_s=estimate.compute_s, shapes=[(hit, miss)],
                    compute_time_source=estimate.source, model_sha256=model.model_sha256)
