"""人工复核契约：暂停态、三种结论、重复复核、续跑闸门。"""

from pathlib import Path
from typing import Any

import pytest

from agentrun.demo import Ledger
from agentrun.engine import Engine, StopReason
from agentrun.events import Event, EventType, make_step_key
from agentrun.journal import MemoryEventLog
from agentrun.ledger_tools import Transfer
from agentrun.model import InvalidEvent, Outcome, RunStatus, apply, replay
from agentrun.policy import Finish, ScriptedPolicy, ToolCall
from agentrun.reconcile import NothingToReconcile, NothingToResolve, reconcile, resolve
from agentrun.tools import ToolRegistry

RUN = "run-1"
AT = "2026-10-25T00:00:00Z"
ALICE = "alice"
BOB = "bob"


def ev(seq: int, type_: EventType, **payload: Any) -> Event:
    return Event(run_id=RUN, seq=seq, type=type_, payload=payload, at=AT)


def seeded_log() -> MemoryEventLog:
    """造一个「第 1 步已开始、没有结果」的日志。"""
    log = MemoryEventLog()
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
    return log


def request_review(log: MemoryEventLog, reason: str = "自动对账查不到") -> None:
    log.append(RUN, EventType.REVIEW_REQUESTED, {"step": 1, "reason": reason})


def view_of(log: MemoryEventLog) -> Any:
    return replay(log.read(RUN))


def apply_next(log: MemoryEventLog, type_: EventType, **payload: Any) -> Any:
    """把一条事件按当前 seq 折叠进视图；非法时由 ``apply`` 抛 InvalidEvent。"""
    view = view_of(log)
    return apply(view, ev(view.last_seq + 1, type_, **payload))


# --- 请求复核 ------------------------------------------------------------


def test_request_enters_waiting_review_and_records_reason() -> None:
    log = seeded_log()
    request_review(log, "账本查不到 run-1:1")

    view = view_of(log)

    assert view.status is RunStatus.WAITING_REVIEW
    assert view.review is not None
    assert view.review.step == 1
    assert view.review.reason == "账本查不到 run-1:1"
    assert view.review.decision is None


def test_request_requires_a_pending_step() -> None:
    log = seeded_log()
    log.append(RUN, EventType.STEP_COMPLETED, {"step": 1, "result": {"ok": True}})

    with pytest.raises(InvalidEvent):
        apply_next(log, EventType.REVIEW_REQUESTED, step=1, reason="没有待定步骤")


def test_request_rejects_blank_reason() -> None:
    log = seeded_log()

    with pytest.raises(InvalidEvent):
        apply_next(log, EventType.REVIEW_REQUESTED, step=1, reason="   ")


def test_waiting_review_rejects_any_other_event() -> None:
    log = seeded_log()
    request_review(log)

    with pytest.raises(InvalidEvent):
        apply_next(log, EventType.STEP_STARTED, step=1, attempt=2)
    assert view_of(log).status is RunStatus.WAITING_REVIEW


# --- 三种结论 ------------------------------------------------------------


def test_applied_requires_result() -> None:
    log = seeded_log()
    request_review(log)

    with pytest.raises(InvalidEvent):
        apply_next(log, EventType.REVIEW_RESOLVED, step=1, decision="applied", note="账本里有记录")


def test_applied_records_result_and_returns_to_running() -> None:
    log = seeded_log()
    request_review(log)

    log.append(
        RUN,
        EventType.REVIEW_RESOLVED,
        {
            "step": 1,
            "decision": "applied",
            "note": "账本 record=transfer",
            "result": {"found": True, "kind": "transfer"},
        },
    )

    view = view_of(log)
    assert view.status is RunStatus.RUNNING
    assert view.steps[0].outcome is Outcome.SUCCEEDED
    assert view.steps[0].result == {"found": True, "kind": "transfer"}
    assert view.review is not None
    assert view.review.decision == "applied"


def test_not_applied_marks_step_failed_and_rejects_result() -> None:
    log = seeded_log()
    request_review(log)

    with pytest.raises(InvalidEvent):
        apply_next(
            log,
            EventType.REVIEW_RESOLVED,
            step=1,
            decision="not_applied",
            note="人工核对",
            result={},
        )

    log.append(
        RUN,
        EventType.REVIEW_RESOLVED,
        {"step": 1, "decision": "not_applied", "note": "人工核对流水，无此笔"},
    )

    view = view_of(log)
    assert view.status is RunStatus.RUNNING
    assert view.steps[0].outcome is Outcome.FAILED
    assert view.steps[0].error == {
        "kind": "reviewed_not_applied",
        "message": "人工核对流水，无此笔",
    }


