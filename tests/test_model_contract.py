"""事件折叠的契约测试。

这些测试锁定第 1B 轮确认的契约：事件顺序、状态转移和非法组合。
只调用公开接口，不检查实现细节；错误消息内容不做断言，只断言异常类型。
"""

from typing import Any

import pytest

from agentrun.events import Event, EventType, make_step_key
from agentrun.model import InvalidEvent, Outcome, RunStatus, apply, replay

RUN = "run-1"
AT = "2026-10-19T00:00:00Z"


def ev(seq: int, type_: EventType, **payload: Any) -> Event:
    return Event(run_id=RUN, seq=seq, type=type_, payload=payload, at=AT)


def started(max_steps: int = 5) -> Event:
    return ev(1, EventType.RUN_STARTED, task="向 bob 转 250", max_steps=max_steps)


def decided(seq: int, step: int = 1, tool: str = "get_balance") -> Event:
    return ev(
        seq,
        EventType.STEP_DECIDED,
        step=step,
        tool=tool,
        args={"owner": "alice"},
        step_key=make_step_key(RUN, step),
    )


def started_step(seq: int, step: int = 1, attempt: int = 1) -> Event:
    return ev(seq, EventType.STEP_STARTED, step=step, attempt=attempt)


def base_view() -> Any:
    return apply(None, started())


# --- RUN_STARTED ---------------------------------------------------------


def test_run_started_builds_view() -> None:
    view = apply(None, started())
    assert view.run_id == RUN
    assert view.task == "向 bob 转 250"
    assert view.max_steps == 5
    assert view.status is RunStatus.RUNNING
    assert view.steps == ()
    assert view.answer is None


def test_first_event_must_be_run_started() -> None:
    with pytest.raises(InvalidEvent):
        apply(None, decided(1))


def test_first_seq_must_be_one() -> None:
    with pytest.raises(InvalidEvent):
        apply(None, ev(2, EventType.RUN_STARTED, task="t", max_steps=1))


def test_negative_max_steps_rejected() -> None:
    with pytest.raises(InvalidEvent):
        apply(None, started(max_steps=-1))


def test_payload_missing_field_rejected() -> None:
    # 缺 tool，且必须是 InvalidEvent 而不是 KeyError
    with pytest.raises(InvalidEvent):
        apply(base_view(), ev(2, EventType.STEP_DECIDED, step=1, args={}, step_key="k"))


# --- 顺序 ----------------------------------------------------------------


def test_seq_must_be_contiguous() -> None:
    view = apply(None, started())
    with pytest.raises(InvalidEvent):
        apply(view, decided(3))


def test_steps_must_be_contiguous() -> None:
    with pytest.raises(InvalidEvent):
        apply(base_view(), decided(2, step=2))


# --- 决策与意图 ----------------------------------------------------------


def test_decided_appends_step() -> None:
    view = apply(base_view(), decided(2))
    assert len(view.steps) == 1
    step = view.steps[0]
    assert step.step == 1
    assert step.tool == "get_balance"
    assert step.args == {"owner": "alice"}
    assert step.step_key == make_step_key(RUN, 1)
    assert step.attempts == 0
    assert step.outcome is None


def test_started_increments_attempts() -> None:
    view = apply(base_view(), decided(2))
    view = apply(view, started_step(3))
    assert view.steps[0].attempts == 1
    assert view.steps[0].outcome is None

    view = apply(view, started_step(4, attempt=2))
    assert view.steps[0].attempts == 2
    assert view.steps[0].outcome is None


def test_result_requires_intent() -> None:
    view = apply(base_view(), decided(2))
    with pytest.raises(InvalidEvent):
        apply(view, ev(3, EventType.STEP_COMPLETED, step=1, result={"ok": True}))


# --- 结果 ----------------------------------------------------------------


def test_completed_sets_outcome_and_result() -> None:
    view = apply(base_view(), decided(2))
    view = apply(view, started_step(3))
    view = apply(view, ev(4, EventType.STEP_COMPLETED, step=1, result={"balance": 1000}))

    step = view.steps[0]
    assert step.outcome is Outcome.SUCCEEDED
    assert step.result == {"balance": 1000}
    assert step.error is None
    assert step.reason is None


