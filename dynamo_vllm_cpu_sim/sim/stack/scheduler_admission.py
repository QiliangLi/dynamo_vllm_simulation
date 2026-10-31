"""Opt-in patch of the pinned real Scheduler before the external KV query.

The source hash and AST anchor fail closed on version drift. The original
scheduler body is retained; only begin-step, acquire/skip, and rollback hooks
are inserted. This deliberately changes scheduling and is not instrumentation.
"""
import ast
import functools
import hashlib
import inspect
import json
import os
from pathlib import Path
import textwrap
import time

from sim.stack.storage_hint import validate_hint
from sim.stack import lease_events

SCHEDULE_SHA256 = "b6a0770641b37718aecf9bd94dc28b6fcca8e944e99a706bf4005b0b5a674914"
# 账房可重试拒绝：额度满 / 每实例加载位满。
DEFERRABLE_REASONS = frozenset({"full", "worker_loading_full"})


class ReadGate:
    def __init__(self, client, retry_interval=0.002, log_path=None, *, idle_bypass=False):
        if client is None:
            raise RuntimeError("read admission requires a control connection")
        self.client = client
        self.retry_interval = retry_interval
        self.full_until = 0.0
        self.full = False
        self.round = 0
        self.permits = {}
        self.pending_allocation = set()
        self.generations = {}
        self.seen = set()
        self.skipped = set()
        self.no_remote = set()
        self.log_path = Path(log_path) if log_path else None
        self.idle_bypass = idle_bypass
        self.force_candidate = None
        self.force_committed = False
        self.idle_proof = None
        if idle_bypass:
            self.client.enable_idle_bypass()

    def log(self, event, **fields):
        if self.log_path:
            with self.log_path.open("a") as out:
                out.write(json.dumps(dict(event=event, monotonic_ns=time.monotonic_ns(),
                                         round=self.round, **fields))+"\n")

    def begin_step(self, scheduler=None):
        self.client.check()  # Surface a late stream failure even on all-light rounds.
        self.round += 1
        self.pending_allocation.clear()
        self.full = time.monotonic() < self.full_until
        self.force_candidate = None
        self.force_committed = False
        if not self.idle_bypass:
            return
        self.client.outbox.check()
        if scheduler is None or scheduler.running:
            return
        queued = list(scheduler.waiting) + list(scheduler.skipped_waiting)
        if not queued:
            return
        # Conservative eligibility: ANY light, already admitted, partially run,
        # or asynchronously blocked request prevents the idle exception. We do
        # not speculate that a light request will fail its native allocation.
        for request in queued:
            hint = self.hint(request)
            if (not hint['is_heavy_read'] or not hint['estimated_remote_read_bytes']
                    or request.request_id in self.permits or request.num_computed_tokens
                    or scheduler._is_blocked_waiting_status(request.status)):
                return
        q = scheduler._select_waiting_queue_for_scheduling()
        self.force_candidate = q.peek_request().request_id
        self.idle_proof = dict(running_before=0, scheduled_tokens_before=0,
                               nonheavy_candidates=0, heavy_candidates=len(queued))

    def hint(self, request):
        params = request.sampling_params
        hint = validate_hint((params.extra_args or {}).get("storage_hint"))
        if request.request_id not in self.seen:
            self.seen.add(request.request_id)
            self.log("storage_hint_received", request_id=request.request_id, hint=hint)
        return hint

    def allow(self, request, external_tokens=None):
        hint = self.hint(request)
        if external_tokens is None:
            # The existing gateway hint decides whether to acquire before the
            # external query. The query itself still determines actual hits.
            external_tokens = hint["estimated_remote_read_bytes"]
        rid = request.request_id
        # Rollback applies only to a permit acquired for THIS allocation call.
        # An async read may already have submitted IO in an earlier round.
        self.pending_allocation.discard(rid)
        if not external_tokens or not hint["is_heavy_read"] or rid in self.permits or rid in self.no_remote:
            return True
        if rid == self.force_candidate:
            return True  # No permit RPC. Commit only after native allocate_slots.
        if self.force_committed:
            return False  # Exactly one forced request in this native round.
        if self.full:
            if rid not in self.skipped:
                self.log("read_skipped_full", request_id=rid)
                self.skipped.add(rid)
            return False
        start = time.monotonic_ns()
        response = self.client.control("acquire", rid,
            acquisition_key=f"{rid}:allocation:{self.generations.get(rid, 0)}",
            load_attempt_id=1, storage_hint=hint)
        self.log("read_acquire_response", request_id=rid, start_ns=start,
                 duration_ns=time.monotonic_ns()-start, **response)
        if response["granted"]:
            self.permits[rid] = response["permit_id"]
            lease_events.permit_granted(rid, response["permit_id"])
            self.pending_allocation.add(rid)
            return True
        if response.get("reason") not in DEFERRABLE_REASONS:
            raise RuntimeError(f"unexpected acquire response: {response}")
        self.full = True
        self.full_until = time.monotonic()+self.retry_interval
        return False

    def rollback(self, request):
        rid = request.request_id
        permit = self.permits.get(rid)
        if permit is None or rid not in self.pending_allocation:
            return
        self.client.control("rollback", rid, permit_id=permit)
        del self.permits[rid]
        lease_events.permit_removed(rid)
        self.pending_allocation.discard(rid)
        self.generations[rid] = self.generations.get(rid, 0)+1
        self.log("read_permit_rollback", request_id=rid, permit_id=permit)

    def after_allocation(self, request, external_tokens, scheduled_tokens):
        if request.request_id != self.force_candidate or not external_tokens:
            return
        if scheduled_tokens:
            raise RuntimeError('idle exception would join existing scheduled work')
        allocated_mono = time.monotonic()
        proof = dict(self.idle_proof, allocated=True, external_tokens=external_tokens,
                     allocation_completed_mono=allocated_mono)
        token, decision_mono = self.client.force_admit_async(request.request_id,
                                                           self.hint(request), proof)
        self.permits[request.request_id] = token
        self.force_candidate = None
        self.force_committed = True
        self.log('idle_bypass_committed', request_id=request.request_id, permit_id=token,
                 proof=proof, decision_mono=decision_mono, allocation_completed_mono=allocated_mono,
                 admission_waited=False)

    def resolved_no_remote(self, request):
        if (os.environ.get('SIM_STACK_ROUTING_SCORE_READY') and self.hint(request)['is_heavy_read']
                and request.request_id not in self.no_remote and request.request_id not in self.permits):
            self.client.control('routing_ready',request.request_id)
            self.no_remote.add(request.request_id)