def test_abort_is_terminal_and_rejects_later_events() -> None:
    log = seeded_log()
    request_review(log)

    log.append(
        RUN,
        EventType.REVIEW_RESOLVED,
        {"step": 1, "decision": "abort", "note": "证据不足，业务放弃"},
    )

    view = view_of(log)
    assert view.status is RunStatus.ABORTED
    assert view.steps[0].outcome is None
    with pytest.raises(InvalidEvent):
        apply_next(log, EventType.RUN_FAILED, reason="晚了一步")


def test_resolution_must_match_step_and_decision_and_note() -> None:
    log = seeded_log()
    request_review(log)

    with pytest.raises(InvalidEvent):
        apply_next(log, EventType.REVIEW_RESOLVED, step=2, decision="abort", note="写错步骤")
    with pytest.raises(InvalidEvent):
        apply_next(log, EventType.REVIEW_RESOLVED, step=1, decision="maybe", note="没有这种结论")
    with pytest.raises(InvalidEvent):
        apply_next(log, EventType.REVIEW_RESOLVED, step=1, decision="abort", note="   ")
    assert view_of(log).status is RunStatus.WAITING_REVIEW


def test_resolution_rejected_when_not_waiting() -> None:
    log = seeded_log()

    with pytest.raises(InvalidEvent):
        apply_next(log, EventType.REVIEW_RESOLVED, step=1, decision="abort", note="没有请求过复核")


# --- reconcile 与 resolve ------------------------------------------------


def test_reconcile_miss_requests_review(tmp_path: Path) -> None:
    log = seeded_log()
    ledger = Ledger(tmp_path / "ledger", initial={"alice": 1000, "bob": 0})

    from agentrun.ledger_tools import LookupRequest

    result = reconcile(log, RUN, LookupRequest(ledger))

    assert result.recorded is False
    assert result.requested is True
    assert result.event is not None
    assert result.event.type is EventType.REVIEW_REQUESTED
    assert "已提交人工复核" in result.detail
    assert view_of(log).status is RunStatus.WAITING_REVIEW


def test_reconcile_refuses_while_waiting(tmp_path: Path) -> None:
    log = seeded_log()
    request_review(log)
    ledger = Ledger(tmp_path / "ledger", initial={"alice": 1000, "bob": 0})

    from agentrun.ledger_tools import LookupRequest

    with pytest.raises(NothingToReconcile):
        reconcile(log, RUN, LookupRequest(ledger))


def test_resolve_applied_writes_completion(tmp_path: Path) -> None:
    log = seeded_log()
    request_review(log)

    view = resolve(
        log,
        RUN,
        decision="applied",
        note="账本记录与 step_key 一致",
        result={"found": True, "amount": 250},
    )

    assert view.status is RunStatus.RUNNING
    assert view.steps[0].outcome is Outcome.SUCCEEDED
    assert view.steps[0].result == {"found": True, "amount": 250}
    assert view.review is not None
    assert view.review.decision == "applied"


def test_resolve_twice_is_rejected() -> None:
    log = seeded_log()
    request_review(log)
    resolve(log, RUN, decision="abort", note="第一次结论")

    with pytest.raises(NothingToResolve):
        resolve(log, RUN, decision="abort", note="第二次结论")


def test_resolve_without_review_is_rejected() -> None:
    log = seeded_log()

    with pytest.raises(NothingToResolve):
        resolve(log, RUN, decision="applied", note="无复核", result={"found": True})


def test_resolve_validates_arguments() -> None:
    log = seeded_log()
    request_review(log)

    with pytest.raises(ValueError):
        resolve(log, RUN, decision="abort", note="   ")
    with pytest.raises(ValueError):
        resolve(log, RUN, decision="unknown", note="拼错结论")
    with pytest.raises(ValueError):
        resolve(log, RUN, decision="applied", note="缺 result")
    with pytest.raises(ValueError):
        resolve(log, RUN, decision="abort", note="多给 result", result={"found": True})
    assert view_of(log).status is RunStatus.WAITING_REVIEW


# --- 引擎闸门 ------------------------------------------------------------


def test_resume_while_waiting_appends_nothing() -> None:
    log = seeded_log()
    request_review(log)
    before = log.read(RUN)
    engine = Engine(ScriptedPolicy([Finish("不该执行")]), ToolRegistry(), log)

    result = engine.resume(RUN)

    assert result.stop_reason is StopReason.WAITING_REVIEW
    assert result.view.status is RunStatus.WAITING_REVIEW
    assert log.read(RUN) == before


