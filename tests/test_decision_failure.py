"""决策失败不是终局（19D2）：先于实现写下的契约测试。

这份文件红在哪，就说明契约还缺哪一块。要求的符号：

- ``events.EventType.DECISION_FAILED``
- ``model.RunView.decision_failures: int``（连续决策失败次数，由 ``apply`` 折出来）
- ``policy.PolicyError(msg, *, retryable: bool = False)``
- ``engine.MAX_DECISION_FAILURES: int``（连续失败上限，建议 3）
- ``engine.StopReason.DECISION_FAILED``（**非终态**，任务仍是 ``RUNNING``）
- ``ModelPolicy`` 把 ``ModelClientError(retryable=...)`` 透成 ``PolicyError(retryable=...)``

语义约定（与第 19 轮计划一致）：

1. ``DECISION_FAILED`` 只能出现在 ``RUNNING`` 且**没有悬挂步骤**的时候——
   决策失败发生在任何副作用之前，若上一步还没结果，那不是决策失败，是待复核。
2. payload ``{"reason": str, "attempt": int}``，``attempt = decision_failures + 1``。
3. 计数器只在“真的拿到动作”时清零：``STEP_DECIDED`` 与 ``RUN_SUCCEEDED`` 都清零。
4. 可重试失败累计到上限就写 ``RUN_FAILED``（终态），避免端点挂掉时任务永远悬着。
5. 不可重试的失败（401 / 404 / 参数错）立刻 ``RUN_FAILED``，一次也不攒。
"""

from collections.abc import Mapping
from typing import Any

import pytest

from agentrun import cli
from agentrun import engine as engine_module
from agentrun.engine import Engine, StopReason
from agentrun.events import Event, EventType, make_step_key
from agentrun.journal import MemoryEventLog, SqliteEventLog
from agentrun.llm import ModelPolicy
from agentrun.model import InvalidEvent, RunStatus, RunView, replay
from agentrun.model_client import ModelClientError
from agentrun.policy import Action, Finish, Policy, PolicyError
from agentrun.tools import SideEffect, ToolContext, ToolRegistry

RUN = "run-1"
AT = "2026-10-31T00:00:00Z"


def ev(seq: int, type_: EventType, **payload: Any) -> Event:
    """构造一条事件，``run_id`` 与 ``at`` 固定。"""
    return Event(run_id=RUN, seq=seq, type=type_, payload=payload, at=AT)


def started(max_steps: int = 5) -> Event:
    """首条事件：任务开始。"""
    return ev(1, EventType.RUN_STARTED, task="向 bob 转 250", max_steps=max_steps)


def decided(seq: int, step: int = 1) -> Event:
    """一次成功决策：选定工具并生成 step_key。"""
    return ev(
        seq,
        EventType.STEP_DECIDED,
        step=step,
        tool="get_balance",
        args={"owner": "alice"},
        step_key=make_step_key(RUN, step),
    )


def started_step(seq: int, step: int = 1) -> Event:
    """步骤开始，但还没有结果——这就是「悬挂」。"""
    return ev(seq, EventType.STEP_STARTED, step=step, attempt=1)


def failed_decision(seq: int, attempt: int = 1, reason: str = "网络超时") -> Event:
    """一次决策失败。"""
    return ev(seq, EventType.DECISION_FAILED, reason=reason, attempt=attempt)


class FailingPolicy:
    """先失败 ``times`` 次，然后给出 ``Finish``。

    Notes:
        这是测试替身：真实策略不允许依赖内存状态（D18），这里用它模拟“网络恢复”。
    """

    def __init__(self, times: int, *, retryable: bool = True) -> None:
        self._left = times
        self._retryable = retryable
        self.calls = 0

    def decide(self, view: RunView) -> Action:
        """按剩余次数决定失败还是结束。"""
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise PolicyError("上游 503", retryable=self._retryable)
        return Finish("完成")


class EchoTool:
    """永远成功的读工具，用来验证“决策失败后仍能继续跑”。"""

    name = "get_balance"
    side_effect = SideEffect.READ_ONLY

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """无参数要求。"""

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        """返回一个固定结果。"""
        return {"balance": 1000}


# --- 折叠层：事件合法性 ---------------------------------------------------


def test_decision_failed_is_not_terminal() -> None:
    view = replay([started(), failed_decision(2)])
    assert view.status is RunStatus.RUNNING
    assert view.decision_failures == 1


def test_decision_failed_attempt_must_be_next() -> None:
    with pytest.raises(InvalidEvent):
        replay([started(), failed_decision(2, attempt=2)])


def test_decision_failed_rejected_when_step_is_dangling() -> None:
    """上一步已开始却没有结果，说明该走复核，不是决策失败。"""
    with pytest.raises(InvalidEvent):
        replay([started(), decided(2), started_step(3), failed_decision(4)])


def test_decision_failed_rejected_after_terminal() -> None:
    with pytest.raises(InvalidEvent):
        replay(
            [
                started(),
                ev(2, EventType.RUN_FAILED, reason="业务拒绝"),
                failed_decision(3),
            ]
        )


def test_decision_failed_requires_non_blank_reason() -> None:
    with pytest.raises(InvalidEvent):
        replay([started(), failed_decision(2, reason="   ")])


