"""One generation of the search (section 6) and the loop around it (sections 8 and 12).

A generation has five steps: open, analyst, propose, pipeline, meta. In the
pipeline every candidate advances by its own status:

    drafted → (red team) → reviewed → (static gate, smoke) → screen → confirm
            → promotable → (audit) → audited → (judge, cross-check) → champion | kept
    ready → (static gate) → screen → ...          (a parameter child of the champion)

Any candidate can be sent back to ``revising`` twice, and is rejected when that
fails. ``Search.step`` advances by one unit of work and returns; it is safe to
call it again after a crash.
"""
import copy
import json
import random
import shutil
import statistics
import subprocess
import sys
import time

from . import config, evaluator, gates, roles, wsl
from .report import (_mean_digest, baseline_series, digests_of, leave_one_out, open_tier_of,
                     reference_series, series, stress_results, write_baseline_digest,
                     write_champion_digest, write_context, write_generation_report, write_run_digests)
from .store import read_json, write_json

ATTACK_DEFAULT = ["bw10_540", "arr_fast_540", "heavy_only", "zero_read"]
ATTACK_PARAM = ["bw10_540", "arr_fast_540"]
ACTIVE = ("drafted", "revising", "reviewed", "ready", "smoke", "screen", "confirm", "promotable", "audited")
REPLAY_TAGS = ("smoke", "screen", "confirm", "attack", "final")


