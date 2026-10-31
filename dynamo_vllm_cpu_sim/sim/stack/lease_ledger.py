"""存储带宽租约账本：每盘一本，五步放行 + 两护栏 + TTL + 周期对账。

设计来源：docs/存储带宽租约调度实现方案-20261031.md（第四臂 tfs-lease）。
- 账目单位是 B/s 需求份额，不是并发条数。
- 份额从获准（acquire 原子点）计入、末层读毕（lease_completed）或
  回滚/TTL/对账兜底时移出。获准到读开始只隔一个调度步，属保守单侧误差。
- 例外路的拉伸预测复用 GroupedStorage.weighted_waterfill 同一函数
  （E26f 教训：记账口径必须与真实分配同构）。
当前合同限定 disks=1；结构按盘键留，多盘仅是份额表扩展。
"""
import math

from sim.grouped_storage import weighted_waterfill


class LeaseEntry:
    """一条持租约重读的账目行；时间均为模型秒。"""

    __slots__ = ("rid", "worker", "permit_id", "disk", "share_Bps", "remote_blocks",
                 "read_bytes", "compute_s", "service_s", "deadline_s", "granted_s",
                 "started_s", "expires_s", "path")

    def __init__(self, rid, worker, permit_id, disk, share_Bps, remote_blocks,
                 read_bytes, compute_s, service_s, deadline_s, granted_s, expires_s):
        self.rid, self.worker, self.permit_id, self.disk = rid, worker, permit_id, disk
        self.share_Bps, self.remote_blocks, self.read_bytes = share_Bps, remote_blocks, read_bytes
        self.compute_s, self.service_s, self.deadline_s = compute_s, service_s, deadline_s
        self.granted_s, self.started_s, self.expires_s = granted_s, None, expires_s
        self.path = "normal"

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