def test_successful_decision_resets_counter() -> None:
    view = replay([started(), failed_decision(2), failed_decision(3, attempt=2), decided(4)])
    assert view.decision_failures == 0


def test_successful_finish_resets_counter() -> None:
    view = replay([started(), failed_decision(2), ev(3, EventType.RUN_SUCCEEDED, answer="完成")])
    assert view.decision_failures == 0


# --- 引擎层：暂停而不是判死 -----------------------------------------------


def build_engine(log: MemoryEventLog, policy: Policy) -> Engine:
    """装配一个带 ``EchoTool`` 的引擎。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    return Engine(policy, registry, log)


def test_retryable_failure_pauses_instead_of_failing() -> None:
    log = MemoryEventLog()
    engine = build_engine(log, FailingPolicy(times=1))

    result = engine.run(RUN, "测试任务", 5)

    assert result.stop_reason is StopReason.DECISION_FAILED
    assert result.view.status is RunStatus.RUNNING
    assert result.view.decision_failures == 1
    assert [item.type for item in log.read(RUN)] == [
        EventType.RUN_STARTED,
        EventType.DECISION_FAILED,
    ]


def test_resume_continues_after_decision_failure() -> None:
    log = MemoryEventLog()
    policy = FailingPolicy(times=1)
    engine = build_engine(log, policy)

    assert engine.run(RUN, "测试任务", 5).stop_reason is StopReason.DECISION_FAILED
    result = engine.resume(RUN)

    assert result.stop_reason is StopReason.SUCCEEDED
    assert result.view.decision_failures == 0
    assert [item.type for item in log.read(RUN)] == [
        EventType.RUN_STARTED,
        EventType.DECISION_FAILED,
        EventType.RUN_SUCCEEDED,
    ]


def test_non_retryable_failure_is_terminal() -> None:
    log = MemoryEventLog()
    engine = build_engine(log, FailingPolicy(times=1, retryable=False))

    result = engine.run(RUN, "测试任务", 5)

    assert result.stop_reason is StopReason.FAILED
    assert result.view.status is RunStatus.FAILED
    assert EventType.DECISION_FAILED not in [item.type for item in log.read(RUN)]


def test_consecutive_failures_stop_at_the_cap() -> None:
    """一直失败：攒满上限后必须进入终态，不能无限暂停。"""
    max_failures = engine_module.MAX_DECISION_FAILURES
    log = MemoryEventLog()
    engine = build_engine(log, FailingPolicy(times=99))

    result = engine.run(RUN, "测试任务", 5)
    rounds = 1
    while result.stop_reason is StopReason.DECISION_FAILED and rounds < 10:
        result = engine.resume(RUN)
        rounds += 1

    assert rounds == max_failures
    assert result.stop_reason is StopReason.FAILED
    assert result.view.status is RunStatus.FAILED
    kinds = [item.type for item in log.read(RUN)]
    assert kinds.count(EventType.DECISION_FAILED) == max_failures - 1
    assert kinds[-1] is EventType.RUN_FAILED


def test_counter_survives_a_new_engine_instance() -> None:
    """计数必须从日志折出来：换一个进程接着跑，不能归零。"""
    log = MemoryEventLog()
    build_engine(log, FailingPolicy(times=1)).run(RUN, "测试任务", 5)

    fresh = build_engine(log, FailingPolicy(times=0))
    fresh.resume(RUN)

    view = replay(log.read(RUN))
    assert view.decision_failures == 0  # 成功决策后清零
    assert [item.type for item in log.read(RUN)].count(EventType.DECISION_FAILED) == 1


# --- 策略层：错误分类 -----------------------------------------------------


class BrokenClient:
    """按配置抛出客户端错误或返回坏输出。"""

    def __init__(self, *, retryable: bool = True, text: str | None = None) -> None:
        self._retryable = retryable
        self._text = text

    def complete(self, prompt: str) -> str:
        """抛错或返回固定文本。"""
        if self._text is not None:
            return self._text
        raise ModelClientError("上游 503", retryable=self._retryable)


def test_model_policy_marks_client_error_retryable() -> None:
    policy = ModelPolicy(BrokenClient(), ())
    with pytest.raises(PolicyError) as info:
        policy.decide(replay([started()]))
    assert info.value.retryable is True


def test_model_policy_marks_unusable_output_retryable() -> None:
    policy = ModelPolicy(BrokenClient(text="我觉得应该转账"), ())
    with pytest.raises(PolicyError) as info:
        policy.decide(replay([started()]))
    assert info.value.retryable is True


def test_policy_error_defaults_to_non_retryable() -> None:
    assert PolicyError("配置错").retryable is False


# --- CLI：看得见 ----------------------------------------------------------


def test_status_reports_decision_failure(tmp_path: Any, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "events.db"
    log = SqliteEventLog(db)
    build_engine(log, FailingPolicy(times=1)).run(RUN, "测试任务", 5)

    assert cli.main(["status", "--run-id", RUN, "--db", str(db)]) == 0
    printed = capsys.readouterr().out
    assert "决策失败" in printed
    assert "1 次" in printed
