"""工具协议与单次执行的契约测试。

用计数工具证明“调用了几次”，而不是只看返回值。参数不合法时，工具必须一次都
没被调用——这是本文件最重要的断言。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from agentrun.tools import (
    DuplicateTool,
    InvalidArgs,
    SideEffect,
    ToolContext,
    ToolCrashed,
    ToolOutcome,
    ToolRegistry,
    ToolRejected,
    ToolStatus,
    UnknownTool,
    run_tool,
)

CTX = ToolContext(run_id="run-1", step=1, attempt=1, step_key="run-1:1")


@dataclass
class CountingTool:
    """可控失败的计数工具，记录 validate / run 各被调用几次。"""

    name: str = "counter"
    side_effect: SideEffect = SideEffect.NON_IDEMPOTENT
    value: dict[str, Any] = field(default_factory=lambda: {"ok": True})
    validate_error: Exception | None = None
    run_error: Exception | None = None
    validate_calls: int = 0
    run_calls: int = 0
    seen_args: Mapping[str, Any] | None = None
    seen_ctx: ToolContext | None = None

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        self.validate_calls += 1
        if self.validate_error is not None:
            raise self.validate_error

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        self.run_calls += 1
        self.seen_args = args
        self.seen_ctx = ctx
        if self.run_error is not None:
            raise self.run_error
        return self.value


# --- 成功 ----------------------------------------------------------------


def test_success_runs_exactly_once() -> None:
    tool = CountingTool()
    outcome = run_tool(tool, {"x": 1}, CTX)

    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.value == {"ok": True}
    assert outcome.error is None
    assert outcome.reason is None
    assert (tool.validate_calls, tool.run_calls) == (1, 1)


def test_args_are_passed_through_unchanged() -> None:
    tool = CountingTool()
    args = {"owner": "alice"}
    run_tool(tool, args, CTX)
    assert tool.seen_args == args


def test_context_reaches_the_tool() -> None:
    tool = CountingTool()
    run_tool(tool, {}, CTX)
    assert tool.seen_ctx == CTX


# --- 参数不合法 ----------------------------------------------------------


def test_invalid_args_never_reach_run() -> None:
    tool = CountingTool(validate_error=InvalidArgs("amount 必须为正"))
    outcome = run_tool(tool, {"amount": -1}, CTX)

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error["kind"] == "invalid_args"
    assert tool.validate_calls == 1
    assert tool.run_calls == 0


def test_validate_crash_is_failed_because_tool_never_ran() -> None:
    # 分类依据是“工具有没有被执行过”，不是异常类型
    tool = CountingTool(validate_error=RuntimeError("校验函数自己崩了"))
    outcome = run_tool(tool, {}, CTX)

    assert outcome.status is ToolStatus.FAILED
    assert tool.run_calls == 0


def test_non_mapping_args_fail_without_any_tool_call() -> None:
    tool = CountingTool()
    outcome = run_tool(tool, ["not", "a", "mapping"], CTX)  # type: ignore[arg-type]

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error["kind"] == "invalid_args"
    assert (tool.validate_calls, tool.run_calls) == (0, 0)


# --- 明确失败 ------------------------------------------------------------


def test_rejected_is_failed_and_counted_once() -> None:
    tool = CountingTool(run_error=ToolRejected("余额不足"))
    outcome = run_tool(tool, {}, CTX)

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error["kind"] == "rejected"
    assert tool.run_calls == 1


# --- 未知 ----------------------------------------------------------------


def test_crashed_is_uncertain() -> None:
    tool = CountingTool(run_error=ToolCrashed("连接中断"))
    outcome = run_tool(tool, {}, CTX)

    assert outcome.status is ToolStatus.UNCERTAIN
    assert outcome.reason is not None
    assert outcome.error is None


def test_unexpected_exception_is_uncertain_not_failed() -> None:
    # 关键断言：没有收到明确拒绝，就不能声称“确定没生效”
    tool = CountingTool(run_error=RuntimeError("socket closed"))
    outcome = run_tool(tool, {}, CTX)

    assert outcome.status is ToolStatus.UNCERTAIN
    assert outcome.status is not ToolStatus.FAILED
    assert outcome.reason is not None


def test_non_mapping_result_is_uncertain_not_success() -> None:
    # 工具确实执行过，副作用可能已发生；返回值不可落盘，所以不能算成功
    tool = CountingTool()
    tool.value = ["not", "a", "mapping"]  # type: ignore[assignment]
    outcome = run_tool(tool, {}, CTX)

    assert outcome.status is ToolStatus.UNCERTAIN
    assert outcome.status is not ToolStatus.SUCCEEDED
    assert outcome.value is None
    assert outcome.reason is not None
    assert tool.run_calls == 1


def test_none_result_is_uncertain() -> None:
    tool = CountingTool()
    tool.value = None  # type: ignore[assignment]
    outcome = run_tool(tool, {}, CTX)

    assert outcome.status is ToolStatus.UNCERTAIN
    assert outcome.value is None
    assert tool.run_calls == 1


def test_outcome_is_a_plain_value_object() -> None:
    outcome = ToolOutcome(status=ToolStatus.UNCERTAIN, reason="未确认")
    assert outcome.value is None
    assert outcome.error is None


# --- 注册表 --------------------------------------------------------------


def test_registry_roundtrip() -> None:
    registry = ToolRegistry()
    tool = CountingTool(name="get_balance")
    registry.register(tool)

    assert registry.get("get_balance") is tool
    assert registry.names() == ("get_balance",)


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(CountingTool(name="transfer"))

    with pytest.raises(DuplicateTool):
        registry.register(CountingTool(name="transfer"))

    # 原工具未被覆盖
    assert registry.get("transfer").side_effect is SideEffect.NON_IDEMPOTENT


def test_unknown_tool_is_rejected() -> None:
    registry = ToolRegistry()
    with pytest.raises(UnknownTool):
        registry.get("nope")


def test_names_are_sorted() -> None:
    registry = ToolRegistry()
    for name in ("transfer", "get_balance", "save_receipt"):
        registry.register(CountingTool(name=name))

    assert registry.names() == ("get_balance", "save_receipt", "transfer")


def test_tool_declares_side_effect() -> None:
    tool = CountingTool(name="get_balance", side_effect=SideEffect.READ_ONLY)
    assert tool.side_effect is SideEffect.READ_ONLY