class Search:
    def __init__(self, daemon):
        self.d = daemon
        self.state = daemon.state
        self.ws = daemon.ws

    # ------------------------------------------------------------ plumbing

    def step(self):
        getattr(self, "step_" + self.state["gen"]["step"])()

    def log(self, event, **fields):
        self.d.log(event, gen=self.state["gen"]["n"], **fields)

    @property
    def read_shell(self):
        """Read-only commands, plus the analysis tools the orchestrator has validated."""
        if any(tool.get("ok") for tool in (self.state.get("tools") or {}).values()):
            return roles.TOOL_SHELL
        return roles.READ_SHELL

    def cand(self, cand_id):
        return self.state["candidates"][cand_id]

    def cand_dir(self, cand_id):
        return self.ws / "candidates" / cand_id

    def generation(self):
        return self.state["candidates"] if False else self.state["gen"]["n"]

    def members(self):
        gen = self.state["gen"]
        return list(gen.get("llm") or []) + list(gen.get("param") or [])

    def active(self):
        return [c for c in self.members() if self.cand(c)["status"] in ACTIVE]

    def new_candidate(self, **fields):
        self.state["cand_seq"] = self.state.get("cand_seq", 0) + 1
        cand_id = "c%04d" % self.state["cand_seq"]
        self.state["candidates"][cand_id] = dict(id=cand_id, created=time.time(), runs={}, queued={}, gate=None,
                                                 reason="", score=None, fail={}, **fields)
        self.cand_dir(cand_id).mkdir(parents=True, exist_ok=True)
        self.log("candidate_created", cand=cand_id, candidate_kind=fields.get("kind"))
        return cand_id

    def reject(self, cand_id, reason):
        self.cand(cand_id).update(status="rejected", reason=reason[:700])
        self.log("candidate_rejected", cand=cand_id, reason=reason[:300])

    def failed_replay(self, cand_id):
        """The reason a replay of this candidate failed twice, if any."""
        return self.cand(cand_id).get("run_failed")

    MEASURED_TAGS = ("confirm", "attack")

    def enqueue(self, cand_id, tag, rounds, workloads=None, stress=False, exclusive=None):
        """Queue replays so that every workload ends with ``rounds`` finished runs under ``tag``.

        Section 10: confirmations, attacks and the final runs are measured alone (no other replay
        in flight, zero port offset); screening may share the machine.
        """
        cand = self.cand(cand_id)
        if exclusive is None:
            exclusive = tag in Search.MEASURED_TAGS
        folder = self.cand_dir(cand_id)
        overlay = folder / "overlay"
        policy_path = folder / "policy.json"
        if stress:
            cand["attack_specs"] = list(workloads or [])
        for workload in workloads or list(config.WORKLOADS):
            spec = config.STRESS_SPECS.get(workload) if stress else None
            have = len((cand["runs"].get(workload) or {}).get(tag) or [])
            outstanding = cand["queued"].get((tag, workload), 0)
            for _ in range(max(0, rounds - have - outstanding)):
                self.d.new_run(workload, "candidate", tag, cand=cand_id, gen=cand["gen"],
                               stress=workload if stress else None, workers=(spec or {}).get("workers", 4) if spec else 4,
                               overlay=overlay if overlay.exists() else None,
                               policy=read_json(policy_path) if policy_path.exists() else None,
                               max_tier=cand.get("tier", 0), exclusive=exclusive)
                cand["queued"][(tag, workload)] = outstanding + 1
                outstanding += 1

    def note_queued(self, cand_id, tag, workload):
        cand = self.cand(cand_id)
        cand["queued"][(tag, workload)] = max(0, cand["queued"].get((tag, workload), 0) - 1)

    def complete(self, cand_id, tag, need, stress=False):
        cand = self.cand(cand_id)
        names = cand.get("attack_specs") or [] if stress else list(config.WORKLOADS)
        return bool(names) and all(len((cand["runs"].get(name) or {}).get(tag) or []) >= need for name in names)

    def ensure(self, task_id, role, write_paths, shell, builder, max_attempts=3, **options):
        """'wait' while the task runs, 'ok', or 'failed' after too many attempts."""
        status = self.d.task_state(task_id)
        attempts = self.state["tasks"].get(task_id, {}).get("attempts", 0)
        if status in ("none", "lost") or (status in ("error", "timeout") and attempts < max_attempts):
            if attempts >= max_attempts + 1:
                return "failed"
            self.d.start_task(task_id, role, builder(), write_paths, shell, **options)
            return "wait"
        return "wait" if status == "running" else status

    def crossval(self, task_id, question, inputs, output):
        """One kimi-k3.1-pro opinion within the per-generation cap: 'wait', a dict, or None."""
        book = self.state["crossval"]
        if book.get("gen") != self.state["gen"]["n"]:
            self.state["crossval"] = book = dict(gen=self.state["gen"]["n"], used=0)
        if task_id not in self.state["tasks"]:
            if book["used"] >= config.CROSSVAL_PER_GENERATION:
                self.d.alert("交叉验证已到本代上限，跳过 %s" % task_id)
                return None
            book["used"] += 1
        status = self.ensure(task_id, "crossval", [output], [], lambda: roles.task_book(
            self.ws / "tasks" / task_id, "交叉验证", question, inputs, [(output, "你的独立意见，JSON")],
            ["只回答问题。先自己从材料得出结论，再对比别人的结论。"]), max_attempts=2)
        if status == "wait":
            return "wait"
        try:
            return roles.parse_json((self.ws / output).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return dict(opinion="（交叉验证没有给出可解析的意见）", agree=None)

    # ------------------------------------------------------------ open and diagnose

    def step_open(self):
        gen = self.state["gen"]
        if not gen.get("opened"):
            gen.update(n=gen["n"] + 1, opened=True, llm=[], param=[], promoted=None)
            self.state["crossval"] = dict(gen=gen["n"], used=0)
        generation = gen["n"]
        write_baseline_digest(self.d)
        write_champion_digest(self.d)
        write_run_digests(self.d, generation)
        allocation = self.allocation()
        champion = self.state.get("champion")
        if champion and self.state["candidates"].get(champion):
            for _ in range(allocation["param"]):
                child = self.param_child(champion, generation)
                if child:
                    gen["param"].append(child)
                    self.enqueue(child, "screen", config.SCREEN_RUNS)
        for lab in ("A", "B"):
            gen["llm"].append(self.new_candidate(kind="llm", lab=lab, gen=generation, tier=open_tier_of(self.state),
                                                 status="drafting"))
        gen["step"] = "analyst"

    def allocation(self):
        """Section 8. New mechanisms never fall below the exploration floor."""
        wanted = dict(self.state["meta"].get("allocation") or dict(llm=2, param=2))
        llm = max(1, min(3, int(wanted.get("llm", 2))))
        param = max(0, min(4, int(wanted.get("param", 2))))
        kinds = self.state["runs_by_kind"]
        total = kinds.get("llm", 0) + kinds.get("param", 0)
        if total >= 40 and kinds.get("llm", 0) / total < config.EXPLORE_MIN:
            param, llm = 0, max(llm, 2)
        return dict(llm=llm, param=param)

    def param_child(self, champion, generation):
        """Layer 1: the champion with one or two parameters moved inside their ranges."""
        parent = self.cand(champion)
        params = (parent.get("card") or {}).get("params") or {}
        source = self.cand_dir(champion)
        if not params or not (source / "policy.json").exists():
            return None
        policy, card = read_json(source / "policy.json"), copy.deepcopy(parent["card"])
        for name in random.sample(sorted(params), k=min(len(params), random.choice([1, 1, 2]))):
            spec, node, parts = params[name], policy, name.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            low, high = spec["min"], spec["max"]
            if random.random() < 0.5:
                value = low + (high - low) * random.random()
            else:
                value = min(high, max(low, node.get(parts[-1], spec["value"]) + random.gauss(0, 0.15 * (high - low))))
            node[parts[-1]] = round(value, 6)
            card["params"][name]["value"] = node[parts[-1]]
        child = self.new_candidate(kind="param", lab=None, gen=generation, tier=parent.get("tier", 0), status="ready",
                                   parent=champion, card=card)
        folder = self.cand_dir(child)
        for name in ("overlay", "tests"):
            if (source / name).exists():
                shutil.copytree(source / name, folder / name)
        write_json(folder / "policy.json", policy)
        card["name"] = "%s｜调参 %s" % ((parent.get("card") or {}).get("name", champion), child)
        write_json(folder / "card.json", card)
        gates.write_manifest(folder)
        return child

    def step_analyst(self):
        generation = self.state["gen"]["n"]
        output = "digest/gen_%d/bottlenecks.md" % generation
        status = self.ensure("analyst_g%d" % generation, "analyst", [output], self.read_shell, lambda: roles.task_book(
            self.ws / "tasks" / ("analyst_g%d" % generation), "诊断（第 %d 代）" % generation,
            "逐个负载回答：时间花在哪里，哪一部分是读调度能改变的。只诊断，不开药方。",
            [("digest/gen_%d/run_digests.md" % generation, "程序算出的数字与时间线"), ("digest/baseline.md", "基线与已知反例"),
             ("digest/code_map.md", "第五节：诊断量的定义")],
            [(output, "每个负载一节：主要损失（带数字）、在时间线哪一段、现有 TFS 为什么没解决；最后写跨负载的共同规律")],
            ["每个结论后面标注它来自哪个文件。不要提解决方案。"]))
        if status == "wait":
            return
        if status != "ok" or not (self.ws / output).exists():
            shutil.copyfile(self.ws / "digest" / ("gen_%d" % generation) / "run_digests.md", self.ws / output)
            self.d.alert("诊断分析师未能完成，用程序摘要代替", level="warn")
        self.state["gen"]["step"] = "propose"

    # ------------------------------------------------------------ propose

    def step_propose(self):
        gen = self.state["gen"]
        for lab, cand_id in zip(("A", "B"), gen["llm"]):
            cand = self.cand(cand_id)
            if cand["status"] != "drafting":
                continue
            task = "proposer_g%d_%s" % (gen["n"], lab)
            folder = "candidates/%s" % cand_id
            status = self.ensure(task, "proposer", ["candidates/%s/**" % cand_id], roles.PROPOSER_SHELL + self.read_shell,
                                 lambda task=task, lab=lab, cand_id=cand_id, folder=folder: self.proposer_book(task, lab, cand_id, folder),
                                 max_attempts=2, timeout_min=config.TIMEOUT_MIN["proposer"])
            waiting = False
            if status == "wait":
                # Both proposal groups work at the same time; come back when each has finished.
                waiting = True
                continue
            card, errors = gates.load_card(self.cand_dir(cand_id))
            if status == "ok" and card is not None:
                cand.update(status="drafted", card=card)
            else:
                self.reject(cand_id, "提案未完成：%s" % ("；".join(errors) or self.state["tasks"][task].get("error", "")))
        if not waiting:
            self.state["gen"]["step"] = "pipeline"

    def proposer_book(self, task, lab, cand_id, folder):
        generation = self.state["gen"]["n"]
        context = write_context(self.d, generation, lab)
        inputs = [("digest/code_map.md", "代码地图与冻结校验的硬约束（必读）"), (context, "本代上下文：排行榜、经验、机制地图"),
                  ("digest/gen_%d/bottlenecks.md" % generation, "瓶颈卡"), ("digest/gen_%d/run_digests.md" % generation, "逐负载数字"),
                  ("digest/baseline.md", "基线"), ("digest/champion.md", "当前冠军"),
                  ("base/dynamo_vllm_cpu_sim/sim", "基线代码（只读）")]
        champion = self.state["candidates"].get(self.state["champion"]) if self.state["champion"] else None
        if champion:
            inputs.append(("candidates/%s" % champion["id"], "现任冠军的覆盖层与卡片，可以在它之上改进"))
        outputs = [(folder + "/card.json", "候选卡（JSON，字段见下）"), (folder + "/CARD.md", "给人看的说明"),
                   (folder + "/policy.json", "要下发的配置与参数"), (folder + "/overlay/**", "替换的包内文件，路径与 base/ 里一致"),
                   (folder + "/tests/test_*.py", "unittest，其中必须有文件名含 scale 的尺度不变性测试")]
        notes = ["card.json 的字段：name、tier（整数，0 表示只用核心范围）、parent（父候选编号或 null）、mechanism"
                 "（signal/action/timing/feedback 四个列表，标签见上下文的机制地图）、params（对象，键是 policy.json 里的点路径，"
                 "值含 value/min/max）、causal、signature（列表，每项 workload/metric/direction）、failure、protection。",
                 "参数统一放在 policy.json 的 admission.params 或 routing_accounting.params 下，代码从配置读取，不要写死常数。",
                 "新增代码里不要出现 class_id、负载名、os.environ、open() 等被禁内容；确实需要的行在行尾写 `# tuning-allow: 理由`。",
                 "最多申请一次 `python tune.py smoke %s 408`，结果在 candidates/%s/smoke.json。" % (cand_id, cand_id),
                 "如果现任冠军已经不错，优先考虑在它之上叠加一个针对瓶颈卡里最大损失的机制。"]
        return roles.task_book(self.ws / "tasks" / task, "提案（第 %d 代，提案组 %s，候选 %s）" % (generation, lab, cand_id),
                               "提出并实现一个候选策略，写入 candidates/%s/。" % cand_id, inputs, outputs, notes)

    # ------------------------------------------------------------ pipeline

    def step_pipeline(self):
        again = True
        while again:
            again = False
            for cand_id in self.members():
                if self.advance(cand_id):
                    again = True
        if not self.active():
            self.state["gen"]["step"] = "meta"

    def advance(self, cand_id):
        """Move one candidate forward. True when its status changed."""
        cand = self.cand(cand_id)
        status = cand["status"]
        handler = getattr(self, "at_" + status, None)
        if not handler:
            return False
        return handler(cand_id)

    def at_drafted(self, cand_id):
        cand = self.cand(cand_id)
        task = "redteam_%s_%d" % (cand_id, cand.get("review_round", 0))
        review = "reviews/%s/review.json" % cand_id
        folder = "candidates/%s" % cand_id
        status = self.ensure(task, "redteam", ["reviews/%s/**" % cand_id], self.read_shell, lambda: roles.task_book(
            self.ws / "tasks" / task, "红队审查（候选 %s）" % cand_id,
            "先盲审代码，再核对说法。找出它不通用的地方和作弊路径，设计一个攻击负载，给出判定。",
            [(folder + "/overlay", "覆盖层（先看这个）"), (folder + "/policy.json", "配置"), (folder + "/tests", "单测"),
             ("base/dynamo_vllm_cpu_sim/sim", "基线代码"), (folder + "/card.json", "候选卡（看完代码再看）"),
             (folder + "/CARD.md", "说明"), ("digest/code_map.md", "硬约束")],
            [(review, "JSON：cheat_paths、case_specific、risks、zero_findings、verdict（pass/fix/reject）、required_fixes、"
                       "attack（name 取压力负载名：%s）、why" % "、".join(config.STRESS_SPECS))],
            ["判定 fix 时，required_fixes 必须具体到文件和位置。"]), max_attempts=2)
        if status == "wait":
            return False
        verdict = self.read_review(cand_id)
        if verdict is None:
            self.reject(cand_id, "红队审查没有给出可用的判定：%s" % self.state["tasks"][task].get("error", ""))
            return True
        cand["review"] = verdict
        if verdict["verdict"] == "reject":
            self.reject(cand_id, "红队否决：%s" % "；".join((verdict.get("case_specific") or verdict.get("cheat_paths") or ["见 review.json"]))[:500])
            return True
        if verdict["verdict"] == "fix":
            return self.send_back(cand_id, verdict.get("required_fixes") or ["按 review.json 修改"], "红队要求修改")
        if verdict.get("zero_findings"):
            opinion = self.crossval("crossval_review_%s_%d" % (cand_id, cand.get("review_round", 0)),
                                    "红队对这个候选零发现。请独立判断：它是否依赖负载身份、会不会饿死请求或死锁、在尺度变换下是否失效？",
                                    [(folder + "/overlay", "覆盖层"), (review, "红队审查")], "crossval/review_%s.json" % cand_id)
            if opinion == "wait":
                return False
            cand["crossval_review"] = opinion
            if isinstance(opinion, dict) and opinion.get("agree") is False:
                return self.send_back(cand_id, opinion.get("concerns") or [str(opinion.get("opinion", ""))[:500]], "交叉验证不同意")
        cand["status"] = "reviewed"
        return True

    def read_review(self, cand_id):
        path = self.ws / "reviews" / cand_id / "review.json"
        if not path.exists():
            return None
        try:
            review = roles.parse_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        if not isinstance(review, dict) or review.get("verdict") not in ("pass", "fix", "reject"):
            return None
        attack = review.get("attack")
        review["attack"] = attack if isinstance(attack, dict) and attack.get("name") in config.STRESS_SPECS else None
        review["zero_findings"] = bool(review.get("zero_findings"))
        return review

    def send_back(self, cand_id, issues, why):
        """One more revision round, or rejection when the rounds are used up."""
        cand = self.cand(cand_id)
        rounds = cand.get("fix_rounds", 0) + 1
        cand.update(fix_rounds=rounds, fix_issues=list(issues)[:20], fix_why=why, status="revising")
        if cand["kind"] != "llm" or rounds > 3:
            self.reject(cand_id, "%s，且无法再修改：%s" % (why, "；".join(str(i) for i in issues)[:500]))
        return True

    def at_revising(self, cand_id):
        cand = self.cand(cand_id)
        task = "revise_%s_%d" % (cand_id, cand.get("fix_rounds", 0))
        folder = "candidates/%s" % cand_id
        notes = ["%s。必须解决：" % cand.get("fix_why", "") + json.dumps(cand.get("fix_issues") or [], ensure_ascii=False)[:3000],
                 "静态门与单测的输出在 candidates/%s/gate.json 与 candidates/%s/unittest.txt。" % (cand_id, cand_id)]
        status = self.ensure(task, "proposer", ["candidates/%s/**" % cand_id], roles.PROPOSER_SHELL + self.read_shell, lambda: roles.task_book(
            self.ws / "tasks" / task, "修改候选 %s" % cand_id, "按意见修改候选，保持因果链成立。改完重新运行单测。",
            [("digest/code_map.md", "硬约束"), ("digest/gen_%d/context_%s.md" % (self.state["gen"]["n"], cand.get("lab") or "A"), "本代上下文"),
             ("candidates/%s/gate.json" % cand_id, "静态门结果（如果已有）"), ("candidates/%s/unittest.txt" % cand_id, "单测输出（如果已有）"),
             ("reviews/%s/review.json" % cand_id, "红队审查（如果已有）")],
            [(folder + "/card.json", ""), (folder + "/CARD.md", ""), (folder + "/policy.json", ""), (folder + "/overlay/**", ""), (folder + "/tests/**", "")],
            notes), max_attempts=2, timeout_min=config.TIMEOUT_MIN["revise"])
        if status == "wait":
            return False
        card, errors = gates.load_card(self.cand_dir(cand_id))
        if status == "ok" and card is not None:
            cand.update(card=card, status="reviewed" if cand["kind"] == "llm" else "ready", review_round=cand.get("review_round", 0) + 1)
        else:
            self.reject(cand_id, "修改未完成：%s" % ("；".join(errors) or self.state["tasks"][task].get("error", "")))
        return True

    def at_reviewed(self, cand_id):
        cand = self.cand(cand_id)
        cand["status"] = "ready"
        return True

    def at_ready(self, cand_id):
        """The static gate, then unit tests, then one smoke replay."""
        cand = self.cand(cand_id)
        folder = self.cand_dir(cand_id)
        result = gates.static_gate(folder, self.d.base_dir, open_tier_of(self.state))
        if not result.passed:
            cand["gate"] = result.as_dict()
            write_json(folder / "gate.json", cand["gate"])
            return self.send_back(cand_id, result.issues, "静态门未通过")
        if cand["kind"] == "llm" and not cand.get("tests_passed"):
            with self.d._wsl_lock:
                passed, detail = wsl.candidate_tests(folder, cand.get("tier", 0))
            (folder / "unittest.txt").write_text(detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False), encoding="utf-8")
            if passed != "pass":
                cand["gate"] = dict(passed=False, issues=["单测没有通过（%s）" % passed])
                return self.send_back(cand_id, ["单测没有通过，见 candidates/%s/unittest.txt" % cand_id], "单测未通过")
            cand["tests_passed"] = True
        gates.write_manifest(folder)
        cand["gate"] = result.as_dict()
        write_json(folder / "gate.json", cand["gate"])
        self.log("candidate_gated", cand=cand_id, allow_lines=result.allow_lines[:10])
        if cand["kind"] == "param":
            cand["status"] = "screen"
            self.enqueue(cand_id, "screen", config.SCREEN_RUNS)
        else:
            # The official smoke replays exactly what passed the gate. A smoke the proposer asked
            # for earlier may predate its last edits, so it does not count.
            cand["runs"].get(config.SMOKE_WORKLOAD, {}).pop("smoke", None)
            cand["status"] = "smoke"
            self.enqueue(cand_id, "smoke", 1, workloads=[config.SMOKE_WORKLOAD])
        return True

    def at_smoke(self, cand_id):
        cand = self.cand(cand_id)
        rows = (cand["runs"].get(config.SMOKE_WORKLOAD) or {}).get("smoke") or []
        if not rows:
            if cand.get("run_failed"):
                reason, cand["run_failed"] = cand["run_failed"], ""
                return self.send_back(cand_id, [reason[:1500]], "冒烟回放失败")
            return False
        result = read_json(self.ws / "runs" / rows[0]["run"] / "result.json")
        write_json(self.cand_dir(cand_id) / "smoke.json", dict(summary=result["summary"], digest=result["digest"]))
        cand["status"] = "screen"
        self.enqueue(cand_id, "screen", config.SCREEN_RUNS)
        return True

    def at_screen(self, cand_id):
        cand = self.cand(cand_id)
        if cand.get("run_failed"):
            reason, cand["run_failed"] = cand["run_failed"], ""
            return self.send_back(cand_id, [reason[:1500]], "筛选阶段回放失败")
        if not self.complete(cand_id, "screen", config.SCREEN_RUNS):
            return False
        reference, _ = reference_series(self.state)
        results = evaluator.workload_result(reference, series(cand["runs"], "screen"))
        ok, why = evaluator.screen_pass(results)
        cand.update(score=evaluator.total_score(results), screen=why)
        if not ok:
            self.reject(cand_id, "筛选未通过：%s" % why)
            return True
        cand["status"] = "confirm"
        self.enqueue(cand_id, "confirm", config.CONFIRM_RUNS)
        return True

    def at_confirm(self, cand_id):
        cand = self.cand(cand_id)
        if cand.get("run_failed"):
            reason, cand["run_failed"] = cand["run_failed"], ""
            self.reject(cand_id, "确认阶段回放失败：%s" % reason[:600])
            return True
        target = cand.get("confirm_target", config.CONFIRM_RUNS)
        if not self.complete(cand_id, "confirm", target):
            return False
        reference, _ = reference_series(self.state)
        results = evaluator.workload_result(reference, series(cand["runs"], "confirm"))
        base_results = evaluator.workload_result(baseline_series(self.state), series(cand["runs"], "confirm"))
        cand.update(results=results, score=evaluator.total_score(results))
        ok, why = evaluator.confirm_pass(base_results)
        if not ok:
            self.reject(cand_id, "确认未通过：%s" % why)
            return True
        uncertain = evaluator.needs_more_runs(results, reference, cand["runs"], target)
        if target < config.MAX_CONFIRM_RUNS and uncertain:
            cand["confirm_target"] = min(config.MAX_CONFIRM_RUNS, target + 5)
            self.enqueue(cand_id, "confirm", cand["confirm_target"], workloads=uncertain)
            return True
        cand["status"] = "promotable"
        return True

    def at_promotable(self, cand_id):
        """Audit: attack workloads, an independent recomputation, the promised signature, leave-one-out."""
        cand = self.cand(cand_id)
        if not cand.get("audit_queued"):
            names = []
            attack = (cand.get("review") or {}).get("attack")
            if attack and cand["kind"] == "llm":
                names.append(attack["name"])
            for name in (ATTACK_DEFAULT if cand["kind"] == "llm" else ATTACK_PARAM):
                if name not in names:
                    names.append(name)
            names = [name for name in names if self.d.state["stress_baseline"].get(name, {}).get("light", {}).get("ttft")]
            if not names:
                names = [name for name in config.STRESS_SPECS if self.d.state["stress_baseline"].get(name, {}).get("light", {}).get("ttft")][:2]
            self.enqueue(cand_id, "attack", 2, workloads=names, stress=True)
            cand["audit_queued"] = True
            self.launch_auditor(cand_id)
        task = "auditor_%s" % cand_id
        status = self.d.task_state(task)
        if status == "lost" and self.state["tasks"][task].get("attempts", 0) < 3:
            self.launch_auditor(cand_id)
            return False
        if cand.get("run_failed"):
            reason, cand["run_failed"] = cand["run_failed"], ""
            self.reject(cand_id, "压力负载回放失败：%s" % reason[:600])
            return True
        if not self.complete(cand_id, "attack", 2, stress=True) or status in ("running", "lost"):
            return False
        self.finish_audit(cand_id, status)
        return True

    def launch_auditor(self, cand_id):
        cand = self.cand(cand_id)
        task = "auditor_%s" % cand_id
        rows = [(w, r["run"]) for w in config.WORKLOADS for r in (cand["runs"].get(w) or {}).get("confirm") or []]
        expected = {w: dict(mean_ttft_s=statistics.mean(x["ttft"] for x in cand["runs"][w]["confirm"]),
                            makespan_s=statistics.mean(x["makespan"] for x in cand["runs"][w]["confirm"])) for w in config.WORKLOADS}
        write_json(self.ws / "audits" / cand_id / "expected.json", expected)
        inputs = [("runs/%s/raw/report.json" % run_id, "%s 的一轮原始记录（逐请求 ttft_s、e2e_s、sent_perf_s）" % w) for w, run_id in rows]
        inputs += [("runs/%s/raw/worker_of.json" % run_id, "同上：每条请求所在实例") for w, run_id in rows]
        inputs += [("audits/%s/expected.json" % cand_id, "编排器给出的数字，用来对比")]
        output = "audits/%s/audit.json" % cand_id
        self.d.start_task(task, "auditor", roles.task_book(
            self.ws / "tasks" / task, "复核审计（候选 %s）" % cand_id, "自己写代码从原始记录重算两个指标，再与编排器的数字对比。",
            inputs, [(output, "JSON：recomputed（每个负载的 mean_ttft_s、makespan_s）、method、agrees、notes"),
                     ("audits/%s/*.py" % cand_id, "你的计算脚本。写在这个目录下，用 `python audits/%s/脚本名.py` 运行" % cand_id)]),
            ["audits/%s/**" % cand_id], roles.AUDITOR_SHELL)

    def finish_audit(self, cand_id, task_status):
        cand = self.cand(cand_id)
        audit = None
        try:
            audit = roles.parse_json((self.ws / "audits" / cand_id / "audit.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not isinstance(audit, dict) or not isinstance(audit.get("recomputed"), dict):
            if task_status in ("running", "error", "timeout"):
                return
            cand["audit"] = dict(agrees=None, notes="审计没有给出可解析的结果")
        else:
            worst = 0.0
            for workload, values in audit["recomputed"].items():
                if workload not in cand["runs"] or not isinstance(values, dict):
                    continue
                rows = cand["runs"][workload]["confirm"]
                try:
                    worst = max(worst, abs(values["mean_ttft_s"] - statistics.mean(x["ttft"] for x in rows)) / statistics.mean(x["ttft"] for x in rows),
                                abs(values["makespan_s"] - statistics.mean(x["makespan"] for x in rows)) / statistics.mean(x["makespan"] for x in rows))
                except (KeyError, TypeError, ZeroDivisionError):
                    worst = 1.0
            audit["max_relative_difference"] = worst
            cand["audit"] = audit
            if worst > 0.005:
                self.d.alert("审计重算与编排器相差 %.2f%%（%s），按审计失败处理" % (100 * worst, cand_id), level="warn")
        stress = stress_results(self.state, cand)
        cand["stress"] = stress
        bad = sorted(name for name, row in stress.items() if row["regresses"])
        if bad:
            self.reject(cand_id, "压力负载上回退：%s" % "、".join(bad))
            return
        if cand["audit"].get("agrees") is False or cand["audit"].get("max_relative_difference", 0) > 0.005:
            self.reject(cand_id, "审计未通过：%s" % str(cand["audit"].get("notes", "数字不一致"))[:400])
            return
        pairs = {}
        for w in config.WORKLOADS:
            mine = [r["run"] for r in cand["runs"][w]["confirm"]]
            base = self.state["baseline"][w]["light"]
            pairs[w] = dict(cand=_mean_digest(digests_of(self.d, mine)), ref=_mean_digest(digests_of(self.d, base["runs"])),
                            cand_summary=_mean_summary(self.d, mine), ref_summary=_mean_summary(self.d, base["runs"]))
        kept, missed = gates.signature_check(cand["card"], pairs)
        cand["signature"] = dict(kept=kept, missed=missed)
        if missed:
            self.reject(cand_id, "未解释收益：承诺的数据特征没有出现（%s）" % "；".join(missed)[:400])
            return
        ok, report = leave_one_out(self.state, cand_id)
        cand["leave_one_out"] = dict(ok=ok, report=report)
        if not ok:
            self.reject(cand_id, "留一法未通过：去掉某个负载后掉出前三")
            return
        cand["status"] = "audited"

    def at_audited(self, cand_id):
        cand = self.cand(cand_id)
        task = "judge_%s_%d" % (cand_id, cand.get("judge_round", 0))
        output = "judgements/%s/judge.json" % cand_id
        reference, champion_id = reference_series(self.state)
        results = cand.get("results") or evaluator.workload_result(reference, series(cand["runs"], "confirm"))
        write_json(self.ws / "judgements" / cand_id / "results.json", dict(
            versus=champion_id or "baseline", workloads=results, signature=cand.get("signature"), stress=cand.get("stress")))
        folder = "candidates/%s" % cand_id
        status = self.ensure(task, "judge", ["judgements/%s/**" % cand_id], self.read_shell, lambda: roles.task_book(
            self.ws / "tasks" / task, "理论评审（候选 %s）" % cand_id, "盲评这个候选是否通用、因果链是否成立、证据是否充分。",
            [(folder + "/card.json", "候选卡"), (folder + "/CARD.md", "说明"), (folder + "/overlay", "代码"),
             ("judgements/%s/results.json" % cand_id, "运行结果"), ("reviews/%s/review.json" % cand_id, "红队审查（如果已有）"),
             ("digest/baseline.md", "基线")],
            [(output, "JSON：generality、causal、evidence 各 0 到 2，issues（列表）、comment")],
            ["看不到也不猜提案人是谁。每个分数都要有依据。"]), max_attempts=2)
        if status == "wait":
            return False
        try:
            verdict = roles.parse_json((self.ws / output).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            verdict = {}
        if not isinstance(verdict, dict) or status != "ok":
            verdict = {}
        cand["judge"] = verdict
        total = sum(int(verdict.get(k) or 0) for k in ("generality", "causal", "evidence"))
        if int(verdict.get("generality") or 0) < 2 or total < 4:
            cand.update(status="kept", reason="理论评审未达标：%s" % json.dumps(verdict.get("issues") or verdict, ensure_ascii=False)[:400])
            return True
        decision = evaluator.decide_promotion(results)
        cand["decision"] = decision
        if not decision["promote"]:
            cand.update(status="kept", reason="；".join(decision["reasons"]))
            return True
        if not cand.get("crossval_done"):
            opinion = self.crossval("crossval_promote_%s" % cand_id,
                                    "这个候选将取代现任冠军。请独立判断它是否通用、因果链是否成立、证据是否足以支持更替。",
                                    [(folder + "/card.json", "候选卡"), (folder + "/CARD.md", "说明"), (folder + "/overlay", "代码"),
                                     ("judgements/%s/results.json" % cand_id, "运行结果"), ("judgements/%s/judge.json" % cand_id, "理论评审")],
                                    "crossval/promote_%s.json" % cand_id)
            if opinion == "wait":
                return False
            cand.update(crossval_done=True, crossval_opinion=opinion)
            if (cand.get("confirm_target", config.CONFIRM_RUNS) < config.MAX_CONFIRM_RUNS
                    and isinstance(opinion, dict) and opinion.get("recommend_more_runs")):
                cand["confirm_target"] = min(config.MAX_CONFIRM_RUNS, cand.get("confirm_target", config.CONFIRM_RUNS) + 5)
                self.enqueue(cand_id, "confirm", cand["confirm_target"])
                cand["status"] = "confirm"
                return True
        self.promote(cand_id)
        return True

    def promote(self, cand_id):
        cand = self.cand(cand_id)
        for record in self.state["candidates"].values():
            if record.get("status") == "champion":
                record["status"] = "retired"
        cand["status"] = "champion"
        self.state["champion"] = cand_id
        self.state["champion_history"].append(dict(id=cand_id, gen=cand["gen"], score=cand.get("score"), at=time.time()))
        self.state["gen"]["promoted"] = cand_id
        self.state["stagnation"] = 0
        self.log("promoted", cand=cand_id, score=cand.get("score"))
        write_champion_digest(self.d)
        from .gitops import record_champion
        try:
            record_champion(self.d, cand_id)
        except Exception as error:  # noqa: BLE001
            self.d.alert("冠军 %s 已产生，但登记到仓库失败，收尾时重试：%r" % (cand_id, error), level="warn")

    # ------------------------------------------------------------ end of a generation

    def step_meta(self):
        from .report import scored
        gen = self.state["gen"]
        generation = gen["n"]
        report = write_generation_report(self.d)
        meta = self.state["meta"]
        output = "meta/amendments/gen_%d.json" % generation
        untested = "digest/gen_%d/untested.json" % generation
        meta_status = self.ensure("meta_g%d" % generation, "meta", ["meta/amendments/**"], self.read_shell, lambda: roles.task_book(
            self.ws / "tasks" / ("meta_g%d" % generation), "元策略复盘（第 %d 代）" % generation,
            "更新经验教训、假设库、下一代的重点提示和预算分配，让搜索本身变得更好。",
            [(report, "本代报告"), ("digest/gen_%d/context_A.md" % generation, "本代开始时的上下文与机制地图"),
             ("digest/gen_%d/bottlenecks.md" % generation, "本代的瓶颈卡")],
            [(output, "JSON：lessons_md、hypotheses（列表：family/status/priority/note）、proposer_hints、redteam_hints、"
                       "allocation（llm 1–3，param 0–4）、tool_requests（列表）")],
            ["不能改评价规则、范围清单、基线定义。", "每条经验都要引用本代的证据。"]), session=meta.get("session"))
        prover_status = self.ensure("prover_g%d" % generation, "prover", [untested], self.read_shell, lambda: roles.task_book(
            self.ws / "tasks" / ("prover_g%d" % generation), "举证（第 %d 代）" % generation,
            "找出还没试过的方向，证明「优化不动了」还不成立。",
            [(report, "本代报告"), ("digest/gen_%d/context_A.md" % generation, "机制地图与排行榜"),
             ("meta/lessons.md", "经验教训"), ("meta/hypotheses.json", "假设库")],
            [(untested, "JSON：directions（列表，每项 cell、idea、why）；确实穷尽了才交空列表")],
            ["宁可多列，也不要说「没有了」。"]))
        if meta_status == "wait" or prover_status == "wait":
            return
        if gen.get("amended") != generation:
            gen["amended"] = generation
            self.apply_amendment(generation, meta_status)
        if self.build_tools(generation):
            return
        self.update_map()
        self.update_ladder()
        best = max([row["score"] for row in scored(self.state) if row["score"] is not None] or [0])
        self.state.setdefault("history", []).append(dict(gen=generation, at=time.time(), runs=self.state["runs_total"],
                                                         promoted=gen.get("promoted"), best_score=best))
        try:
            shutil.copyfile(self.ws / "dashboard" / "index.html", self.ws / "generations" / ("gen_%d" % generation) / "dashboard.html")
        except OSError:
            pass
        try:
            from .report import Dashboard
            Dashboard(self.d).render()
            shutil.copyfile(self.ws / "dashboard" / "index.html", self.ws / "generations" / ("gen_%d" % generation) / "dashboard.html")
            from .gitops import record_generation
            record_generation(self.d, generation)
        except Exception as error:  # noqa: BLE001
            self.d.alert("第 %d 代小结没有提交到分支，下次一并推送：%r" % (generation, error), level="warn")
        self.cleanup()
        self.state["gen"] = dict(n=generation, step="open", opened=False)

    def build_tools(self, generation):
        """Section 5, the toolmaker: at most one read-only analysis tool per generation, validated before use.

        Returns True while the toolmaker is still working.
        """
        requests = [r for r in (self.state["meta"].get("tool_requests") or [])
                    if r.get("gen") == generation and not r.get("built")]
        if not requests or self.state["gen"].get("tool_built") == generation:
            return False
        name, task = "tool_g%d" % generation, "toolmaker_g%d" % generation
        status = self.ensure(task, "toolmaker", ["tools/%s/**" % name], [], lambda: roles.task_book(
            self.ws / "tasks" / task, "造一个分析工具（第 %d 代）" % generation,
            "按元策略师的请求写一个只读的分析脚本，放在 tools/%s/ 下，并确认它能跑通。" % name,
            [("digest/code_map.md", "诊断量的定义"), ("generations/gen_%d/report.md" % generation, "本代报告"),
             ("runs", "每轮回放的结果：runs/<编号>/result.json 里有 summary 和 digest")],
            [("tools/%s/run.py" % name, "不需要参数就能运行的分析脚本，60 秒内结束"),
             ("tools/%s/README.md" % name, "它回答什么问题、怎么用、输出样例、未完成的事")],
            ["请求：" + requests[0]["text"], "只读。不要修改 tools/ 之外的任何文件。"]), max_attempts=2)
        if status == "wait":
            return True
        requests[0]["built"] = True
        ok, detail = False, "没有交出 run.py"
        if (self.ws / "tools" / name / "run.py").exists():
            try:
                proc = subprocess.run([sys.executable, "tools/%s/run.py" % name], cwd=str(self.ws), capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", timeout=120)
                ok, detail = proc.returncode == 0, (proc.stdout + proc.stderr)[-800:]
            except (OSError, subprocess.TimeoutExpired) as error:
                detail = repr(error)
        self.state.setdefault("tools", {})[name] = dict(
            ok=ok, gen=generation, request=requests[0]["text"][:300], detail="" if ok else detail[-600:])
        self.state["gen"]["tool_built"] = generation
        self.log("tool_built", tool=name, ok=ok)
        if not ok:
            self.d.alert("工具匠交出的 %s 没有跑通，未启用" % name, level="warn")
        return False

    def write_meta_files(self):
        meta = self.state["meta"]
        (self.ws / "meta" / "lessons.md").write_text(meta.get("lessons_md") or "（还没有）", encoding="utf-8")
        write_json(self.ws / "meta" / "hypotheses.json", meta.get("hypotheses") or [])

    def apply_amendment(self, generation, status):
        """Section 12. Validate, snapshot, and roll back after two declining generations."""
        meta = self.state["meta"]
        record = self.state["tasks"].get("meta_g%d" % generation, {})
        if record.get("session_id"):
            meta["session"] = record["session_id"]
        amendment = None
        try:
            amendment = roles.parse_json((self.ws / "meta" / "amendments" / ("gen_%d.json" % generation)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not isinstance(amendment, dict):
            self.d.alert("元策略师没有给出可解析的修改单，本代沿用上一代的设置", level="warn")
            self.write_meta_files()
            return
        before = {key: copy.deepcopy(meta.get(key)) for key in ("lessons_md", "hypotheses", "proposer_hints", "redteam_hints", "allocation")}
        if isinstance(amendment.get("lessons_md"), str) and amendment["lessons_md"].strip():
            meta["lessons_md"] = amendment["lessons_md"][:20000]
        if isinstance(amendment.get("hypotheses"), list):
            meta["hypotheses"] = [h for h in amendment["hypotheses"] if isinstance(h, dict) and "family" in h][:80]
        for key in ("proposer_hints", "redteam_hints"):
            if isinstance(amendment.get(key), str):
                meta[key] = amendment[key][:4000]
        wanted = amendment.get("allocation") if isinstance(amendment.get("allocation"), dict) else {}
        current = before["allocation"] or dict(llm=2, param=2)
        try:
            meta["allocation"] = dict(llm=max(1, min(3, int(wanted.get("llm", current.get("llm", 2))))),
                                      param=max(0, min(4, int(wanted.get("param", current.get("param", 2))))))
        except (TypeError, ValueError):
            meta["allocation"] = current
        for request in amendment.get("tool_requests") or []:
            if isinstance(request, str):
                meta.setdefault("tool_requests", []).append(dict(gen=generation, text=request[:400]))
        meta.setdefault("snapshots", []).append(dict(gen=generation, before=before, at=time.time()))
        meta.setdefault("amendments", []).append(dict(gen=generation, at=time.time()))
        self.write_meta_files()
        history = self.state.get("history") or []
        gains = [b.get("best_score", 0) - a.get("best_score", 0) for a, b in zip(history, history[1:])]
        if len(gains) >= 2 and gains[-1] < gains[-2] and gains[-1] <= 0 and len(meta["snapshots"]) >= 2:
            restore = meta["snapshots"][-2]["before"]
            for key in ("hypotheses", "proposer_hints", "redteam_hints", "allocation"):
                meta[key] = restore[key]
            meta["snapshots"].pop()
            self.d.alert("连续两代收益下降，已回滚上一次的流程修改", level="warn")
            self.d.log("meta_rollback", generation=generation)
            self.write_meta_files()
        self.d.log("meta_amendment", generation=generation, allocation=meta["allocation"], hypotheses=len(meta["hypotheses"]))

    def update_map(self):
        for cand_id in self.members():
            mechanism = (self.cand(cand_id).get("card") or {}).get("mechanism") or {}
            for signal in mechanism.get("signal") or []:
                for action in mechanism.get("action") or []:
                    for timing in mechanism.get("timing") or []:
                        for feedback in mechanism.get("feedback") or []:
                            cell = "%s/%s/%s/%s" % (signal, action, timing, feedback)
                            if cell in config.MECHANISM_CELLS:
                                self.state["map"].setdefault(cell, [])
                                if cand_id not in self.state["map"][cell]:
                                    self.state["map"][cell].append(cand_id)

    def update_ladder(self):
        if not self.state["gen"].get("promoted"):
            self.state["stagnation"] += 1
        if self.state["stagnation"] >= config.STAGNATION_GENERATIONS and self.state["ladder"] < len(config.LADDER) - 1:
            self.state["ladder"] += 1
            self.state["stagnation"] = 0
            self.d.alert("连续 %d 代冠军未更替，停滞阶梯升到第 %d 级：%s" % (
                config.STAGNATION_GENERATIONS, self.state["ladder"], config.LADDER[self.state["ladder"]]), level="warn")
            self.d.log("ladder_up", ladder=self.state["ladder"])
        path = self.ws / "digest" / ("gen_%d" % self.state["gen"]["n"]) / "untested.json"
        untested = None
        try:
            untested = len(roles.parse_json(path.read_text(encoding="utf-8")).get("directions") or [])
        except (OSError, ValueError, AttributeError):
            pass
        open_cells = sum(1 for cell in config.MECHANISM_CELLS if not self.state["map"].get(cell))
        if untested == 0 and open_cells == 0:
            self.state["exhausted_streak"] = self.state.get("exhausted_streak", 0) + 1
        else:
            self.state["exhausted_streak"] = 0
        if self.state["exhausted_streak"] >= 3 and not (self.ws / "meta" / "exhaustion.md").exists():
            (self.ws / "meta" / "exhaustion.md").write_text(
                "# 枯竭档案\n\n举证者连续三代提不出新方向，机制地图上没有未试的格子。这只是信息，系统不会因此停止。\n\n"
                + "\n".join("| %s | %s |" % (cell, "、".join(ids)) for cell, ids in sorted(self.state["map"].items())) + "\n",
                encoding="utf-8")
            self.d.log("exhaustion_dossier")

    def cleanup(self):
        """Keep the WSL run directories of the champion and the five best candidates; delete the rest."""
        keep = set()
        ranked = sorted(self.state["candidates"].items(), key=lambda kv: -(kv[1].get("score") if kv[1].get("score") is not None else -9))
        for cand_id, record in ranked[:5] + ([(self.state["champion"], self.cand(self.state["champion"]))] if self.state.get("champion") else []):
            for tags in (record.get("runs") or {}).values():
                for rows in tags.values():
                    keep.update(row["run"] for row in rows)
        with self.d._wsl_lock:
            self.state["env"]["disk"] = wsl.cleanup_runs(keep)


def _mean_summary(daemon, run_ids):
    rows = [read_json(daemon.ws / "runs" / run_id / "result.json")["summary"] for run_id in run_ids]
    return {key: statistics.mean(row[key] for row in rows) for key in ("mean_ttft_s", "makespan_s")}


class Finalizer:
    """After the user asks to stop: a fresh interleaved confirmation, the merge, the report, exit."""

    def __init__(self, daemon):
        self.d = daemon
        self.state = daemon.state

    def step(self):
        final = self.state.setdefault("final", dict(step="confirm", queued=False, light={}, cand={}))
        champion = self.state.get("champion")
        if final["step"] == "confirm":
            if champion and not final["queued"]:
                folder = self.d.ws / "candidates" / champion
                cand = self.state["candidates"][champion]
                for index in range(config.FINAL_RUNS):
                    for workload in config.WORKLOADS:
                        self.d.new_run(workload, "light", "final", exclusive=True)
                        self.d.new_run(workload, "candidate", "final", cand=champion, overlay=folder / "overlay",
                                       policy=read_json(folder / "policy.json"), max_tier=cand.get("tier", 0),
                                       exclusive=True)
                final["queued"] = True
            if self.d.busy():
                return
            final["step"] = "merge"
        if final["step"] == "merge" and champion:
            from .gitops import merge_champion
            try:
                merge_champion(self.d, champion)
            except Exception as error:  # noqa: BLE001
                self.d.alert("合入仓库失败：%r" % error, level="error")
            final["step"] = "report"
        final["step"] = "report"
        (self.d.ws / "FINAL_REPORT.md").write_text(self.report(), encoding="utf-8")
        self.d.log("final_report_written")
        self.state["phase"] = "stopped"

    def report(self):
        lines = ["# 收尾报告", "", "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"), "",
                 "冠军：%s" % (self.state.get("champion") or "无（没有候选通过全部门禁）"), ""]
        champion = self.state.get("champion")
        if champion:
            cand = self.state["candidates"][champion]
            lines += ["机制：%s" % (cand.get("card") or {}).get("name", ""), "", "因果链：%s" % (cand.get("card") or {}).get("causal", ""), ""]
            lines += ["第二梯队用到的部分：%s" % ("、".join(config.TIER2[t]["name"] for t in sorted(config.TIER2_RUNG) if t <= int(cand.get("tier", 0))) or "无"), ""]
            rows = [[config.WORKLOADS[w]["title"]] + [
                "%.3f（%s）" % (statistics.mean(x[key] for x in cand["runs"][w]["final"]),
                                "%.1f%%" % (100 * evaluator.improvement(evaluator.stats([r[key] for r in self.d.state["baseline"][w]["light"][key_col]]),
                                                                                    evaluator.stats([x[key] for x in cand["runs"][w]["final"]]))))
                for key, key_col in (("ttft", "ttft"), ("makespan", "makespan"))] if w in cand.get("runs", {}) and cand["runs"][w].get("final")
                else [config.WORKLOADS[w]["title"], "无数据", "无数据"] for w in config.WORKLOADS]
            lines += ["| 负载 | 最终 平均TTFT s（相对默认） | 最终 完成时间 s（相对默认） |", "|---|---|---|"]
            lines += ["| %s | %s | %s |" % tuple(row) for row in rows]
            lines += ["", "用到的第二梯队：见上。压力负载结果与留一法记录在工作区 state.json 的该候选条目里。", ""]
        lines += ["共 %d 轮运行（成功 %d，失败 %d）。历史冠军：%s。" % (
            self.state["runs_total"], self.state["runs_ok"], self.state["runs_failed"],
            " → ".join("%s（第 %d 代）" % (h["id"], h["gen"]) for h in self.state.get("champion_history") or []) or "无"), ""]
        return "\n".join(lines)