def test_aborted_run_resume_returns_aborted() -> None:
    log = seeded_log()
    request_review(log)
    resolve(log, RUN, decision="abort", note="业务放弃")
    before = log.read(RUN)
    engine = Engine(ScriptedPolicy([Finish("不该执行")]), ToolRegistry(), log)

    result = engine.resume(RUN)

    assert result.stop_reason is StopReason.ABORTED
    assert result.view.status is RunStatus.ABORTED
    assert log.read(RUN) == before


def test_not_applied_then_resume_retries_as_new_step(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger", initial={"alice": 1000, "bob": 0})
    log = seeded_log()
    request_review(log)
    resolve(log, RUN, decision="not_applied", note="人工核对：账本无此笔")

    registry = ToolRegistry()
    registry.register(Transfer(ledger))
    policy = ScriptedPolicy(
        [
            Finish("占位"),  # 第 1 步：已被复核为未生效，不再执行
            ToolCall("transfer", {"src": "alice", "dst": "bob", "amount": 250}),
            Finish("转账完成"),
        ]
    )
    engine = Engine(policy, registry, log)

    result = engine.resume(RUN)

    assert result.stop_reason is StopReason.SUCCEEDED
    assert result.view.answer == "转账完成"
    assert [step.step for step in result.view.steps] == [1, 2]
    assert result.view.steps[0].outcome is Outcome.FAILED
    assert result.view.steps[1].outcome is Outcome.SUCCEEDED
    assert result.view.steps[1].step_key == make_step_key(RUN, 2)
    assert ledger.balance("alice") == 750
    assert ledger.balance("bob") == 250
    # 重做是新步骤，幂等键不同——否则账本会把第二次调用当成第一次的重试
    assert result.view.steps[0].step_key != result.view.steps[1].step_key


def test_reconcile_can_complete_an_uncertain_step(tmp_path: Path) -> None:
    # 工具抛了异常、引擎写下 STEP_UNCERTAIN：这不是终局结果，对账命中后应补记成功
    log = seeded_log()
    log.append(RUN, EventType.STEP_UNCERTAIN, {"step": 1, "reason": "响应丢失"})
    ledger = Ledger(tmp_path / "ledger", initial={"alice": 1000, "bob": 0})
    ledger.transfer(ALICE, BOB, 250, request_id=make_step_key(RUN, 1))

    from agentrun.ledger_tools import LookupRequest

    result = reconcile(log, RUN, LookupRequest(ledger))

    view = view_of(log)
    assert result.recorded is True
    assert view.steps[0].outcome is Outcome.SUCCEEDED
    assert ledger.balance(ALICE) == 750


def test_completed_after_uncertain_is_allowed_only_once() -> None:
    log = seeded_log()
    log.append(RUN, EventType.STEP_UNCERTAIN, {"step": 1, "reason": "响应丢失"})
    view = view_of(log)

    converged = apply(
        view,
        ev(view.last_seq + 1, EventType.STEP_COMPLETED, step=1, result={"found": True}),
    )
    assert converged.steps[0].outcome is Outcome.SUCCEEDED

    with pytest.raises(InvalidEvent):
        apply(
            converged,
            ev(converged.last_seq + 1, EventType.STEP_COMPLETED, step=1, result={"found": True}),
        )
    with pytest.raises(InvalidEvent):
        apply(
            view,
            ev(view.last_seq + 1, EventType.STEP_FAILED, step=1, error={"kind": "rejected"}),
        )


def test_review_request_accepts_an_uncertain_step() -> None:
    log = seeded_log()
    log.append(RUN, EventType.STEP_UNCERTAIN, {"step": 1, "reason": "响应丢失"})

    request_review(log, "查不到，需要人工确认")

    view = view_of(log)
    assert view.status is RunStatus.WAITING_REVIEW
    assert view.review is not None
    assert view.review.reason == "查不到，需要人工确认"


def test_resolve_uncertain_step_overrides_outcome() -> None:
    log = seeded_log()
    log.append(RUN, EventType.STEP_UNCERTAIN, {"step": 1, "reason": "响应丢失"})
    request_review(log)

    view = resolve(log, RUN, decision="not_applied", note="人工核对无记录")

    assert view.status is RunStatus.RUNNING
    assert view.steps[0].outcome is Outcome.FAILED
    assert view.review is not None
    assert view.review.decision == "not_applied"


def test_review_request_only_accepts_a_pending_step_via_apply() -> None:
    # 直接对视图做折叠，确认非法请求不会返回半成品
    view = apply(None, ev(1, EventType.RUN_STARTED, task="t", max_steps=3))

    with pytest.raises(InvalidEvent):
        apply(view, ev(2, EventType.REVIEW_REQUESTED, step=1, reason="没有待定步骤"))
