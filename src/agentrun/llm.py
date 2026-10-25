"""模型适配器：把模型输出转成 ``Action``。

位置：``policy`` 的一种实现，夹在「模型服务」与「引擎」之间。它只做三件事：
把视图渲染成提示、调用注入的客户端、把文本严格解析成动作。

它不联网、不重试、不缓存、不记游标：**视图是唯一输入**，同一份视图必然得到同一份
提示；解析失败一律抛 ``PolicyError``——决策失败，一个工具都没调用。

依赖方向：
    llm → policy / model
    llm 不认识工具实现、日志、引擎；它只知道工具的名字与一句说明。
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .model import Outcome, RunView, StepView
from .policy import Action, Finish, PolicyError, ToolCall


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """提示里描述一个可用工具。

    Attributes:
        name: 工具名，必须与引擎注册表里的一致；拼错不会被这里拦住，引擎会在执行
            前判为未知工具。
        description: 一句话说明，只写给模型看，不参与任何校验。
    """

    name: str
    description: str = ""


@runtime_checkable
class ModelClient(Protocol):
    """模型服务的最小协议：给一段提示，返回一段文本。

    Notes:
        实现方不应自己解析动作、不应访问日志或账本。它唯一的输出就是文本；文本是否
        符合约定由 ``parse_action`` 判断。
    """

    def complete(self, prompt: str) -> str:
        """返回模型对提示的原始输出。

        Args:
            prompt: 由 ``build_prompt`` 渲染的完整提示。

        Returns:
            str: 原始文本，允许包含换行。不做任何修剪，解析器会自己 ``strip``。

        Raises:
            Exception: 网络、鉴权、超时等任何失败都可直接抛出；``ModelPolicy`` 会把
                它转成 ``PolicyError``（决策失败，外部世界未改变）。
        """
        ...


def _json(value: Any) -> str:
    """把任意值渲染成单行 JSON，供提示展示使用。

    Notes:
        ``default=repr`` 只服务提示：``args`` / ``result`` 里出现不可 JSON 化的值时，
        宁可显示 ``<object at 0x...>`` 也不让渲染失败。真正要求可无损落盘的是事件，
        不是提示——两者的严格程度不同，不能互相替代。
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)


def _render_step(step: StepView) -> str:
    """渲染历史中的一步：``序号. 工具(参数) → 结果``。"""
    head = f"{step.step}. {step.tool}({_json(step.args)})"
    if step.outcome is Outcome.SUCCEEDED:
        return f"{head} → SUCCEEDED {_json(step.result or {})}"
    if step.outcome is Outcome.FAILED:
        return f"{head} → FAILED {_json(step.error or {})}"
    if step.outcome is Outcome.UNCERTAIN:
        return f"{head} → UNCERTAIN {step.reason or ''}"
    return f"{head} → {'已开始，没有结果' if step.attempts else '已决策，尚未开始'}"


def build_prompt(view: RunView, tools: Sequence[ToolSpec]) -> str:
    """把视图渲染成提示。

    Args:
        view: 当前视图；只读取，不修改。
        tools: 可用工具清单。按名字排序后渲染，调用方给的顺序不影响结果。

    Returns:
        str: 纯文本提示。同样的视图与同一组工具必然得到同一份字符串。

    Notes:
        提示里包含三样东西：任务与预算、已执行的步骤、可用工具与输出约定。没有第四
        样——时间、进程、随机数都不允许进入提示，否则续跑时同一视图会得到不同决策。

        工具名不排序以外的任何归一化都不做：大小写、空白原样保留。拼错的名字应由
        引擎报「未注册的工具」，而不是在这里被悄悄纠正成另一个工具。
    """
    lines = [
        "你是任务执行引擎的决策模块。根据下面的状态选择下一步：调用一个工具，或结束任务。",
        "",
        f"任务：{view.task}",
        f"已决策 {len(view.steps)} 步，预算 {view.max_steps} 步，"
        f"剩余 {max(view.max_steps - len(view.steps), 0)} 步。",
        "",
        "已执行的步骤：",
    ]
    if view.steps:
        lines.extend(_render_step(step) for step in view.steps)
    else:
        lines.append("（还没有执行任何步骤。）")

    lines.extend(["", "可用工具："])
    ordered = sorted(tools, key=lambda spec: spec.name)
    if ordered:
        lines.extend(
            f"- {spec.name}：{spec.description}" if spec.description else f"- {spec.name}"
            for spec in ordered
        )
    else:
        lines.append("（没有可用工具，只能结束任务。）")

    lines.extend(
        [
            "",
            "输出要求：",
            "- 只输出一个 JSON 对象，不要代码块、不要解释、不要多余字段。",
            '- 调用工具：{"tool": "<工具名>", "args": {...}}',
            '- 结束任务：{"finish": "<结论>"}',
        ]
    )
    return "\n".join(lines)