def patch_schedule(original):
    source = inspect.getsource(original)
    if hashlib.sha256(source.encode()).hexdigest() != SCHEDULE_SHA256:
        raise RuntimeError("unrecognized Scheduler.schedule source; do not apply admission patch")
    tree = ast.parse(textwrap.dedent(source))
    fn = tree.body[0]
    fn.body.insert(0, ast.parse("self._read_gate.begin_step(self)").body[0])
    # Pin both boundaries: the same waiting admission path, earlier acquisition.
    loops = [node for node in ast.walk(fn) if isinstance(node, ast.While)
             and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "allocate_slots"
                     and any(k.arg == "new_computed_blocks" for k in n.keywords)
                     for n in ast.walk(node))]
    if len(loops) != 1:
        raise RuntimeError("expected one waiting allocation loop")
    loop = loops[0]
    queries = [node for node in ast.walk(loop) if isinstance(node, ast.If)
               and ast.unparse(node.test) == "self.connector is not None"
               and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "get_num_new_matched_tokens"
                       for n in ast.walk(node))]
    if len(queries) != 1:
        raise RuntimeError("expected one external KV query anchor")
    query = queries[0]
    hook = ast.parse('''
if not self._read_gate.allow(request):
    request_queue.pop_request()
    step_skipped_waiting.prepend_request(request)
    continue
''').body[0]
    # An acquired permit must not survive an original pre-allocation exit.
    # Stop before connector.update_state_after_alloc: IO may be submitted there.
    allocation = [i for i, node in enumerate(loop.body)
                  if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                  and isinstance(node.value.func, ast.Attribute)
                  and node.value.func.attr == "allocate_slots"]
    if len(allocation) != 1:
        raise RuntimeError("expected one waiting allocation anchor")
    end = allocation[0] + 1
    if ast.unparse(loop.body[end].test) != "new_blocks is None":
        raise RuntimeError("allocation failure branch changed")
    query_index = next(i for i, node in enumerate(loop.body)
                       if any(n is query for n in ast.walk(node)))

    class RollbackExits(ast.NodeTransformer):
        def visit_Break(self, node):
            return [ast.parse("self._read_gate.rollback(request)").body[0], node]

        def visit_Continue(self, node):
            return [ast.parse("self._read_gate.rollback(request)").body[0], node]

    for node in loop.body[query_index:end+1]:
        RollbackExits().visit(node)
    query.body.insert(0, hook)
    query.body.append(ast.parse('''
if num_external_computed_tokens == 0:
    self._read_gate.rollback(request)
    self._read_gate.resolved_no_remote(request)
''').body[0])
    loop.body.insert(end+1, ast.parse('''
self._read_gate.after_allocation(request, num_external_computed_tokens, num_scheduled_tokens)
''').body[0])
    ast.fix_missing_locations(tree)
    namespace = {}
    exec(compile(tree, "<storage-read-admission-v1>", "exec"), original.__globals__, namespace)
    result = namespace[original.__name__]
    result._storage_read_admission = True
    return result


def install_scheduler_adapter():
    from vllm.v1.core.sched.scheduler import Scheduler
    from sim.stack.executor import load_stack_config
    from sim.stack.feedback import feedback_client
    if getattr(Scheduler.schedule, "_storage_read_admission", False):
        return
    original_init = Scheduler.__init__
    original_add = Scheduler.add_request
    patched = patch_schedule(Scheduler.schedule)

    @functools.wraps(original_init)
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        cfg = load_stack_config()
        config_path = Path(os.environ["SIM_STACK_CONFIG"])
        worker = os.environ["SIM_STACK_WORKER_ID"]
        self._read_gate = ReadGate(feedback_client(),
            cfg["admission"].get("retry_interval_wall_s", 0.002),
            config_path.parent/f"worker{worker}.read_admission.jsonl",
            idle_bypass=cfg['admission'].get('idle_bypass',False))

    @functools.wraps(original_add)
    def add_request(self, request):
        self._read_gate.hint(request)  # Missing/malformed metadata fails at ingress.
        return original_add(self, request)

    Scheduler.__init__ = initialize
    Scheduler.add_request = add_request
    Scheduler.schedule = patched
