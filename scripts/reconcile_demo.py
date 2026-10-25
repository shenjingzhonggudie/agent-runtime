"""崩溃、对账、续跑的完整链路。

场景：进程在「转账已经发出去、结果还没写进日志」的瞬间消失。重启后：

1. ``resume`` 看到最后一步没有结果，返回 ``UNCERTAIN``，一步也不执行。
2. ``reconcile`` 拿 ``step_key`` 去账本查，查到记录 → 补记 ``STEP_COMPLETED``。
3. 再次 ``resume``，从第 2 步接着跑，直到结束。

只用于观察，不参与引擎实现。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\reconcile_demo.py
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from agentrun.demo import Ledger
from agentrun.engine import Engine
from agentrun.events import EventType
from agentrun.journal import SqliteEventLog
from agentrun.ledger_tools import GetBalance, LookupRequest, SaveReceipt, Transfer
from agentrun.model import replay
from agentrun.policy import Finish, ScriptedPolicy, ToolCall
from agentrun.reconcile import NothingToReconcile, pending_step, reconcile
from agentrun.tools import ToolRegistry

ALICE = "alice"
BOB = "bob"
AMOUNT = 250
RUN_ID = "crash-run"
MAX_STEPS = 10


def section(title: str) -> None:
    """打印一节的分隔标题。"""
    print(f"\n=== {title} ===")


def build_registry(ledger: Ledger) -> ToolRegistry:
    """注册四个账本工具，共用同一个账本实例。"""
    registry = ToolRegistry()
    for tool in (GetBalance(ledger), Transfer(ledger), SaveReceipt(ledger), LookupRequest(ledger)):
        registry.register(tool)
    return registry


def record_crash(log: SqliteEventLog) -> None:
    """写下「已决策、已开始、没有结果」的三条事件，模拟进程消失。"""
    log.append(RUN_ID, EventType.RUN_STARTED, {"task": "转账 250", "max_steps": MAX_STEPS})
    log.append(
        RUN_ID,
        EventType.STEP_DECIDED,
        {
            "step": 1,
            "tool": "transfer",
            "args": {"src": ALICE, "dst": BOB, "amount": AMOUNT},
            "step_key": f"{RUN_ID}:1",
        },
    )
    log.append(RUN_ID, EventType.STEP_STARTED, {"step": 1, "attempt": 1})


def build_policy() -> ScriptedPolicy:
    """第 1 步已被补记覆盖，脚本从第 2 项开始才真正生效。"""
    return ScriptedPolicy(
        [
            ToolCall("transfer", {"src": ALICE, "dst": BOB, "amount": AMOUNT}),
            ToolCall("save_receipt", {"text": f"{ALICE} 向 {BOB} 转 {AMOUNT}"}),
            Finish("对账完成后结束"),
        ]
    )


def main() -> None:
    """依次演示崩溃现场、对账补记与续跑。"""
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "events.db"
        ledger = Ledger(Path(tmp) / "ledger", initial={ALICE: 1000, BOB: 0})
        registry = build_registry(ledger)
        log = SqliteEventLog(db)

        section("第 0 步：转账其实已经生效，但结果没写进日志")
        ledger.transfer(ALICE, BOB, AMOUNT, request_id=f"{RUN_ID}:1")
        record_crash(log)
        print(f"账本余额：{ALICE}={ledger.balance(ALICE)}  {BOB}={ledger.balance(BOB)}")
        print(f"日志事件：{[e.type.value for e in log.read(RUN_ID)]}")
        view = replay(log.read(RUN_ID))
        print(f"待对账步骤：{pending_step(view)}  该步 outcome={view.steps[0].outcome}")

        section("第 1 次 resume：状态未知，什么都不做")
        engine = Engine(build_policy(), registry, log)
        first = engine.resume(RUN_ID)
        print(f"停止原因：{first.stop_reason}")
        print(f"任务状态：{first.view.status}")
        print(f"余额未变：{ALICE}={ledger.balance(ALICE)}  {BOB}={ledger.balance(BOB)}")

        section("对账：用 step_key 查账本")
        try:
            result = reconcile(log, RUN_ID, registry.get("lookup_request"))
        except NothingToReconcile as exc:
            print(f"无需对账：{exc}")
            raise SystemExit(1) from exc
        print(result.detail)

        section("第 2 次 resume：接着跑第 2 步")
        second = engine.resume(RUN_ID)
        for event in log.read(RUN_ID):
            print(f"  {event.seq:>2} {event.type.value}")
        print(f"停止原因：{second.stop_reason}")
        print(f"任务状态：{second.view.status}  答案：{second.view.answer}")
        print(f"最终余额：{ALICE}={ledger.balance(ALICE)}  {BOB}={ledger.balance(BOB)}")

        section("结论")
        ok = (
            first.stop_reason.value == "UNCERTAIN"
            and result.recorded
            and second.view.answer == "对账完成后结束"
            and ledger.balance(ALICE) == 1000 - AMOUNT
            and ledger.balance(BOB) == AMOUNT
        )
        print("转账只发生一次，日志补记后链条完整。" if ok else "不符合预期。")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
