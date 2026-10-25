"""策略：只读视图，返回下一个动作。

策略**不能**依赖任何没有落盘的状态（内部计数器、上一次调用的记忆）。恢复时视图
可以从事件重建，内部游标不能——这是 D18 的由来。

依赖方向：
    engine → policy → model
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .model import RunView


@dataclass(frozen=True, slots=True)
class ToolCall:
    """调用一个工具。

    Attributes:
        tool: 工具名，必须在注册表里存在。
        args: 调用参数，交给工具的 ``validate`` 校验。
    """

    tool: str
    args: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Finish:
    """结束任务并给出结论。

    Attributes:
        answer: 最终结论；去空白后不得为空。

    Raises:
        ValueError: 构造时 ``answer`` 为空白。放在构造处而不是引擎里，是为了让
            “空答案不算完成”在产生动作时就失败。
    """

    answer: str

    def __post_init__(self) -> None:
        if not self.answer.strip():
            raise ValueError("Finish.answer 不能为空白")


Action = ToolCall | Finish


class PolicyError(Exception):
    """策略无法给出下一个合法的动作。

    抛出它表示「这次决策失败了」，不是「工具失败了」：此时一个工具都还没被调用，
    外部世界没有发生任何改变。引擎收到它会写 ``RUN_FAILED``，而不是 ``UNCERTAIN``
    ——后者意味着「可能已经改了」，这里恰恰相反。
    """


class Policy(Protocol):
    """策略协议：给定当前视图，返回下一个动作。"""

    def decide(self, view: RunView) -> Action:
        """决定下一步。

        Args:
            view: 从事件重建的当前视图；策略只能依据它做决定。

        Returns:
            Action: 要么调用工具，要么结束任务。

        Raises:
            PolicyError: 策略无法给出合法动作。引擎据此写 ``RUN_FAILED``，而不是让
                进程带着半截状态崩掉。

        Notes:
            同一个视图必须给出同一个动作。策略不得修改 ``view``。
        """
        ...


class ScriptExhausted(PolicyError):
    """脚本已用完，但任务还没有结束。

    它同样是「策略给不出动作」，因此继承 ``PolicyError``：引擎记 ``RUN_FAILED``，
    不再让异常穿过主循环。脚本写短了是调用方的错误，不是引擎该猜测的情形。
    """


class ScriptedPolicy:
    """按视图中的步数选取脚本动作，不保存任何内部游标。

    Args:
        script: 动作序列。第 n 个动作对应视图里已有 n 步时的决策。
    """

    def __init__(self, script: Sequence[Action]) -> None:
        self._script = tuple(script)

    def decide(self, view: RunView) -> Action:
        """返回 ``script[len(view.steps)]``。

        Args:
            view: 当前视图。

        Returns:
            Action: 脚本中与当前步数对应的动作。

        Raises:
            ScriptExhausted: 步数已经超出脚本长度，说明脚本写得不够长。

        Notes:
            索引必须由 ``len(view.steps)`` 现算，不能存成 ``self._index``。前者在
            恢复后仍然正确，后者会从头再来。
        """
        index = len(view.steps)
        if index >= len(self._script):
            raise ScriptExhausted("脚本已用完")
        return self._script[index]
