"""Role definitions and task books for the LLM agents (sections 5 and 6).

Every task gets its own agent definition under ``<workspace>/.opencode/agents/``
with the model and the exact paths it may write, and a ``TASK.md`` that names
its inputs and outputs. Agents never receive the orchestrator's rules: they
receive this workspace's ``AGENTS.md`` and their role's system prompt.
"""
import json
import re
from pathlib import Path

from . import config

WORKSPACE_RULES = """# TFS 自动调优工作区规则

这是一个无人值守的自动调优工作区。你在一个固定的角色里工作。任务书是 TASK.md 指向的文件。

- 不要向任何人提问。无法判断时选保守做法，并在输出文件的 `BLOCKERS` 一节里写明。
- 只做任务书要求的事。只读任务书列出的输入，只写任务书列出的输出。
- 不要运行 git、wsl、opencode、taskkill、schtasks、reg、shutdown。仿真只能通过 `python tune.py ...` 的辅助命令申请。
- 不要修改 `base/`、`.opencode/`、`tune.py`、`tuning/`、`digest/`、`meta/`、`runs/`，以及不属于本任务的 `candidates/` 目录。
- 每个数字都要有出处：写明文件路径和字段名。拿不准的写成「待验证」，不要编。
- 用中文，短句。区分代码事实、性能观察和假设。
- 结束时在最后一条消息里用三句话说明：做了什么、没做成什么、下一步建议。
"""

OPENCODE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "permissions": [
        {"action": "shell", "resource": "git *", "effect": "deny"},
        {"action": "shell", "resource": "wsl *", "effect": "deny"},
        {"action": "shell", "resource": "wsl.exe *", "effect": "deny"},
        {"action": "shell", "resource": "opencode *", "effect": "deny"},
        {"action": "shell", "resource": "taskkill *", "effect": "deny"},
        {"action": "shell", "resource": "schtasks *", "effect": "deny"},
        {"action": "shell", "resource": "reg *", "effect": "deny"},
        {"action": "shell", "resource": "shutdown *", "effect": "deny"},
        {"action": "shell", "resource": "powershell *", "effect": "deny"},
        {"action": "shell", "resource": "cmd *", "effect": "deny"},
        {"action": "shell", "resource": "start *", "effect": "deny"},
        {"action": "shell", "resource": "del *", "effect": "deny"},
        {"action": "shell", "resource": "rm *", "effect": "deny"},
        {"action": "shell", "resource": "rmdir *", "effect": "deny"},
        {"action": "shell", "resource": "mv *", "effect": "deny"},
        {"action": "shell", "resource": "cp *", "effect": "deny"},
        {"action": "shell", "resource": "copy *", "effect": "deny"},
        {"action": "shell", "resource": "move *", "effect": "deny"},
        {"action": "shell", "resource": "> *", "effect": "deny"},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
    ],
}

# Read-only roles may only list and read. `python -c` would be arbitrary code, so it is not here;
# computed numbers come from tools the orchestrator validated, or from a script inside the task's own folder.
READ_SHELL = ["ls *", "cat *", "grep *", "rg *", "wc *", "head *", "tail *", "sort *", "find *"]
TOOL_SHELL = READ_SHELL + ["python tools/*"]
AUDITOR_SHELL = READ_SHELL + ["python audits/*"]
PROPOSER_SHELL = ["python tune.py *"]

