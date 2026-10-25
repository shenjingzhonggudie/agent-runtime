"""模型策略契约：提示只由视图决定，解析失败即决策失败。"""

import pytest

from agentrun.llm import ModelPolicy, ToolSpec, build_prompt, parse_action
from agentrun.model import Outcome, RunStatus, RunView, StepView
from agentrun.policy import Finish, PolicyError, ToolCall

TOOLS = [
    ToolSpec("transfer", "转账，不可重复"),
    ToolSpec("get_balance", "读取账户余额"),
]


def view(*steps: StepView, task: str = "转账 250", max_steps: int = 10) -> RunView:
    return RunView(
        run_id="run-1",
        task=task,
        max_steps=max_steps,
        status=RunStatus.RUNNING,
        last_seq=max(1, len(steps) * 3),
        steps=steps,
    )


def step(number: int, **changes: object) -> StepView:
    base: dict[str, object] = {
        "step": number,
        "tool": "get_balance",
        "args": {"owner": "alice"},
        "step_key": f"run-1:{number}",
    }
    base.update(changes)
    return StepView(**base)  # type: ignore[arg-type]


class OneShotModel:
    """返回固定文本，并记下收到的每一份提示。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


class BrokenModel:
    """模拟模型服务不可用。"""

    def complete(self, prompt: str) -> str:
        raise TimeoutError("模型服务超时")


# --- 提示渲染 ------------------------------------------------------------


def test_prompt_contains_task_budget_tools_and_convention() -> None:
    prompt = build_prompt(view(), TOOLS)

    assert "任务：转账 250" in prompt
    assert "已决策 0 步，预算 10 步，剩余 10 步。" in prompt
    assert "（还没有执行任何步骤。）" in prompt
    assert "- get_balance：读取账户余额" in prompt
    assert '{"tool": "<工具名>", "args": {...}}' in prompt
    assert '{"finish": "<结论>"}' in prompt


def test_prompt_sorts_tools_so_caller_order_does_not_matter() -> None:
    assert build_prompt(view(), TOOLS) == build_prompt(view(), list(reversed(TOOLS)))


def test_prompt_renders_history_with_outcome_and_payload() -> None:
    prompt = build_prompt(
        view(
            step(
                1,
                tool="transfer",
                args={"src": "alice", "dst": "bob", "amount": 250},
                attempts=1,
                outcome=Outcome.SUCCEEDED,
                result={"ok": True},
            ),
            step(
                2,
                tool="transfer",
                attempts=1,
                outcome=Outcome.FAILED,
                error={"kind": "rejected", "message": "余额不足"},
            ),
            step(3, attempts=1, outcome=Outcome.UNCERTAIN, reason="连接中断"),
        ),
        TOOLS,
    )

    assert (
        '1. transfer({"amount": 250, "dst": "bob", "src": "alice"}) → SUCCEEDED {"ok": true}'
        in prompt
    )
    assert (
        '2. transfer({"owner": "alice"}) → FAILED {"kind": "rejected", "message": "余额不足"}'
        in prompt
    )
    assert '3. get_balance({"owner": "alice"}) → UNCERTAIN 连接中断' in prompt


def test_prompt_renders_unfinished_step_honestly() -> None:
    prompt = build_prompt(view(step(1), step(2, attempts=1)), TOOLS)

    assert "1. get_balance" in prompt
    assert "已决策，尚未开始" in prompt
    assert "已开始，没有结果" in prompt


def test_prompt_is_a_pure_function_of_view_and_tools() -> None:
    current = view(step(1, attempts=1, outcome=Outcome.SUCCEEDED, result={"ok": True}))

    assert build_prompt(current, TOOLS) == build_prompt(current, TOOLS)
    assert build_prompt(current, TOOLS) != build_prompt(view(), TOOLS)


def test_prompt_does_not_crash_on_unserializable_args() -> None:
    prompt = build_prompt(view(step(1, args={"blob": object()})), TOOLS)

    assert "blob" in prompt


def test_prompt_without_tools_says_so() -> None:
    assert "（没有可用工具，只能结束任务。）" in build_prompt(view(), [])


# --- 输出解析 ------------------------------------------------------------


def test_parse_tool_call() -> None:
    action = parse_action('{"tool": "get_balance", "args": {"owner": "alice"}}')

    assert action == ToolCall("get_balance", {"owner": "alice"})


def test_parse_tool_call_defaults_to_empty_args() -> None:
    assert parse_action('{"tool": "get_balance"}') == ToolCall("get_balance", {})


def test_parse_finish() -> None:
    assert parse_action('{"finish": "已转账 250"}') == Finish("已转账 250")


def test_parse_strips_surrounding_whitespace() -> None:
    assert parse_action('\n  {"finish": "完成"}\n') == Finish("完成")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "把 250 从 alice 转到 bob",
        "[]",
        '"done"',
        "null",
        "{}",
        '{"tool": ""}',
        '{"tool": 1}',
        '{"tool": "get_balance", "finish": "完成"}',
        '{"tool": "get_balance", "args": []}',
        '{"tool": "get_balance", "args": "owner=alice"}',
        '{"tool": "get_balance", "extra": 1}',
        '{"finish": ""}',
        '{"finish": 250}',
        '```json\n{"tool": "get_balance"}\n```',
    ],
)
def test_reject_invalid_output(text: str) -> None:
    with pytest.raises(PolicyError):
        parse_action(text)


def test_non_str_output_is_rejected() -> None:
    with pytest.raises(PolicyError):
        parse_action(b'{"tool": "get_balance"}')


# --- 策略本身 ------------------------------------------------------------


def test_model_policy_sends_rendered_prompt_and_returns_action() -> None:
    client = OneShotModel('{"tool": "get_balance", "args": {"owner": "alice"}}')
    policy = ModelPolicy(client, TOOLS)

    assert policy.decide(view()) == ToolCall("get_balance", {"owner": "alice"})
    assert client.prompts == [build_prompt(view(), TOOLS)]


def test_same_view_produces_same_prompt() -> None:
    client = OneShotModel('{"finish": "完成"}')
    policy = ModelPolicy(client, TOOLS)

    policy.decide(view())
    policy.decide(view())

    assert client.prompts[0] == client.prompts[1]


def test_client_failure_becomes_policy_error() -> None:
    with pytest.raises(PolicyError, match="模型服务超时"):
        ModelPolicy(BrokenModel(), TOOLS).decide(view())


def test_unparseable_output_becomes_policy_error() -> None:
    with pytest.raises(PolicyError):
        ModelPolicy(OneShotModel("转 250 给 bob"), TOOLS).decide(view())


def test_model_policy_does_not_check_registry() -> None:
    # 工具是否存在由引擎判定：这里放过，引擎会记「未注册的工具」并 RUN_FAILED
    client = OneShotModel('{"tool": "transfer_money"}')

    assert ModelPolicy(client, TOOLS).decide(view()) == ToolCall("transfer_money", {})
