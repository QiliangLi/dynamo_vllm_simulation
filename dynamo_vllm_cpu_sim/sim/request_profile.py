"""只根据已到达请求的公开字段建立冻结画像；时间秒、容量字节、带宽 B/s。"""

from copy import deepcopy
from dataclasses import dataclass
import math
from sim.compute_time import validate_compute
from sim.reference_time import get_reference_time_estimator
from sim.priority import normalize_ordering
import warnings


@dataclass(frozen=True)
class RequestProfile:
    request_id: str
    arrival_s: float
    class_id: str
    prompt_tokens: int
    estimated_remote_tokens: int
    estimated_read_bytes: int
    standalone_compute_s: float
    pressure_Bps: float
    reference_service_s: float
    deadline_s: float


def normalize_config(config):
    """补齐旧配置的显式默认值，并在创建真实引擎前校验实验参数。"""
    c = deepcopy(config)
    if c["compute"].get("mode") == "glm5_d950pr":
        from sim.timing_models.prefill_time_batch import VERSION, DIRECT_SUM_VERSION
        compute = c["compute"]
        compute.setdefault("tp", 1)
        compute.setdefault("dp", 8)
        compute.setdefault("ep", compute["tp"] * compute["dp"])
        compute.setdefault("kv_hbm_reuse", True)
        compute.setdefault("batch_time_model", VERSION)
        if compute["batch_time_model"] == DIRECT_SUM_VERSION:
            compute.setdefault("launch_saving_fraction", 1.0)
    normalize_ordering(c)
    if "cache_model" in c and c["cache_model"] != "first_seen":
        raise ValueError("cache_model only supports first_seen")
    if c.get("cache_model") == "first_seen" and c.get("enable_prefix_caching",False):
        raise ValueError("first_seen requires enable_prefix_caching=false")
    c["storage"].setdefault("bandwidth_policy", "active_split")
    c["storage"].setdefault("shared_link_Bps", None)
    nominal = c["storage"]["disks"] * c["storage"]["disk_Bps"]
    link = c["storage"]["shared_link_Bps"]
    # Scalar admission reference only; never used as a pooled storage resource.
    reference = nominal if link is None else link
    a = {"mode": "immediate", "quantile": 0.75,
         "reference_bandwidth_Bps": reference,
         "release_on": "first_token", "aging_enabled": True,
         "aging_wait_s": 0.5, "aging_cooldown_s": 0.05, "feedback_delay_s": 0.0,
         "placement": "gateway", "classification": "dynamic",
         "retry_interval_wall_s": 0.002, "rpc_transport": "dynamo", "rpc_timeout_wall_s": 5.0,
         "idle_bypass": False}
    a.update(c.get("admission", {}))
    if a['rpc_transport'] not in ('dynamo','tcp','unix'):
        raise ValueError('admission.rpc_transport must be dynamo, tcp or unix')
    if (type(a['rpc_timeout_wall_s']) not in (int,float)
            or not math.isfinite(a['rpc_timeout_wall_s']) or a['rpc_timeout_wall_s'] <= 0):
        raise ValueError('admission.rpc_timeout_wall_s must be finite and positive')
    if a["mode"] == "tfs_no_aging":
        a.update(mode="tfs", aging_enabled=False)
    if a["mode"] not in ("immediate", "slack_only", "quota_fcfs", "tfs"):
        raise ValueError("unknown admission.mode")
    if a["release_on"] not in ("first_token", "load_done", "lease_events"):
        raise ValueError("release_on must be first_token, load_done or lease_events")
    if a["placement"] not in ("gateway", "scheduler") or a["classification"] not in (
            "dynamic", "arrival", "bandwidth_line"):
        raise ValueError("invalid admission placement or classification")
    if (not isinstance(a["retry_interval_wall_s"], (int, float))
            or not math.isfinite(a["retry_interval_wall_s"]) or a["retry_interval_wall_s"] < 0):
        raise ValueError("retry_interval_wall_s must be finite and nonnegative")
    if a["placement"] == "scheduler":
        v1 = (a["classification"] == "arrival" and a["release_on"] == "first_token")
        lease = (a["classification"] == "bandwidth_line" and a["release_on"] == "lease_events")
        if not v1 and not lease or a["mode"] != "tfs" or a["aging_enabled"]:
            raise ValueError("scheduler admission requires tfs and aging off; "
                             "v1 pairs arrival+first_token, lease pairs bandwidth_line+lease_events")
        if c.get("scheduler", "upstream") != "upstream":
            raise ValueError("scheduler admission v1 supports the pinned upstream scheduler only")
    if a["classification"] == "bandwidth_line":
        sc = c["storage"]
        if (a["mode"] != "tfs" or a["placement"] != "scheduler" or a["aging_enabled"]
                or sc["disks"] != 1):
            raise ValueError("lease admission requires tfs, scheduler placement, "
                             "aging off and the single shared pool contract")
        # 2026-10-31 调参定标（三轮重复采样）：normal=1.2 与 m=10（f=0.1）在
        # mean/p95/完成时间全面优于 1.1/m=4，SLO 在轮间噪声内；random 对
        # normal≥1.5 有 SLO 悬崖（同刻峰值≈1.5×容量），不得放宽到 1.5 以上。
        lease = {"line_factor_heavy": 0.1, "line_factor_normal": 1.2,
                 "line_factor_exception": 1.5, "compute_slots": 10,
                 "loading_slots_per_worker": 4, "ttl_slack_factor": 30.0,
                 "ttl_margin_s": 0.05, "reconcile_interval_s": 0.05,
                 "reconcile_tolerance": 0.1}
        lease.update(c.get("lease", {}))
        for key in ("line_factor_heavy", "line_factor_normal", "line_factor_exception",
                    "ttl_slack_factor", "reconcile_interval_s"):
            if (not isinstance(lease[key], (int, float)) or not math.isfinite(lease[key])
                    or lease[key] <= 0):
                raise ValueError(f"lease.{key} must be finite and positive")
        for key in ("compute_slots", "loading_slots_per_worker"):
            if (not isinstance(lease[key], int) or isinstance(lease[key], bool)
                    or lease[key] <= 0):
                raise ValueError(f"lease.{key} must be a positive integer")
        for key in ("ttl_margin_s", "reconcile_tolerance"):
            if (not isinstance(lease[key], (int, float)) or not math.isfinite(lease[key])
                    or lease[key] < 0):
                raise ValueError(f"lease.{key} must be finite and nonnegative")
        if lease["reconcile_interval_s"] <= 0:
            raise ValueError("lease.reconcile_interval_s must be positive")
        lease["disks"] = sc["disks"]
        a["lease"] = lease
        a["disk_Bps"] = sc["disk_Bps"]
        a["block_size"] = c["block_size"]
        a["heavy_line_Bps"] = sc["disk_Bps"] * lease["line_factor_heavy"]
    if type(a['idle_bypass']) is not bool:
        raise ValueError('admission.idle_bypass must be boolean')
    if a['idle_bypass'] and (a['placement'] != 'scheduler'
            or c.get('kv_transfer_mode', 'bulk') not in ('layer_pipeline', 'layer_serial')
            or a['feedback_delay_s'] != 0):
        raise ValueError('idle_bypass requires scheduler admission, layerwise KV and zero feedback delay')
    for name in ("quantile", "reference_bandwidth_Bps", "aging_wait_s",
                 "aging_cooldown_s", "feedback_delay_s"):
        if not isinstance(a[name], (int, float)) or not math.isfinite(a[name]):
            raise ValueError(f"admission.{name} must be finite")
    if not 0 <= a["quantile"] <= 1 or a["reference_bandwidth_Bps"] <= 0:
        raise ValueError("bad quantile or reference bandwidth")
    if min(a["aging_wait_s"], a["aging_cooldown_s"], a["feedback_delay_s"]) < 0:
        raise ValueError("negative admission time")
    if not isinstance(a["aging_enabled"], bool):
        raise ValueError("aging_enabled must be boolean")
    if a["aging_enabled"] and a["aging_cooldown_s"] <= 0:
        raise ValueError("enabled aging needs positive cooldown")
    profile_mode = {"glm5_d950pr":"glm5_reference_v3","sa_e26f":"sa_reference_v2"}.get(
        c["compute"].get("mode"),"linear_reference_v2")
    p = {"mode": profile_mode, "profile_token_budget": 8192,
         "slo_multiplier": 4.0}
    p.update(c.get("request_profile", {}))
    legacy_modes = ({"glm5_reference_v1", "glm5_reference_v2"}
                    if profile_mode == "glm5_reference_v3"
                    else {profile_mode.replace('_v2', '_v1')})
    if p["mode"] in legacy_modes:
        warnings.warn('request_profile is migrated to '+profile_mode+'; '
                      'old results require their frozen runtime',FutureWarning,stacklevel=2)
        p['mode']=profile_mode
    if p["mode"] != profile_mode:
        raise ValueError("request_profile.mode must match compute.mode")
    if (not isinstance(p["profile_token_budget"], int)
            or isinstance(p["profile_token_budget"], bool)
            or p["profile_token_budget"] <= 0):
        raise ValueError("profile_token_budget must be a positive integer")
    if not math.isfinite(p["slo_multiplier"]) or p["slo_multiplier"] <= 0:
        raise ValueError("slo_multiplier must be positive and finite")
    for name in ("workers", "block_size", "num_blocks", "max_model_len",
                 "max_num_seqs", "max_num_batched_tokens"):
        if not isinstance(c[name], int) or isinstance(c[name], bool) or c[name] <= 0:
            raise ValueError(f"{name} must be a positive integer")
    validate_compute(c["compute"])
    from sim.stack.layerwise import layer_spec
    layer_spec(c)
    if c["compute"].get("mode") == "glm5_d950pr" and c["max_num_seqs"] > 8:
        raise ValueError("glm5_d950pr requires max_num_seqs <= 8")
    for name in ("disks", "paths_per_disk", "disk_Bps",
                 "path_Bps", "kv_bytes_per_token"):
        if not math.isfinite(c["storage"][name]) or c["storage"][name] <= 0:
            raise ValueError(f"bad storage.{name}")
    if link is not None and (not math.isfinite(link) or link <= 0):
        raise ValueError("storage.shared_link_Bps must be positive or null")
    if (not math.isfinite(c["storage"]["read_latency_s"])
            or c["storage"]["read_latency_s"] < 0):
        raise ValueError("bad read_latency_s")
    c["admission"], c["request_profile"] = a, p
    from sim.stack.routing_accounting import normalize as normalize_routing_accounting
    normalize_routing_accounting(c)
    return c


def build_profile(row, c):
    """n−1 上限对应 SimConnector；不读取本地缓存、未来负载或实际完成时间。"""
    n, b = len(row["prompt_token_ids"]), c["block_size"]
    remote = min(row.get("remote_prefix_tokens", 0), ((n - 1) // b) * b)
    read_bytes = remote * c["storage"]["kv_bytes_per_token"]
    timing=get_reference_time_estimator(c).estimate(n,remote)
    return RequestProfile(row["id"],row["arrival_s"],row.get("class_id","unlabelled"),
        n,remote,read_bytes,timing.compute_s,timing.pressure_Bps,timing.service_s,
        row["arrival_s"]+c["request_profile"]["slo_multiplier"]*timing.service_s)
