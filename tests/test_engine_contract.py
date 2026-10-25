"""引擎契约测试：事件顺序、预算边界与停止条件。

这些测试只看两样东西：日志里的事件序列，以及最终视图。工具调用次数用计数工具
单独验证，避免“返回值对了但调用了两次”这种漏网。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentrun.engine import Engine, EngineResult, StopReason
from agentrun.events import EventType
from agentrun.journal import MemoryEventLog
from agentrun.model import Outcome, RunStatus, replay
from agentrun.policy import Action, Finish, PolicyError, ScriptedPolicy, ToolCall
from agentrun.tools import SideEffect, ToolContext, ToolRegistry, ToolRejected


@dataclass
class FakeTool:
    name: str = "fake"
    side_effect: SideEffect = SideEffect.READ_ONLY
    value: dict[str, Any] = field(default_factory=lambda: {"ok": True})
    error: Exception | None = None
    calls: int = 0

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        return None

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


class QueuePolicy:
    """按顺序返回动作，仅用于测试引擎；真实策略必须是视图驱动的。"""

    def __init__(self, actions: Sequence[Action]) -> None:
        self._actions = list(actions)
        self._index = 0
        self.calls = 0

    def decide(self, view: Any) -> Action:
        self.calls += 1
        action = self._actions[self._index]
        self._index += 1
        return action


def build(
    actions: Sequence[Action], tools: Sequence[FakeTool]
) -> tuple[Engine, MemoryEventLog, QueuePolicy]:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    log = MemoryEventLog()
    policy = QueuePolicy(actions)
    engine = Engine(policy, registry, log)
    return engine, log, policy


def run(
    actions: Sequence[Action],
    tools: Sequence[FakeTool],
    max_steps: int = 5,
) -> tuple[EngineResult, MemoryEventLog, list[EventType]]:
    engine, log, _ = build(actions, tools)
    result = engine.run("run-1", "测试任务", max_steps)
    types = [event.type for event in log.read("run-1")]
    return result, log, types


# --- 正常路径 ------------------------------------------------------------


def test_full_run_appends_events_in_contract_order() -> None:
    balance = FakeTool(name="get_balance", value={"balance": 1000})
    transfer = FakeTool(name="transfer", side_effect=SideEffect.NON_IDEMPOTENT)

    result, _, types = run(
        [
            ToolCall("get_balance", {"owner": "alice"}),
            ToolCall("transfer", {"amount": 250}),
            ToolCall("get_balance", {"owner": "bob"}),
            Finish("已转账"),
        ],
        [balance, transfer],
    )

    expected: list[EventType] = [EventType.RUN_STARTED]
    for _ in range(3):
        expected += [
            EventType.STEP_DECIDED,
            EventType.STEP_STARTED,
            EventType.STEP_COMPLETED,
        ]
    expected.append(EventType.RUN_SUCCEEDED)

    assert types == expected
    assert result.stop_reason is StopReason.SUCCEEDED
    assert result.view.status is RunStatus.SUCCEEDED
    assert result.view.answer == "已转账"
    assert len(result.view.steps) == 3


def test_seq_is_contiguous() -> None:
    _, log, _ = run([ToolCall("fake", {}), Finish("完成")], [FakeTool()])
    assert [event.seq for event in log.read("run-1")] == list(range(1, len(log.read("run-1")) + 1))


def test_step_key_is_stable_and_has_no_attempt() -> None:
    result, _, _ = run([ToolCall("fake", {}), Finish("完成")], [FakeTool()])
    assert result.view.steps[0].step_key == "run-1:1"


def test_each_step_calls_tool_exactly_once() -> None:
    tool = FakeTool()
    result, _, _ = run([ToolCall("fake", {}), ToolCall("fake", {}), Finish("完成")], [tool])
    assert tool.calls == 2
    assert result.stop_reason is StopReason.SUCCEEDED


def test_result_view_equals_replay() -> None:
    result, log, _ = run([ToolCall("fake", {}), Finish("完成")], [FakeTool()])
    assert result.view == replay(log.read("run-1"))


# --- 预算 ----------------------------------------------------------------


def test_zero_budget_stops_before_any_decision() -> None:
    tool = FakeTool()
    engine, log, policy = build([ToolCall("fake", {})], [tool])

    result = engine.run("run-1", "测试任务", 0)

    assert [event.type for event in log.read("run-1")] == [
        EventType.RUN_STARTED,
        EventType.RUN_EXHAUSTED,
    ]
    assert result.stop_reason is StopReason.EXHAUSTED
    assert result.view.status is RunStatus.EXHAUSTED
    assert result.view.steps == ()
    assert tool.calls == 0
    assert policy.calls == 0


def test_budget_counts_decisions_not_attempts() -> None:
    result, _, types = run(
        [ToolCall("fake", {}), ToolCall("fake", {}), Finish("完成")],
        [FakeTool()],
        max_steps=2,
    )

    assert len(result.view.steps) == 2
    assert types[-1] is EventType.RUN_EXHAUSTED
    assert result.stop_reason is StopReason.EXHAUSTED


# --- 工具结果 ------------------------------------------------------------


def test_rejected_step_does_not_fail_the_run() -> None:
    transfer = FakeTool(name="transfer", error=ToolRejected("余额不足"))
    result, _, types = run(
        [ToolCall("transfer", {}), Finish("余额不足，未转账")],
        [transfer],
    )

    assert result.view.steps[0].outcome is Outcome.FAILED
    assert types[-1] is EventType.RUN_SUCCEEDED
    assert result.stop_reason is StopReason.SUCCEEDED


def test_uncertain_stops_immediately_and_keeps_run_open() -> None:
    flaky = FakeTool(name="transfer", error=RuntimeError("连接中断"))
    result, _, types = run(
        [ToolCall("transfer", {}), Finish("不该走到这里")],
        [flaky],
    )

    assert types == [
        EventType.RUN_STARTED,
        EventType.STEP_DECIDED,
        EventType.STEP_STARTED,
        EventType.STEP_UNCERTAIN,
    ]
    assert result.view.steps[0].outcome is Outcome.UNCERTAIN
    assert result.view.status is RunStatus.RUNNING
    assert result.stop_reason is StopReason.UNCERTAIN


def test_unknown_tool_fails_the_run() -> None:
    result, _, types = run([ToolCall("nope", {})], [])

    assert types == [EventType.RUN_STARTED, EventType.RUN_FAILED]
    assert result.view.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.FAILED
    assert result.view.steps == ()
    assert result.view.failure_reason == "未注册的工具：'nope'"


# --- 决策失败 ------------------------------------------------------------


class FailingPolicy:
    """模拟策略给不出动作：模型输错了，或脚本用完了。"""

    def decide(self, view: Any) -> Action:
        raise PolicyError("模型输出不是合法 JSON")


def test_policy_error_fails_the_run_without_calling_tools() -> None:
    tool = FakeTool()
    registry = ToolRegistry()
    registry.register(tool)
    log = MemoryEventLog()
    engine = Engine(FailingPolicy(), registry, log)

    result = engine.run("run-1", "测试任务", 5)

    assert [event.type for event in log.read("run-1")] == [
        EventType.RUN_STARTED,
        EventType.RUN_FAILED,
    ]
    assert log.read("run-1")[1].payload["reason"] == "模型输出不是合法 JSON"
    assert result.view.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.FAILED
    assert result.view.steps == ()
    assert tool.calls == 0


def test_exhausted_script_fails_the_run_instead_of_crashing() -> None:
    log = MemoryEventLog()
    engine = Engine(ScriptedPolicy([]), ToolRegistry(), log)

    result = engine.run("run-1", "测试任务", 5)

    assert [event.type for event in log.read("run-1")] == [
        EventType.RUN_STARTED,
        EventType.RUN_FAILED,
    ]
    assert result.stop_reason is StopReason.FAILED
