"""续跑契约测试：终态不重跑、未完成步骤先对账、预算取自日志。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from agentrun.engine import Engine, NothingToResume, StopReason
from agentrun.events import EventType, make_step_key
from agentrun.journal import MemoryEventLog
from agentrun.model import RunStatus
from agentrun.policy import Finish, ScriptedPolicy, ToolCall
from agentrun.tools import SideEffect, ToolContext, ToolRegistry

RUN = "run-1"


@dataclass
class EchoTool:
    """只读工具，记录自己被调用了几次。"""

    name: str = "echo"
    side_effect: SideEffect = SideEffect.READ_ONLY
    calls: int = 0

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        return None

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        self.calls += 1
        return {"echo": dict(args)}


def build() -> tuple[Engine, MemoryEventLog, EchoTool]:
    """构造一个「先调 echo、再结束」的引擎。"""
    log = MemoryEventLog()
    tool = EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    policy = ScriptedPolicy([ToolCall("echo", {"i": 1}), Finish("完成")])
    return Engine(policy, registry, log), log, tool


def seed_event(log: MemoryEventLog, type_: EventType, payload: dict[str, Any]) -> None:
    """向内存日志追加一条原始事件。"""
    log.append(RUN, type_, payload)


def seed_decided(log: MemoryEventLog, step: int) -> None:
    """追加一条 STEP_DECIDED。"""
    seed_event(
        log,
        EventType.STEP_DECIDED,
        {
            "step": step,
            "tool": "echo",
            "args": {"i": step},
            "step_key": make_step_key(RUN, step),
        },
    )


def test_resume_unknown_run_raises() -> None:
    engine, _, _ = build()
    with pytest.raises(NothingToResume):
        engine.resume(RUN)


def test_resume_finished_task_returns_view_without_appending() -> None:
    engine, log, tool = build()
    first = engine.run(RUN, "任务", max_steps=5)
    before = log.read(RUN)

    second = engine.resume(RUN)

    assert second.view == first.view
    assert second.stop_reason is StopReason.SUCCEEDED
    assert log.read(RUN) == before
    assert tool.calls == 1


def test_resume_continues_from_decided_prefix() -> None:
    engine, log, tool = build()
    seed_event(log, EventType.RUN_STARTED, {"task": "t", "max_steps": 5})
    seed_decided(log, 1)
    seed_event(log, EventType.STEP_STARTED, {"step": 1, "attempt": 1})
    seed_event(log, EventType.STEP_COMPLETED, {"step": 1, "result": {"echo": {"i": 1}}})

    result = engine.resume(RUN)

    assert result.stop_reason is StopReason.SUCCEEDED
    assert result.view.status is RunStatus.SUCCEEDED
    assert result.view.answer == "完成"
    assert [step.step for step in result.view.steps] == [1]
    assert tool.calls == 0


def test_resume_stops_when_last_step_has_no_outcome() -> None:
    # 进程死在 STEP_STARTED 与结果事件之间：不许重做，必须先对账
    engine, log, tool = build()
    seed_event(log, EventType.RUN_STARTED, {"task": "t", "max_steps": 5})
    seed_decided(log, 1)
    seed_event(log, EventType.STEP_STARTED, {"step": 1, "attempt": 1})
    before = log.read(RUN)

    result = engine.resume(RUN)

    assert result.stop_reason is StopReason.UNCERTAIN
    assert result.view.status is RunStatus.RUNNING
    assert result.view.steps[0].outcome is None
    assert log.read(RUN) == before
    assert tool.calls == 0


def test_resume_uses_budget_recorded_in_log() -> None:
    engine, log, tool = build()
    seed_event(log, EventType.RUN_STARTED, {"task": "t", "max_steps": 1})
    seed_decided(log, 1)
    seed_event(log, EventType.STEP_STARTED, {"step": 1, "attempt": 1})
    seed_event(log, EventType.STEP_COMPLETED, {"step": 1, "result": {"echo": {"i": 1}}})

    result = engine.resume(RUN)

    assert result.stop_reason is StopReason.EXHAUSTED
    assert result.view.status is RunStatus.EXHAUSTED
    assert tool.calls == 0


def test_resume_after_recorded_uncertain_waits_for_reconciliation() -> None:
    # STEP_UNCERTAIN 不是终局结果，但在对账/复核收敛之前，续跑一步也不能走
    log = MemoryEventLog()
    log.append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 5})
    seed_decided(log, 1)
    seed_event(log, EventType.STEP_STARTED, {"step": 1, "attempt": 1})
    seed_event(log, EventType.STEP_UNCERTAIN, {"step": 1, "reason": "响应丢失"})
    before = log.read(RUN)

    result = Engine(ScriptedPolicy([Finish("不该执行")]), ToolRegistry(), log).resume(RUN)

    assert result.stop_reason is StopReason.UNCERTAIN
    assert log.read(RUN) == before


def test_resume_executes_decided_step_without_deciding_again() -> None:
    # 决策已落盘、意图未落盘：复用决策，补写 STEP_STARTED 后执行；不重新问策略
    log = MemoryEventLog()
    tool = EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    log.append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 2})
    seed_decided(log, 1)

    class RecordingPolicy:
        def __init__(self) -> None:
            self.seen: list[int] = []

        def decide(self, view: Any) -> Finish:
            self.seen.append(len(view.steps))
            return Finish("完成")

    policy = RecordingPolicy()
    result = Engine(policy, registry, log).resume(RUN)

    assert tool.calls == 1
    assert policy.seen == [1]
    assert result.stop_reason is StopReason.SUCCEEDED
    assert [event.type for event in log.read(RUN)] == [
        EventType.RUN_STARTED,
        EventType.STEP_DECIDED,
        EventType.STEP_STARTED,
        EventType.STEP_COMPLETED,
        EventType.RUN_SUCCEEDED,
    ]


def test_resume_decided_step_with_unknown_tool_fails_without_starting() -> None:
    log = MemoryEventLog()
    log.append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 2})
    seed_decided(log, 1)

    result = Engine(ScriptedPolicy([Finish("不该执行")]), ToolRegistry(), log).resume(RUN)

    assert result.stop_reason is StopReason.FAILED
    assert [event.type for event in log.read(RUN)] == [
        EventType.RUN_STARTED,
        EventType.STEP_DECIDED,
        EventType.RUN_FAILED,
    ]
