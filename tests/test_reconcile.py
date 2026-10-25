"""对账契约测试：查到才补记，查不到保持未知，补记后能续跑。"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agentrun.demo import Ledger
from agentrun.engine import Engine, StopReason
from agentrun.events import EventType, make_step_key
from agentrun.journal import MemoryEventLog
from agentrun.ledger_tools import LookupRequest
from agentrun.model import Outcome, RunStatus, replay
from agentrun.policy import Finish, ScriptedPolicy
from agentrun.reconcile import NothingToReconcile, pending_step, reconcile
from agentrun.tools import SideEffect, ToolContext, ToolRegistry

RUN = "run-1"


def seed_pending(log: MemoryEventLog) -> None:
    """造一个「已决策、已开始、没有结果」的现场。"""
    log.append(RUN, EventType.RUN_STARTED, {"task": "转账", "max_steps": 5})
    log.append(
        RUN,
        EventType.STEP_DECIDED,
        {
            "step": 1,
            "tool": "transfer",
            "args": {"src": "alice", "dst": "bob", "amount": 250},
            "step_key": make_step_key(RUN, 1),
        },
    )
    log.append(RUN, EventType.STEP_STARTED, {"step": 1, "attempt": 1})


def seed_completed(log: MemoryEventLog) -> None:
    """造一个已经完整走完一步的现场。"""
    log.append(RUN, EventType.RUN_STARTED, {"task": "转账", "max_steps": 5})
    log.append(
        RUN,
        EventType.STEP_DECIDED,
        {
            "step": 1,
            "tool": "transfer",
            "args": {"src": "alice", "dst": "bob", "amount": 250},
            "step_key": make_step_key(RUN, 1),
        },
    )
    log.append(RUN, EventType.STEP_STARTED, {"step": 1, "attempt": 1})
    log.append(RUN, EventType.STEP_COMPLETED, {"step": 1, "result": {"ok": True}})


def make_ledger(tmp_path: Path) -> Ledger:
    """建一个 alice=1000 的账本。"""
    return Ledger(tmp_path / "ledger", initial={"alice": 1000, "bob": 0})


def test_pending_step_finds_unfinished_step() -> None:
    log = MemoryEventLog()
    seed_pending(log)
    assert pending_step(replay(log.read(RUN))) == 1


def test_pending_step_is_none_when_everything_finished() -> None:
    log = MemoryEventLog()
    seed_completed(log)
    assert pending_step(replay(log.read(RUN))) is None


def test_reconcile_with_empty_log_raises(tmp_path: Path) -> None:
    log = MemoryEventLog()
    with pytest.raises(NothingToReconcile):
        reconcile(log, RUN, LookupRequest(make_ledger(tmp_path)))


def test_reconcile_without_pending_step_raises(tmp_path: Path) -> None:
    log = MemoryEventLog()
    seed_completed(log)
    with pytest.raises(NothingToReconcile):
        reconcile(log, RUN, LookupRequest(make_ledger(tmp_path)))


def test_reconcile_records_completion_when_ledger_has_the_record(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.transfer("alice", "bob", 250, request_id=make_step_key(RUN, 1))
    log = MemoryEventLog()
    seed_pending(log)

    result = reconcile(log, RUN, LookupRequest(ledger))

    assert result.recorded is True
    assert result.event is not None
    view = replay(log.read(RUN))
    assert view.steps[0].outcome is Outcome.SUCCEEDED
    assert view.steps[0].result is not None
    assert view.steps[0].result["found"] is True


def test_reconcile_missing_record_requests_review_without_guessing(tmp_path: Path) -> None:
    # 查不到不等于没生效：不补记成功，也不补记失败，只把决定权交给人
    log = MemoryEventLog()
    seed_pending(log)

    result = reconcile(log, RUN, LookupRequest(make_ledger(tmp_path)))

    view = replay(log.read(RUN))
    assert result.recorded is False
    assert result.requested is True
    assert "无法确认" in result.detail
    assert view.status is RunStatus.WAITING_REVIEW
    assert view.steps[0].outcome is None


def test_resume_continues_after_reconcile(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.transfer("alice", "bob", 250, request_id=make_step_key(RUN, 1))
    log = MemoryEventLog()
    seed_pending(log)

    reconcile(log, RUN, LookupRequest(ledger))

    # 第 1 步已经补记完成，脚本第 0 项对应当初那一步，续跑从第 1 项开始
    registry = ToolRegistry()
    engine = Engine(ScriptedPolicy([Finish("占位"), Finish("对账后结束")]), registry, log)
    result = engine.resume(RUN)

    assert result.stop_reason is StopReason.SUCCEEDED
    assert result.view.answer == "对账后结束"
    assert ledger.balance("alice") == 750


def test_reconcile_reports_query_failure_without_writing(tmp_path: Path) -> None:
    class BrokenLookup:
        name = "lookup_request"
        side_effect = SideEffect.READ_ONLY

        def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
            return None

        def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
            raise RuntimeError("账本文件损坏")

    log = MemoryEventLog()
    seed_pending(log)
    before = log.read(RUN)

    result = reconcile(log, RUN, BrokenLookup())

    assert result.recorded is False
    assert "查询失败" in result.detail
    assert log.read(RUN) == before