def _excerpt(text: str, limit: int = 80) -> str:
    """把模型输出压成一行短摘要，用于失败原因。

    Notes:
        失败原因会进入 ``RUN_FAILED`` 事件并长期留在日志里，不能原样塞入整段输出。
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


def parse_action(text: str) -> Action:
    """把模型输出解析成动作。

    Args:
        text: 模型返回的原始文本。

    Returns:
        Action: ``ToolCall`` 或 ``Finish``。

    Raises:
        PolicyError: 输出不是字符串、不是合法 JSON、不是对象、给了未知字段、同时给了
            ``tool`` 与 ``finish``、两者都没给、工具名不是非空字符串、``args`` 不是
            映射、``finish`` 不是非空字符串。

    Notes:
        解析是严格的，不做这几件事：

        1. 不剥 Markdown 代码块、不截取第一个 ``{`` 到最后一个 ``}``：容错会让「模型
           没按约定输出」变成「有时能跑」，而这种差别在日志里看不出来。
        2. 不把 ``finish`` 的非字符串值转成字符串：``250`` 是参数，不是结论。
        3. 不校验工具是否存在：那是引擎的事，只有注册表知道答案。
    """
    if not isinstance(text, str):
        raise PolicyError(f"模型输出必须是字符串，收到 {type(text).__name__}")
    raw = text.strip()
    if not raw:
        raise PolicyError("模型输出为空")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"模型输出不是合法 JSON：{exc.msg}：{_excerpt(raw)}") from exc

    if not isinstance(data, dict):
        raise PolicyError(f"模型输出必须是 JSON 对象，收到 {type(data).__name__}")

    if "tool" in data and "finish" in data:
        raise PolicyError("模型输出不能同时给出 tool 与 finish")
    if "tool" not in data and "finish" not in data:
        raise PolicyError("模型输出必须给出 tool 或 finish")

    # args 只在调用工具时合法；结束任务时多给 args 说明模型没读懂约定
    allowed = {"tool", "args"} if "tool" in data else {"finish"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise PolicyError(f"模型输出含未知字段：{unknown}")

    if "tool" in data:
        tool = data["tool"]
        if not isinstance(tool, str) or not tool.strip():
            raise PolicyError("模型输出的 tool 必须是非空字符串")
        args = data.get("args", {})
        if not isinstance(args, Mapping):
            raise PolicyError("模型输出的 args 必须是 JSON 对象")
        return ToolCall(tool, dict(args))

    answer = data["finish"]
    if not isinstance(answer, str) or not answer.strip():
        raise PolicyError("模型输出的 finish 必须是非空字符串")
    return Finish(answer)


class ModelPolicy:
    """用模型输出决策；视图是唯一输入。

    Args:
        client: 模型客户端，只要求 ``complete(prompt) -> str``。
        tools: 可用工具清单，渲染进提示。必须与引擎注册表保持一致，否则模型会对着
            不存在的工具做决策。

    Notes:
        不缓存、不记游标：每次 ``decide`` 都重新渲染提示。这保证「同一个视图 → 同一份
        提示」，续跑才有意义；但模型本身是否对同一提示给出同一答案是模型侧的性质，
        引擎无法保证——温度为 0 是使用前提，不是本模块能验证的事实。
    """

    def __init__(self, client: ModelClient, tools: Sequence[ToolSpec]) -> None:
        self._client = client
        self._tools = tuple(tools)

    def decide(self, view: RunView) -> Action:
        """渲染提示、调用模型、解析成动作。

        Args:
            view: 当前视图。

        Returns:
            Action: 模型给出的下一个动作。

        Raises:
            PolicyError: 模型调用失败或输出不符合约定。此刻没有调用任何工具，外部
                世界的状态与决策前完全相同。

        Notes:
            客户端异常不在这里分类：无论是超时还是鉴权失败，对引擎而言都只是「这次
            没拿到决策」，一律归为 ``PolicyError``，不写 ``UNCERTAIN``。后者意味着
            「可能已经改了」，与这里恰好相反。
        """
        prompt = build_prompt(view, self._tools)
        try:
            text = self._client.complete(prompt)
        except Exception as exc:
            raise PolicyError(f"模型调用失败：{exc}") from exc
        return parse_action(text)
