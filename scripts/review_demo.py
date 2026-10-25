"""崩溃 → 自动对账查不到 → 人工复核 → 续跑（或终止）的完整链路。

两个现场，都不靠猜：

1. **确认未生效**：进程死在「转账可能已发出」的窗口里，自动对账查不到。人工核对后
   给出 ``not_applied``，任务回到 ``RUNNING``；续跑把转账当作**新的一步**重做，最终
   只发生一次副作用。
2. **证据不足**：同样的现场，人工决定 ``abort``，任务进入终态 ``ABORTED``，账本一分
   未动，``resume`` 也不会再跑。

只用于观察，不参与引擎实现。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\review_demo.py
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from agentrun.demo import Ledger
from agentrun.engine import Engine
from agentrun.events import EventType, make_step_key
from agentrun.journal import SqliteEventLog
from agentrun.ledger_tools import LookupRequest, Transfer
from agentrun.model import replay
from agentrun.policy import Finish, ScriptedPolicy, ToolCall
from agentrun.reconcile import NothingToReconcile, reconcile, resolve
from agentrun.tools import ToolRegistry

ALICE = "alice"
BOB = "bob"
AMOUNT = 250
MAX_STEPS = 6


def section(title: str) -> None:
    """打印一节的分隔标题。"""
    print(f"\n=== {title} ===")


def balances(ledger: Ledger) -> str:
    return f"{ALICE}={ledger.balance(ALICE)}  {BOB}={ledger.balance(BOB)}"


def record_crash(log: SqliteEventLog, run_id: str) -> None:
    """写下「已决策、已开始、没有结果」三条事件，模拟进程消失。"""
    log.append(run_id, EventType.RUN_STARTED, {"task": f"转 {AMOUNT}", "max_steps": MAX_STEPS})
    log.append(
        run_id,
        EventType.STEP_DECIDED,
        {
            "step": 1,
            "tool": "transfer",
            "args": {"src": ALICE, "dst": BOB, "amount": AMOUNT},
            "step_key": make_step_key(run_id, 1),
        },
    )
    log.append(run_id, EventType.STEP_STARTED, {"step": 1, "attempt": 1})


def retry_policy() -> ScriptedPolicy:
    """第 1 步已被复核为未生效，脚本从第 2 项开始真正生效。"""
    return ScriptedPolicy(
        [
            Finish("占位：第 1 步不会由这里执行"),
            ToolCall("transfer", {"src": ALICE, "dst": BOB, "amount": AMOUNT}),
            Finish("复核后重做完成"),
        ]
    )


def registry_for(ledger: Ledger) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Transfer(ledger))
    return registry


def scenario_not_applied(root: Path) -> bool:
    """人工确认未生效 → 续跑重做 → 只生效一次。"""
    section("场景 A：自动对账查不到，人工确认未生效，然后重做")
    run_id = "review-retry"
    ledger = Ledger(root / "a", initial={ALICE: 1000, BOB: 0})
    log = SqliteEventLog(root / "a" / "events.db")
    engine = Engine(retry_policy(), registry_for(ledger), log)

    record_crash(log, run_id)
    first = engine.resume(run_id)
    print(f"重启后 resume：{first.stop_reason}，余额 {balances(ledger)}（什么都没执行）")

    try:
        result = reconcile(log, run_id, LookupRequest(ledger))
    except NothingToReconcile as exc:
        print(f"无需对账：{exc}")
        return False
    print(f"自动对账：{result.detail}")
    print(f"任务状态：{replay(log.read(run_id)).status}（等人给结论）")

    view = resolve(
        log,
        run_id,
        decision="not_applied",
        note="人工核对：账本没有 request_id = review-retry:1 的记录，进程已退出",
    )
    print(f"复核结论：{view.review.decision if view.review else None} → 状态 {view.status}")

    second = engine.resume(run_id)
    for event in log.read(run_id):
        print(f"  {event.seq:>2} {event.type.value}")
    print(f"续跑结果：{second.stop_reason}  答案：{second.view.answer}")
    print(f"最终余额：{balances(ledger)}")
    return (
        second.view.answer == "复核后重做完成"
        and ledger.balance(ALICE) == 750
        and ledger.balance(BOB) == 250
    )


def scenario_abort(root: Path) -> bool:
    """证据不足 → 人工终止 → 不再执行。"""
    section("场景 B：证据不足，人工终止")
    run_id = "review-abort"
    ledger = Ledger(root / "b", initial={ALICE: 1000, BOB: 0})
    log = SqliteEventLog(root / "b" / "events.db")
    engine = Engine(retry_policy(), registry_for(ledger), log)

    record_crash(log, run_id)
    engine.resume(run_id)
    reconcile(log, run_id, LookupRequest(ledger))
    view = resolve(log, run_id, decision="abort", note="无法确认，业务放弃本次转账")
    before = log.read(run_id)
    after = engine.resume(run_id)

    print(f"复核结论：{view.review.decision if view.review else None} → 状态 {view.status}")
    print(f"再次 resume：{after.stop_reason}，未追加事件 {log.read(run_id) == before}")
    print(f"余额：{balances(ledger)}（一分未动）")
    return after.stop_reason.value == "ABORTED" and ledger.balance(ALICE) == 1000


def main() -> None:
    """依次演示两种复核结论。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        ok = all([scenario_not_applied(root), scenario_abort(root)])
        print(f"\n结论：{'复核链路符合契约，重做只发生一次。' if ok else '不符合预期。'}")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
