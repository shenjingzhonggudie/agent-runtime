"""模型策略的三件事：提示只由视图决定、输出严格解析、决策失败零副作用。

用一个不联网的假模型演示（按提示里的进度返回固定 JSON），不需要任何模型服务：

1. 正常链路：模型输出 → ``ToolCall`` / ``Finish`` → 引擎照常记账。
2. 模型说人话：解析失败 → ``PolicyError`` → ``RUN_FAILED``，账本一分未动。
3. 进程消失后重启：换一个新模型实例，同一步的提示逐字节相同，续跑接着走。

只用于观察，不参与引擎实现。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\model_policy_demo.py
"""

import hashlib
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from agentrun.demo import Ledger
from agentrun.engine import Engine
from agentrun.journal import MemoryEventLog, SqliteEventLog
from agentrun.ledger_tools import GetBalance, Transfer
from agentrun.llm import ModelPolicy, ToolSpec
from agentrun.model import RunView
from agentrun.policy import Action, Policy
from agentrun.tools import ToolRegistry

ALICE = "alice"
BOB = "bob"
AMOUNT = 250
RUN_ID = "model-run"
MAX_STEPS = 6

FINISH = "alice 已转给 bob 250"

SCRIPT = [
    '{"tool": "get_balance", "args": {"owner": "alice"}}',
    '{"tool": "transfer", "args": {"src": "alice", "dst": "bob", "amount": 250}}',
    f'{{"finish": "{FINISH}"}}',
]

PROGRESS = re.compile(r"已决策 (\d+) 步")


class PromptDrivenModel:
    """按提示里的进度取脚本，模拟 temperature=0 的模型：同一提示必得同一输出。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        match = PROGRESS.search(prompt)
        done = int(match.group(1)) if match else 0
        return SCRIPT[min(done, len(SCRIPT) - 1)]


class ProseModel:
    """只会说人话的模型：看起来合理，但不符合输出约定。"""

    def complete(self, prompt: str) -> str:
        return "把 250 从 alice 转到 bob 就可以了"


class CrashAfter:
    """在策略给出动作之后、引擎记账之前模拟进程消失。

    Notes:
        动作没有返回给引擎，所以 ``STEP_DECIDED`` 不会写入；日志停在“已完成 N 步”。
        这是最安全的崩溃点：决策不算数，外部世界也没有被动过。
    """

    def __init__(self, inner: Policy, steps: int) -> None:
        self._inner = inner
        self._steps = steps

    def decide(self, view: RunView) -> Action:
        action = self._inner.decide(view)
        if len(view.steps) >= self._steps:
            raise SystemExit("模拟断电")
        return action


def section(title: str) -> None:
    """打印一节的分隔标题。"""
    print(f"\n=== {title} ===")


def tool_specs() -> list[ToolSpec]:
    """提示里对模型的工具说明，必须与注册表一致。"""
    return [
        ToolSpec("get_balance", "读取账户余额，只读"),
        ToolSpec("transfer", "从一个账户转到另一个；不可重复调用"),
    ]


def build_registry(ledger: Ledger) -> ToolRegistry:
    """只注册演示用到的两个工具。"""
    registry = ToolRegistry()
    for tool in (GetBalance(ledger), Transfer(ledger)):
        registry.register(tool)
    return registry


def digest(prompt: str) -> str:
    """提示的短指纹，用来比较两次重启之间是否渲染出同一份提示。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def balances(ledger: Ledger) -> str:
    return f"{ALICE}={ledger.balance(ALICE)}  {BOB}={ledger.balance(BOB)}"


def scenario_happy_path(root: Path) -> bool:
    """模型给出三段合法输出，引擎照常执行。"""
    section("场景 A：模型输出合法，引擎照常记账")
    ledger = Ledger(root, initial={ALICE: 1000, BOB: 0})
    log = MemoryEventLog()
    model = PromptDrivenModel()
    result = Engine(ModelPolicy(model, tool_specs()), build_registry(ledger), log).run(
        RUN_ID, f"从 {ALICE} 转 {AMOUNT} 给 {BOB}", MAX_STEPS
    )

    print("第一份提示（模型能看到的全部信息）：")
    for line in model.prompts[0].splitlines():
        print(f"  {line}")
    print(f"\n模型调用次数：{len(model.prompts)}")
    print(f"事件：{[event.type.value for event in log.read(RUN_ID)]}")
    print(f"停止原因：{result.stop_reason}  答案：{result.view.answer}")
    print(f"余额：{balances(ledger)}")
    return result.view.answer == FINISH and ledger.balance(ALICE) == 750


def scenario_prose_output(root: Path) -> bool:
    """模型说了句人话，不是动作：决策失败，工具一次都没调。"""
    section("场景 B：模型输出不是动作，记 RUN_FAILED")
    ledger = Ledger(root, initial={ALICE: 1000, BOB: 0})
    log = MemoryEventLog()
    result = Engine(ModelPolicy(ProseModel(), tool_specs()), build_registry(ledger), log).run(
        "prose-run", "转账 250", MAX_STEPS
    )

    events = log.read("prose-run")
    print(f"事件：{[event.type.value for event in events]}")
    print(f"失败原因：{events[-1].payload['reason']}")
    print(f"停止原因：{result.stop_reason}  任务状态：{result.view.status}")
    print(f"已决策步数：{len(result.view.steps)}（对照：正常链路是 3）")
    print(f"余额：{balances(ledger)}（一分未动）")
    return result.stop_reason.value == "FAILED" and len(result.view.steps) == 0


def scenario_resume(root: Path) -> bool:
    """跑完一步后“断电”，重启后换一个新模型实例继续。"""
    section("场景 C：重启后换新的模型实例，提示不变、决策不变")
    db = root / "events.db"
    ledger_root = root / "ledger"

    ledger = Ledger(ledger_root, initial={ALICE: 1000, BOB: 0})
    first_model = PromptDrivenModel()
    crashed = False
    try:
        Engine(
            CrashAfter(ModelPolicy(first_model, tool_specs()), 1),
            build_registry(ledger),
            SqliteEventLog(db),
        ).run(RUN_ID, f"从 {ALICE} 转 {AMOUNT} 给 {BOB}", MAX_STEPS)
    except SystemExit:
        crashed = True

    print(f"进程 1 是否断在第 1 步之后：{crashed}")
    print(f"日志停在：{[e.type.value for e in SqliteEventLog(db).read(RUN_ID)]}")
    print(f"余额：{balances(ledger)}")

    # 「重启」：新的日志实例、新的账本实例、新的模型实例，只共享磁盘
    second_model = PromptDrivenModel()
    result = Engine(
        ModelPolicy(second_model, tool_specs()),
        build_registry(Ledger(ledger_root)),
        SqliteEventLog(db),
    ).resume(RUN_ID)

    before = digest(first_model.prompts[1])
    after = digest(second_model.prompts[0])
    print(f"崩前最后一次决策的提示指纹：{before}")
    print(f"重启后第一次决策的提示指纹：{after}")
    print(f"同一份提示：{before == after}")
    print(f"停止原因：{result.stop_reason}  答案：{result.view.answer}")
    print(f"最终余额：{balances(Ledger(ledger_root))}")
    return before == after and result.view.answer == FINISH


def main() -> None:
    """依次演示正常链路、解析失败与重启续跑。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        ok = all(
            [
                scenario_happy_path(root / "a"),
                scenario_prose_output(root / "b"),
                scenario_resume(root / "c"),
            ]
        )
        print(f"\n结论：{'三条链路都符合契约。' if ok else '不符合预期。'}")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
