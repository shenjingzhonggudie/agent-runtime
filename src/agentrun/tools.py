"""工具协议与单次执行边界。

这个模块只回答三个问题：

1. 一个工具长什么样（名字、副作用类别、参数校验、执行）。
2. 一次调用到底属于哪种结果——**做了一次 / 确定没做 / 不知道**。
3. 工具名怎么找、重名怎么办。

它不写事件、不分配 ``seq``、不重试、不判断预算、不做对账，也不认识任何具体业务
字段（没有 amount、没有 owner）。

依赖方向：
    engine → tools
    tools 不依赖 model / events / journal
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class SideEffect(StrEnum):
    """工具的副作用类别，决定“这个调用能不能重做”。

    Attributes:
        READ_ONLY: 只读。重复调用不改变外部世界，可用于查询、对账。
        IDEMPOTENT: 有副作用，但同一幂等键重复调用只有一次效果。重试相对安全，
            前提是工具端真的实现了原子去重，而不是只在文档里写“支持幂等”。
        NON_IDEMPOTENT: 有副作用且不去重。重复调用会重复生效，结果未知时
            **不得自动重做**。
    """

    READ_ONLY = "READ_ONLY"
    IDEMPOTENT = "IDEMPOTENT"
    NON_IDEMPOTENT = "NON_IDEMPOTENT"


class ToolError(Exception):
    """工具相关错误的基类，便于调用方统一捕获。"""


class InvalidArgs(ToolError):
    """参数不合法，工具**尚未被调用**。

    抛出它必须是零副作用的：校验阶段不碰任何外部状态。
    """


class ToolRejected(ToolError):
    """工具已经被调用，但在业务上明确拒绝，且确定没有产生任何效果。"""


class ToolCrashed(ToolError):
    """工具已经被调用，但执行过程中崩溃，**无法确认是否产生效果**。

    它和 ``ToolRejected`` 的区别就是整个项目存在的理由：前者可以重做，后者不行。
    """


class RegistryError(Exception):
    """注册表相关错误的基类：重名、找不到。"""


class DuplicateTool(RegistryError):
    """同名工具被注册了两次；后者不会覆盖前者。"""


class UnknownTool(RegistryError):
    """按名字取工具时没有找到。"""


class ToolStatus(StrEnum):
    """单次调用的结果类别，与 ``model.Outcome`` 取值相同但是独立类型。

    tools 不依赖 model；由 engine 负责把两者对应起来。
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """一次工具调用的结果。

    Attributes:
        status: 结果类别。
        value: 成功时的返回值，必须是可 JSON 序列化的映射；其他情况为 None。
        error: 失败时的错误描述，约定含 ``kind`` 与 ``message``；其他情况为 None。
        reason: 结果未知时的说明；其他情况为 None。
    """

    status: ToolStatus
    value: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    reason: str | None = None


@runtime_checkable
class Tool(Protocol):
    """工具必须满足的协议。

    Attributes:
        name: 唯一名字，用于日志和注册表。
        side_effect: 副作用类别。

    Notes:
        ``validate`` 与 ``run`` 都必须接受同一个 ``args`` 映射。校验阶段不得产生
        任何副作用；执行阶段只执行一次，不自行重试。
    """

    name: str
    side_effect: SideEffect

    def validate(self, args: Mapping[str, Any]) -> None:
        """校验参数；不合法时抛 ``InvalidArgs``，且不得产生副作用。"""
        ...

    def run(self, args: Mapping[str, Any]) -> Mapping[str, Any]:
        """执行一次工具调用，返回可序列化的结果。

        Raises:
            ToolRejected: 业务拒绝，确定未生效。
            ToolCrashed: 执行中崩溃，效果未知。
        """
        ...


class ToolRegistry:
    """按名字保存工具。重名与未知名字都必须显式失败，不能静默覆盖或返回 None。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具。

        Args:
            tool: 满足 ``Tool`` 协议的对象。

        Raises:
            DuplicateTool: 已有同名工具；原工具保持不变。
        """
        if tool.name in self._tools:
            raise DuplicateTool(f"工具名重复：{tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """按名字取工具。

        Raises:
            UnknownTool: 名字未注册。
        """
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownTool(f"未注册的工具：{name!r}") from None

    def names(self) -> tuple[str, ...]:
        """返回已注册的工具名，按字典序排列，便于稳定展示与测试。"""
        return tuple(sorted(self._tools))


def run_tool(tool: Tool, args: Mapping[str, Any]) -> ToolOutcome:
    """校验并执行一次工具调用，把结果归类成三种之一。

    Args:
        tool: 目标工具。
        args: 调用参数。

    Returns:
        ToolOutcome: 三种结果之一。成功时带 ``value``；明确失败时带 ``error``
            （含 ``kind``）；未知时带 ``reason``。

    Raises:
        Exception: 只有 ``KeyboardInterrupt`` 之类的非 ``Exception`` 才允许逃出；
            所有 ``Exception`` 都必须被归类，不能让调用方裸收到。

    Notes:
        分类规则（本轮契约）：

        - ``args`` 不是映射 → FAILED，``error["kind"] == "invalid_args"``，
          **既不校验也不执行**。
        - ``validate`` 抛任何 ``Exception`` → FAILED，**不得调用 run**。参数不合法
          时 ``kind == "invalid_args"``；校验自身崩了也归 FAILED，因为此时工具
          **一定没有执行过**——分类看的是“有没有调用过”，不是“异常看起来多吓人”。
        - ``run`` 返回映射 → SUCCEEDED。
        - ``run`` 抛 ``ToolRejected`` → FAILED，``kind == "rejected"``。
        - ``run`` 抛 ``ToolCrashed`` 或任何其他 ``Exception`` → UNCERTAIN，
          ``reason`` 说明原因。**不能**归为 FAILED：没收到明确拒绝，就无法证明
          外部世界没有发生变化。

        本函数只执行一次，不重试、不捕获 ``BaseException``。
    """
    if not isinstance(args, Mapping):
        return ToolOutcome(
            status=ToolStatus.FAILED,
            error={"kind": "invalid_args", "message": "args must be a mapping"},
        )

    try:
        tool.validate(args)
    except Exception as e:
        return ToolOutcome(
            status=ToolStatus.FAILED, error={"kind": "invalid_args", "message": str(e)}
        )

    try:
        result = tool.run(args)
        return ToolOutcome(status=ToolStatus.SUCCEEDED, value=result)
    except ToolRejected as e:
        return ToolOutcome(status=ToolStatus.FAILED, error={"kind": "rejected", "message": str(e)})
    except ToolCrashed as e:
        return ToolOutcome(status=ToolStatus.UNCERTAIN, reason=str(e))
    except Exception as e:
        return ToolOutcome(status=ToolStatus.UNCERTAIN, reason=str(e))
