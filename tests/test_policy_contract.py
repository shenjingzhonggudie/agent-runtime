"""策略契约测试：决策只能来自视图，不能来自内部状态。"""

import pytest

from agentrun.model import RunStatus, RunView, StepView
from agentrun.policy import Finish, PolicyError, ScriptedPolicy, ScriptExhausted, ToolCall


def view_with_steps(count: int) -> RunView:
    steps = tuple(
        StepView(step=index + 1, tool="fake", args={}, step_key=f"run-1:{index + 1}")
        for index in range(count)
    )
    return RunView(
        run_id="run-1",
        task="测试任务",
        max_steps=9,
        status=RunStatus.RUNNING,
        last_seq=max(1, count * 3),
        steps=steps,
    )


def test_decide_returns_action_matching_step_count() -> None:
    script = [ToolCall("get_balance", {"owner": "alice"}), ToolCall("transfer", {}), Finish("完成")]
    policy = ScriptedPolicy(script)

    assert policy.decide(view_with_steps(0)) == script[0]
    assert policy.decide(view_with_steps(1)) == script[1]
    assert policy.decide(view_with_steps(2)) == script[2]


def test_same_view_gives_same_action() -> None:
    policy = ScriptedPolicy([ToolCall("a", {}), ToolCall("b", {})])
    view = view_with_steps(0)

    # 连续两次用同一个视图决策，必须得到同一个动作：策略里没有游标
    assert policy.decide(view) == policy.decide(view)


def test_script_exhausted_raises() -> None:
    policy = ScriptedPolicy([Finish("完成")])
    with pytest.raises(ScriptExhausted):
        policy.decide(view_with_steps(1))


def test_finish_rejects_blank_answer() -> None:
    with pytest.raises(ValueError):
        Finish("   ")


def test_script_exhausted_is_a_policy_error() -> None:
    # 脚本用完与模型乱输出是同一件事：策略给不出动作，都必须走 RUN_FAILED
    assert issubclass(ScriptExhausted, PolicyError)