def test_failed_sets_outcome_and_error() -> None:
    view = apply(base_view(), decided(2))
    view = apply(view, started_step(3))
    view = apply(view, ev(4, EventType.STEP_FAILED, step=1, error={"kind": "rejected"}))

    step = view.steps[0]
    assert step.outcome is Outcome.FAILED
    assert step.error == {"kind": "rejected"}
    assert step.result is None


def test_uncertain_is_distinct_from_failed() -> None:
    view = apply(base_view(), decided(2))
    view = apply(view, started_step(3))
    view = apply(view, ev(4, EventType.STEP_UNCERTAIN, step=1, reason="timeout"))

    step = view.steps[0]
    assert step.outcome is Outcome.UNCERTAIN
    assert step.outcome is not Outcome.FAILED
    assert step.reason == "timeout"
    assert step.error is None


def test_second_outcome_rejected() -> None:
    view = apply(base_view(), decided(2))
    view = apply(view, started_step(3))
    view = apply(view, ev(4, EventType.STEP_COMPLETED, step=1, result={}))
    with pytest.raises(InvalidEvent):
        apply(view, ev(5, EventType.STEP_FAILED, step=1, error={"kind": "x"}))


# --- 终态 ----------------------------------------------------------------


@pytest.mark.parametrize("answer", ["", "   ", "\n\t"])
def test_run_succeeded_requires_non_blank_answer(answer: str) -> None:
    with pytest.raises(InvalidEvent):
        apply(base_view(), ev(2, EventType.RUN_SUCCEEDED, answer=answer))


def test_run_succeeded_sets_answer() -> None:
    view = apply(base_view(), ev(2, EventType.RUN_SUCCEEDED, answer="已转账，余额 750"))
    assert view.status is RunStatus.SUCCEEDED
    assert view.answer == "已转账，余额 750"


def test_exhausted_is_not_success() -> None:
    view = apply(base_view(), ev(2, EventType.RUN_EXHAUSTED, max_steps=5))
    assert view.status is RunStatus.EXHAUSTED
    assert view.answer is None


def test_failed_sets_run_status() -> None:
    view = apply(base_view(), ev(2, EventType.RUN_FAILED, reason="tool crashed"))
    assert view.status is RunStatus.FAILED
    assert view.answer is None


@pytest.mark.parametrize(
    "terminal",
    [
        ev(2, EventType.RUN_SUCCEEDED, answer="done"),
        ev(2, EventType.RUN_FAILED, reason="x"),
        ev(2, EventType.RUN_EXHAUSTED, max_steps=5),
    ],
)
def test_no_events_after_terminal(terminal: Event) -> None:
    view = apply(base_view(), terminal)
    with pytest.raises(InvalidEvent):
        apply(view, decided(3))


# --- 不变性与重放 --------------------------------------------------------


def test_apply_does_not_mutate_previous_view() -> None:
    first = apply(None, started())
    second = apply(first, decided(2))
    assert first.steps == ()
    assert len(second.steps) == 1
    assert first is not second


def test_replay_matches_sequential_apply() -> None:
    events = [
        started(),
        decided(2),
        started_step(3),
        ev(4, EventType.STEP_COMPLETED, step=1, result={"balance": 1000}),
        ev(5, EventType.RUN_SUCCEEDED, answer="余额 1000，无需转账"),
    ]

    view: Any = None
    for event in events:
        view = apply(view, event)

    assert replay(events) == view


def test_replay_empty_raises() -> None:
    with pytest.raises(InvalidEvent):
        replay([])


def test_step_args_are_isolated_from_event_payload() -> None:
    # 观察者改视图，不能污染日志里的事实
    event = decided(2)
    view = apply(apply(None, started()), event)

    view.steps[0].args["owner"] = "bob"  # type: ignore[index]

    assert event.payload["args"] == {"owner": "alice"}


def test_step_args_nested_structures_are_isolated() -> None:
    event = ev(2, EventType.STEP_DECIDED, step=1, tool="t", args={"a": {"b": 1}}, step_key="k")
    view = apply(apply(None, started()), event)

    view.steps[0].args["a"]["b"] = 99  # type: ignore[index]

    assert event.payload["args"]["a"]["b"] == 1
