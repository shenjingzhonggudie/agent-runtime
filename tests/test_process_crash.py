"""进程级故障矩阵：真正退出进程，再由新进程恢复。

子进程用 ``os._exit`` 立即终止，不执行 finally、不关连接；恢复在 pytest 进程里完成，
它相对崩溃进程就是一个全新的进程。窗口与验收对应关系见 ``scripts/crash_runner.py``。
"""

import subprocess
import sys
from pathlib import Path
from typing import Any

from agentrun.demo import Ledger
from agentrun.engine import Engine, StopReason
from agentrun.events import EventType, make_step_key
from agentrun.journal import SqliteEventLog
from agentrun.ledger_tools import LookupRequest, Transfer
from agentrun.model import RunStatus, replay
from agentrun.policy import Finish, ScriptedPolicy, ToolCall
from agentrun.reconcile import reconcile, resolve
from agentrun.tools import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "crash_runner.py"
RUN = "crash-run"
ALICE = "alice"
BOB = "bob"


def crash(tmp_path: Path, window: str) -> None:
    """跑子进程并断言它确实以约定的退出码死掉。"""
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--db",
            str(tmp_path / "events.db"),
            "--ledger",
            str(tmp_path / "ledger"),
            "--run-id",
            RUN,
            "--crash",
            window,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 9, (
        f"{completed.returncode}: {completed.stdout} {completed.stderr}"
    )


def ledger_of(tmp_path: Path) -> Ledger:
    """重新打开账本；初始余额只在首次创建时生效。"""
    return Ledger(tmp_path / "ledger")


def log_of(tmp_path: Path) -> SqliteEventLog:
    """重新打开事件日志。"""
    return SqliteEventLog(tmp_path / "events.db")


def transfer_call() -> ToolCall:
    return ToolCall("transfer", {"src": ALICE, "dst": BOB, "amount": 250})


def test_crash_before_tool_leaves_uncertain_step_and_no_side_effect(tmp_path: Path) -> None:
    crash(tmp_path, "before_tool")

    # 意图已落盘、工具没被调用：账本没动，但日志只能证明「可能调用了」
    assert ledger_of(tmp_path).balance(ALICE) == 1000
    view = replay(log_of(tmp_path).read(RUN))
    assert view.status is RunStatus.RUNNING
    assert view.steps[0].attempts == 1
    assert view.steps[0].outcome is None

    before = log_of(tmp_path).read(RUN)
    unsure = Engine(ScriptedPolicy([Finish("不该执行")]), ToolRegistry(), log_of(tmp_path)).resume(
        RUN
    )
    assert unsure.stop_reason is StopReason.UNCERTAIN
    assert log_of(tmp_path).read(RUN) == before

    result = reconcile(log_of(tmp_path), RUN, LookupRequest(ledger_of(tmp_path)))
    assert result.requested is True
    resolve(
        log_of(tmp_path),
        RUN,
        decision="not_applied",
        note="账本查不到本次调用，进程已在调用前退出",
    )

    registry = ToolRegistry()
    registry.register(Transfer(ledger_of(tmp_path)))
    retry = Engine(
        ScriptedPolicy([Finish("占位"), transfer_call(), Finish("重做完成")]),
        registry,
        log_of(tmp_path),
    )
    done = retry.resume(RUN)

    assert done.stop_reason is StopReason.SUCCEEDED
    assert done.view.answer == "重做完成"
    assert ledger_of(tmp_path).balance(ALICE) == 750
    assert ledger_of(tmp_path).balance(BOB) == 250
    assert done.view.steps[0].step_key == make_step_key(RUN, 1)
    assert done.view.steps[1].step_key == make_step_key(RUN, 2)


def test_crash_after_tool_recovers_by_reconcile_without_second_transfer(tmp_path: Path) -> None:
    crash(tmp_path, "after_tool")

    # 副作用已经发生，但结果事件没写下来
    assert ledger_of(tmp_path).balance(ALICE) == 750
    assert ledger_of(tmp_path).balance(BOB) == 250
    assert replay(log_of(tmp_path).read(RUN)).steps[0].outcome is None

    unsure = Engine(ScriptedPolicy([Finish("不该执行")]), ToolRegistry(), log_of(tmp_path)).resume(
        RUN
    )
    assert unsure.stop_reason is StopReason.UNCERTAIN

    result = reconcile(log_of(tmp_path), RUN, LookupRequest(ledger_of(tmp_path)))
    assert result.recorded is True
    assert ledger_of(tmp_path).lookup(make_step_key(RUN, 1)) is not None

    done = Engine(
        ScriptedPolicy([Finish("占位"), Finish("对账后结束")]),
        ToolRegistry(),
        log_of(tmp_path),
    ).resume(RUN)

    assert done.stop_reason is StopReason.SUCCEEDED
    assert done.view.answer == "对账后结束"
    # 关键断言：补记之后没有第二次执行
    assert ledger_of(tmp_path).balance(ALICE) == 750
    assert ledger_of(tmp_path).balance(BOB) == 250
    starts = [e for e in log_of(tmp_path).read(RUN) if e.type is EventType.STEP_STARTED]
    assert len(starts) == 1


def test_crash_after_decided_reuses_persisted_decision(tmp_path: Path) -> None:
    crash(tmp_path, "after_decided")

    view = replay(log_of(tmp_path).read(RUN))
    assert view.steps[0].attempts == 0
    assert view.steps[0].outcome is None
    assert ledger_of(tmp_path).balance(ALICE) == 1000

    class GuardedPolicy:
        """只允许在「第 1 步已执行完」时被调用。"""

        def __init__(self) -> None:
            self.seen: list[int] = []

        def decide(self, view: Any) -> Finish:
            self.seen.append(len(view.steps))
            assert len(view.steps) == 1, "恢复时又对第 1 步做了一次决策"
            return Finish("恢复完成")

    registry = ToolRegistry()
    registry.register(Transfer(ledger_of(tmp_path)))
    policy = GuardedPolicy()
    done = Engine(policy, registry, log_of(tmp_path)).resume(RUN)

    assert done.stop_reason is StopReason.SUCCEEDED
    assert done.view.answer == "恢复完成"
    assert policy.seen == [1]
    assert ledger_of(tmp_path).balance(ALICE) == 750
    assert [step.step for step in done.view.steps] == [1]
