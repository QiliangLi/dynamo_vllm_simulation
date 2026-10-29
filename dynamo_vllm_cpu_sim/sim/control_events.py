"""带可见时刻的请求级反馈；物理完成与控制器观测分离。"""

from dataclasses import dataclass, field
import math

KINDS = frozenset(("load_submitted", "layer_load_submitted", "load_completed", "no_external_load",
                   "first_token", "request_finished"))


@dataclass(frozen=True)
class ControlEvent:
    event_id: str
    kind: str
    request_id: str
    worker_id: int
    occurred_s: float
    visible_s: float
    payload: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in KINDS or not self.event_id:
            raise ValueError("unknown control event")
        if (not math.isfinite(self.occurred_s) or not math.isfinite(self.visible_s)
                or self.occurred_s < 0 or self.visible_s < self.occurred_s):
            raise ValueError("invalid control event time")
