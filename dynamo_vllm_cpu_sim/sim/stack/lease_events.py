"""Worker 侧租约事件：只对持开放许可的重读发 lease_started / lease_completed。

启用条件：SIM_STACK_LEASE_EVENTS=1（run_tfs_stack 仅对 tfs-lease 臂的 worker 设置）。
开放许可表由 ReadGate 在获准时登记；读开始/末层读毕的执行点调用 notify_*。
消息走既有准入控制 RPC（FeedbackClient.control），失败即关闭连接并向上抛错，
不猜测、不重放。轻读/零读不进入本表，零消息。
"""
import os
import time

_open_permits = {}
_started = set()
_client = None
_enabled = None


def permit_granted(rid, permit_id):
    _open_permits[rid] = permit_id


def permit_removed(rid):
    _open_permits.pop(rid, None)
    _started.discard(rid)


def has_open(rid):
    return rid in _open_permits


def forget(rid):
    _open_permits.pop(rid, None)
    _started.discard(rid)


def _lease_mode():
    global _enabled
    if _enabled is None:
        _enabled = os.environ.get("SIM_STACK_LEASE_EVENTS") == "1"
    return _enabled


def _client_get():
    global _client
    if _client is None:
        from sim.stack.feedback import feedback_client
        _client = feedback_client()
    return _client


def _control(op, rid, permit_id):
    client = _client_get()
    if client is None:
        raise RuntimeError("lease events require the admission control endpoint")
    client.control(op, rid, permit_id=permit_id,
                   occurred_mono=time.monotonic(), clock_id=client.clock_id)


def notify_started(rid):
    """读开始：首层读实际提交之后调用（layerwise 第0层 submit / bulk submit）。

    同一租约只发一次；分块 prefill 的后续执行步不再重复。
    """
    if not _lease_mode() or rid not in _open_permits or rid in _started:
        return
    _started.add(rid)
    _control("lease_started", rid, _open_permits[rid])


def notify_completed(rid):
    """末层读毕：最后一层读 ready 时调用；不等计算完成、不等首 token。"""
    if not _lease_mode():
        return
    permit = _open_permits.pop(rid, None)
    _started.discard(rid)
    if permit is None:
        return
    _control("lease_completed", rid, permit)


def reset_for_test():
    """测试隔离用；运行期不调用。"""
    global _client, _enabled
    _open_permits.clear()
    _started.clear()
    _client = None
    _enabled = None