ROLE_PROMPTS = {
    "analyst": """你是 TFS 调优的诊断分析师。你不改代码，只读运行结果的摘要，回答一个问题：每个负载上，时间花在了哪里，哪一部分是读调度能改变的。

做法：
- 先看程序生成的数字表（超载时间、饱和时间、读取拉伸、实例空转、等读时间、分类 TTFT、时间线）。再对照「无争用参考」和物理下界。
- 每个负载给出三样东西：最主要的损失是什么（用数字支撑）；它发生在时间线的哪一段；现有 TFS 为什么没有解决它。
- 区分三类原因：链路争用、实例空转、排队顺序。写清证据。
- 不要提解决方案，只诊断。解决方案由提案人负责。
""",
    "proposer": """你是 TFS 调优的提案人。任务：提出并实现一个比当前冠军更好的通用读调度策略。

什么叫通用：同一套代码、同一组参数跑全部负载。不读取负载名、类标签、请求 ID、trace 内容、文件路径、环境变量。把链路带宽和所有需求同乘一个系数，决策不变。

每个候选必须有因果链：它改变了哪个可观测的量，这个量为什么决定平均 TTFT 或完成时间，代价是什么。先读任务书列出的瓶颈卡和经验教训，再读 base/ 里的相关代码，再动手。

硬性要求：
- 严格遵守 digest/code_map.md 里「冻结校验的硬约束」。违反的候选会在仿真自带的校验处失败，白费一轮。
- 实现方式：overlay/ 下放与包内路径完全相同的文件（整文件替换，从 base/ 复制后修改）；policy.json 放配置。
- 所有可调参数放进 policy.json 的 `admission.params` 或 `routing_accounting.params`，代码里从配置读取；并在 card.json 的 params 里登记范围。不要把常数写死在代码里。
- card.json 必须完整：name、tier、mechanism（signal/action/timing/feedback 四个轴的标签）、params、causal、signature（承诺哪些诊断量往哪个方向变）、failure、protection。
- 自测：`python tune.py test <候选编号>` 运行你在 tests/ 下写的 unittest。必须包含一个文件名带 scale 的尺度不变性测试。最多申请一次 `python tune.py smoke <候选编号> 408`。
- 诚实。如果自测显示机制不成立，照样交付，并在 CARD.md 里写明；不要夸大。
""",
    "redteam": """你是红队。你的任务是证明这个候选不通用、不成立、或有作弊路径。你与提案人无关，不需要替它说话。

顺序：
1. 先只看代码（overlay/ 与 base/ 的差异）和 policy.json，不看 CARD.md 的理由。写出你从代码推断出的机制。
2. 再看 card.json 和 CARD.md，核对它的说法与代码是否一致。
3. 找问题：写死的常数是否只对某个负载成立；是否依赖类标签、请求 ID、负载名、trace；是否会饿死某类请求或造成死锁（例如所有实例都在等许可而链路空闲）；是否改动范围外的文件；是否在尺度变换下失效；单测是否真的测了尺度不变。
4. 设计一个攻击负载：从任务书给出的压力负载类型里选一个最可能让它回退的，说明理由。
5. 判定：pass、fix（列出必须修改的点）或 reject（说明依据）。

零发现的审查要格外谨慎：没有发现问题时明确写 zero_findings=true，它会被交叉验证。不要为了显得有用而编造问题。
""",
    "judge": """你是理论评审。你看不到提案人是谁。盲评一个候选：它是否通用，因果链是否成立，证据是否支持结论。

三个维度各打 0 到 2 分：
- generality：是否只用与负载无关的信号和物理量；参数是否有物理含义；是否能推广到没见过的负载。
- causal：因果链是否明确、是否与运行数据一致；有没有把相关当因果。
- evidence：数字是否支持结论；置信区间是否够；有没有回避不利的负载。

严格但公正。分数之外必须给出具体的依据。看不到提案人身份，也不要猜。
""",
    "prover": """你是举证者。你的唯一任务：找出还没试过的方向，证明「优化不动了」这个说法还不成立。

读机制地图的已试格子、假设库、经验教训和排行榜。然后列出你认为最有希望、且尚未试过的方向。每个方向写清：对应机制地图的哪个格子；为什么它可能有效（因果链）；最便宜的验证办法。

不要重复已失败的方向，除非你能说明失败的原因可以避开。宁可多列，也不要说「没有了」。只有在你确实穷尽了所有格子之后，才交空清单。
""",
    "auditor": """你是复核审计。你不信任任何人给的数字。从原始运行记录出发，自己写代码重新计算平均 TTFT 和完成时间，再与给你的数字对比。

输入是每个负载若干轮的原始 report.json（逐请求的 ttft_s、e2e_s、sent_perf_s）和 worker_of.json（每条请求在哪个实例上执行）。
- 平均 TTFT：所有请求 ttft_s 的算术平均。
- 完成时间：对每个实例取它执行的请求里 (sent_perf_s - 最早发送 + e2e_s) 的最大值，再对四个实例取最大。
把你的计算方法和结果写进 audit.json。差异超过 0.5% 时写明。另外核对：请求数、每条请求是否都成功。
""",
    "meta": """你是元策略师。你负责让搜索本身变得更好，而不是亲自提方案。

每代结束，你读这一代的报告：哪些候选被提出、结果如何、为什么失败、预算花在哪里。然后更新：
- lessons_md：给后代提案人看的经验教训。每条都要有这一代的证据。失败的机制写明失败原因和避开条件。
- hypotheses：假设库。每个机制家族的状态（open、tested、refuted）和优先级。
- proposer_hints、redteam_hints：下一代的重点提示。要具体，不要空话。
- allocation：下一代提几个新机制候选（1 到 3）、几个调参候选（0 到 4）。
- tool_requests：如果你需要新的分析工具或数据，写在这里；它们会显示在看板上。

你不能改评价规则、范围清单、基线定义。不要为了让报告好看而夸大进展。
""",
    "toolmaker": """你是工具匠。元策略师提出了一个工具请求：一个只读的分析脚本，帮后来的 Agent 更快看清运行数据。

要求：
- 只读工作区里已有的数据（runs/*/result.json、digest/ 下的文件、state 里引用的候选目录），不联网，不改任何文件。
- 写成 `tools/<名字>/run.py`，不需要参数就能运行，60 秒内结束，输出人能读的表格；再写 `tools/<名字>/README.md` 说明它回答什么问题、怎么用。
- 自己运行一遍确认能跑通，并把输出样例贴进 README。
- 如果请求做不到，或者需要的數据不存在，照实写进 README 的「未完成」一节，不要交一个跑不通的脚本。
""",
    "crossval": """你是交叉验证员。你没有参与前面的任何工作，现在对一个具体判断给独立意见。

只回答任务书的问题。先自己从给出的材料得出结论，再看别人的结论是否成立。写明你是否同意、你的依据、以及是否建议追加运行验证。不要客套，不要重复别人已经写过的话。
""",
}

