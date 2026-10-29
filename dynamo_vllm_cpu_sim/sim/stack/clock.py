"""Wall seconds per model second for the process-based simulation only.

Model inputs, resource rates, deadlines and profiles retain their original SI
units. A factor K slows modeled execution by K in wall time. Real protocol,
Python and OS overhead is not multiplied; its normalized cost decreases by K.
This is time dilation, not a synchronized discrete-event or virtual clock.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class TimeScale:
    factor: float = 1.

    def __post_init__(self):
        if (isinstance(self.factor, bool) or not isinstance(self.factor, (int, float))
                or not math.isfinite(self.factor) or self.factor < 1):
            raise ValueError("wall_time_scale must be finite and >= 1")

    @classmethod
    def from_config(cls, config):
        return cls(config.get("wall_time_scale", 1.))

    def to_wall(self, model_seconds):
        return model_seconds * self.factor

    def to_model(self, wall_seconds):
        return wall_seconds / self.factor
