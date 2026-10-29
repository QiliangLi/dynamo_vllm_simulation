"""SA admission semantics shared by DES and the real process stack."""
from sim.admission import AdmissionController


class SAAdmission(AdmissionController):
    """沿用已验证的租约框架，零 P75 时严格采用 SA 的无限配额语义。"""

    def is_heavy(self, profile):
        return self.theta is not None and profile.pressure_Bps >= self.theta

    def on_arrivals(self, profiles, now_s):
        profiles = list(profiles)
        if not profiles:
            return
        super().on_arrivals(profiles, now_s)
        log = self.logs[-1]
        if log["raw_quantile"] == 0:
            self.theta, self.cap = 0.0, 1 << 30
            log.update(theta=self.theta, cap=self.cap, held=self.held,
                       reclassified_over_cap=False, zero_rule="sa_zero_threshold_unlimited")