class LeaseLedger:
    """单盘（现合同）带宽账本与放行规则；只在账房事件循环线程内被调用。"""

    def __init__(self, lease_config, disk_Bps, disks):
        if disks != 1:
            raise ValueError("lease ledger contract currently requires a single shared disk")
        self.disk_Bps = float(disk_Bps)
        self.line_Bps = self.disk_Bps * lease_config["line_factor_normal"]
        self.exception_Bps = self.disk_Bps * lease_config["line_factor_exception"]
        self.heavy_line_Bps = self.disk_Bps * lease_config["line_factor_heavy"]
        self.loading_slots = lease_config["loading_slots_per_worker"]
        self.ttl_slack = lease_config["ttl_slack_factor"]
        self.ttl_margin_s = lease_config["ttl_margin_s"]
        self.reconcile_interval_s = lease_config["reconcile_interval_s"]
        self.open = {}
        self.worker_open = {}
        self.light_rate_Bps = 0.0
        self.warmup = False
        self.service_samples = []
        self.counters = dict(normal=0, exception=0, guardrail_empty=0,
                             denied_exception_band=0, denied_condition_a=0,
                             denied_condition_b=0, denied_loading=0,
                             started=0, completed=0, ttl_expired=0, reconciles=0)
        self.last_reconcile = None

    # ------------------------------------------------------------------
    # 聚合量
    # ------------------------------------------------------------------
    def heavy_sum(self, disk=0):
        return sum(e.share_Bps for e in self.open.values() if e.disk == disk)

    def total_Bps(self, disk=0):
        return self.heavy_sum(disk) + self.light_rate_Bps

    def delta_s(self):
        """紧迫窗口 Δ = P95(重读参考服务时长)；样本不足时用最大值。"""
        if not self.service_samples:
            return None
        ordered = sorted(self.service_samples)
        return ordered[min(len(ordered) - 1, math.floor(0.95 * (len(ordered) - 1)))]

    # ------------------------------------------------------------------
    # waterfill 同构预测
    # ------------------------------------------------------------------
    @staticmethod
    def _alloc_rates(entries, capacity):
        """与存储侧一致的每块速率；请求聚合速率 = 每块速率 × 块数。"""
        if not entries:
            return {}
        demands = [e.share_Bps / e.remote_blocks for e in entries]
        weights = [e.remote_blocks for e in entries]
        per_block = weighted_waterfill(capacity, demands, weights)
        return {e.rid: per_block[i] * e.remote_blocks for i, e in enumerate(entries)}

    @staticmethod
    def _remaining_bytes(entry, now, rates):
        """剩余字节估计：按当前分配速率折算已读量，单侧保守（偏高）。"""
        elapsed = max(0.0, now - entry.granted_s)
        rate = rates.get(entry.rid, 0.0)
        consumed = min(entry.read_bytes, elapsed * rate)
        return entry.read_bytes - consumed

    def predict(self, entries, now):
        """含候选集合的水fill重放：返回 (剩余字节, 预测读毕时刻, 聚合速率)。"""
        rates = self._alloc_rates(entries, self.disk_Bps)
        result = []
        for e in entries:
            remaining = self._remaining_bytes(e, now, rates)
            completion = now + (remaining / rates[e.rid] if rates[e.rid] > 0 else math.inf)
            result.append((e, remaining, completion))
        return result

    def free_time_for(self, share_Bps, now, candidate=None):
        """新份额最早可加入时刻：含候选占用，按同构预测的完成顺序走账本回落。"""
        entries = list(self.open.values()) + ([candidate] if candidate is not None else [])
        total = self.total_Bps() + (candidate.share_Bps if candidate is not None else 0.0)
        if total <= self.line_Bps:
            return now
        predictions = sorted(self.predict(entries, now), key=lambda row: row[2])
        released = 0.0
        for entry, _remaining, completion in predictions:
            released += entry.share_Bps
            if total - released + share_Bps <= self.line_Bps:
                return completion
        return math.inf

    # ------------------------------------------------------------------
    # 五步放行 + 护栏
    # ------------------------------------------------------------------
    def evaluate(self, cand, urgents, now):
        """cand: LeaseEntry 候选；urgents: [{share_Bps,deadline_s,service_s}]。

        返回 dict(granted, reason, path, k, sum_after, detail)。
        reason ∈ {granted, full, worker_loading_full}；ReadGate 只认识后两个为可重试拒绝。
        """
        disk = cand.disk
        loading = self.worker_open.get(cand.worker, 0)
        if loading >= self.loading_slots:
            self.counters["denied_loading"] += 1
            return dict(granted=False, reason="worker_loading_full", path=None, k=None,
                        sum_after=self.total_Bps(disk), detail="loading slots")
        if self.warmup:
            return self._grant(cand, now, "normal", 1.0)
        sum_after = self.heavy_sum(disk) + cand.share_Bps + self.light_rate_Bps
        k = max(1.0, sum_after / self.disk_Bps)
        # 步1 正常路（护栏2"盘有富余必放"被 line ≥ 容量覆盖，不单独出现）。
        if sum_after <= self.line_Bps:
            return self._grant(cand, now, "normal", k)
        # 护栏1：盘账为空必放一条（优先于一切）。
        if not any(e.disk == disk for e in self.open.values()):
            return self._grant(cand, now, "guardrail_empty", k)
        # 步2 界外。
        if sum_after > self.exception_Bps:
            self.counters["denied_exception_band"] += 1
            return dict(granted=False, reason="full", path=None, k=k,
                        sum_after=sum_after, detail="exception band")
        # 步3-4 条件(a)：同构重放预测在读者完成时刻。只有"不加候选仍能达标"
        # 的在读者才有一票否决权——已注定超期者不再阻塞他人（饱和工况实测
        # 修正：否则少数巨请求长期否决全部新进入，mean TTFT 单调劣化）。
        current_entries = [e for e in self.open.values() if e.disk == disk]
        doomed = set()
        for entry, remaining, completion in self.predict(current_entries, now):
            remaining_compute = entry.service_s * (
                remaining / entry.read_bytes if entry.read_bytes else 0.)
            if completion + remaining_compute > entry.deadline_s + 1e-9:
                doomed.add(entry.rid)
        for entry, remaining, completion in self.predict(current_entries + [cand], now):
            if entry is cand or entry.rid in doomed:
                continue
            remaining_compute = entry.service_s * (
                remaining / entry.read_bytes if entry.read_bytes else 0.)
            if completion + remaining_compute > entry.deadline_s + 1e-9:
                self.counters["denied_condition_a"] += 1
                return dict(granted=False, reason="full", path=None, k=k,
                            sum_after=sum_after, detail=f"condition a:{entry.rid}")
        # 步5 条件(b)：队内紧迫者不被推过线（候选占用计入回落预测）。
        for j in urgents:
            free = self.free_time_for(j["share_Bps"], now, candidate=cand)
            if free > j["deadline_s"] - j["service_s"] + 1e-9:
                self.counters["denied_condition_b"] += 1
                return dict(granted=False, reason="full", path=None, k=k,
                            sum_after=sum_after, detail=f"condition b:{j['rid']}")
        return self._grant(cand, now, "exception", k)

    def _grant(self, cand, now, path, k):
        cand.path = path
        # TTL 只是丢消息兜底：读可能被共享带宽大幅拉长（重读需求速率可超盘带宽，
        # bytes/d 远小于实际读时），按 service/独占下界的较大倍数取值，
        # 正常路径永不触发；迟到读毕事件被容忍，不会误杀运行。
        base = max(cand.service_s, cand.read_bytes / self.disk_Bps, cand.compute_s)
        ttl = base * self.ttl_slack + self.ttl_margin_s
        cand.expires_s = now + ttl
        self.counters["guardrail_empty" if path == "guardrail_empty" else path] += 1
        return dict(granted=True, reason="granted", path=path, k=k,
                    sum_after=self.total_Bps(cand.disk), ttl_s=ttl,
                    detail=f"path={path}")

    # ------------------------------------------------------------------
    # 账目生命周期
    # ------------------------------------------------------------------
    def admit(self, entry):
        self.open[entry.rid] = entry
        self.worker_open[entry.worker] = self.worker_open.get(entry.worker, 0) + 1

    def close(self, rid):
        entry = self.open.pop(rid, None)
        if entry is None:
            return None
        count = self.worker_open.get(entry.worker, 1) - 1
        if count > 0:
            self.worker_open[entry.worker] = count
        else:
            self.worker_open.pop(entry.worker, None)
        self.service_samples.append(entry.service_s)
        return entry

    def sweep_ttl(self, now):
        """TTL 兜底回收：读毕消息丢失时账目仍能归零。"""
        expired = [rid for rid, e in self.open.items() if now >= e.expires_s]
        for rid in expired:
            self.close(rid)
        self.counters["ttl_expired"] += len(expired)
        return expired

    def next_deadline(self, now):
        """最早 TTL 到期时刻；账房 pump 据此定时。"""
        targets = [e.expires_s for e in self.open.values() if e.expires_s > now]
        return min(targets) if targets else None

    def reconcile(self, actual_rate_Bps, now):
        """用存储侧每盘实际流量校正轻读聚合（重读按 waterfill 期望速率折算）。"""
        entries = [e for e in self.open.values()]
        heavy_expected = sum(self._alloc_rates(entries, self.disk_Bps).values())
        estimate = max(0.0, actual_rate_Bps - heavy_expected)
        # EMA：对账间隔远小于轻读起伏尺度，取 0.5 平衡响应与抖动。
        self.light_rate_Bps = (0.5 * estimate if self.light_rate_Bps == 0.0
                               else 0.5 * estimate + 0.5 * self.light_rate_Bps)
        self.counters["reconciles"] += 1
        self.last_reconcile = dict(t=now, actual_rate_Bps=actual_rate_Bps,
                                   heavy_expected_Bps=heavy_expected,
                                   light_rate_Bps=self.light_rate_Bps)
        return self.last_reconcile

    def snapshot(self):
        return dict(disk_Bps=self.disk_Bps, line_Bps=self.line_Bps,
                    exception_Bps=self.exception_Bps, heavy_line_Bps=self.heavy_line_Bps,
                    loading_slots=self.loading_slots, warmup=self.warmup,
                    open={rid: e.as_dict() for rid, e in self.open.items()},
                    worker_open=dict(self.worker_open), light_rate_Bps=self.light_rate_Bps,
                    heavy_sum_Bps=self.heavy_sum(), total_Bps=self.total_Bps(),
                    delta_s=self.delta_s(), counters=dict(self.counters),
                    last_reconcile=self.last_reconcile)