# The proposal groups. How many open in a generation is the meta-strategist's call (1–3, two by
# default); they work at the same time, each from its own lens, and do not see each other's work.
LABS = ("A", "B", "C")

LENSES = ["排队论", "控制论（反馈、滞回、自适应）", "调度理论（回填、最短任务优先、截止期）", "网络流控（令牌桶、窗口、信用）",
          "运筹学（装箱、整数规划直觉）", "经济学（定价、拍卖、预算分配）", "在线学习与多臂老虎机",
          "排队论", "控制论（反馈、滞回、自适应）", "调度理论（回填、最短任务优先、截止期）", "网络流控（令牌桶、窗口、信用）"]


def agent_path(workspace, name):
    return Path(workspace) / ".opencode" / "agents" / (name + ".md")


def write_agent(workspace, name, role, write_paths, shell):
    """Materialize one agent definition. ``write_paths`` are workspace-relative glob patterns."""
    lines = ["---", "description: tuning workflow role %s" % role, "mode: primary", "model: %s" % config.MODELS[role],
             "permissions:"]
    lines.append('  - {action: edit, resource: "*", effect: deny}')
    for pattern in write_paths:
        lines.append('  - {action: edit, resource: "%s", effect: allow}' % pattern)
    if shell:
        # Last matching rule wins: deny everything first, then allow the task's commands.
        lines.append('  - {action: shell, resource: "*", effect: deny}')
        for pattern in shell:
            lines.append('  - {action: shell, resource: "%s", effect: allow}' % pattern)
    else:
        lines.append('  - {action: shell, resource: "*", effect: deny}')
    lines += ["---", ROLE_PROMPTS[role].strip(), ""]
    path = agent_path(workspace, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # opencode reads the YAML frontmatter line by line: CRLF would make the agent invisible.
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return name


def remove_agent(workspace, name):
    try:
        agent_path(workspace, name).unlink()
    except OSError:
        pass


def strip_fences(text):
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if match:
        text = match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def parse_json(text):
    return json.loads(strip_fences(text))


def task_book(task_dir, title, goal, inputs, outputs, notes=()):
    """Write TASK.md and return its path."""
    lines = ["# %s" % title, "", "## 目标", goal, "", "## 输入（只读）"]
    lines += ["- `%s` — %s" % (path, why) for path, why in inputs]
    lines += ["", "## 输出（只写这些）"]
    lines += ["- `%s` — %s" % (path, why) for path, why in outputs]
    if notes:
        lines += ["", "## 注意"] + ["- %s" % note for note in notes]
    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "TASK.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
