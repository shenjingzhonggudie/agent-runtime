"""任务驱动器：把策略、工具与事件日志串成一个循环。

它只做三件事：按契约顺序追加事件、调用策略与工具、决定何时停止。
它不实现工具、不实现策略、不做持久化事务、不做对账。

依赖方向：
    engine → {policy, tools, journal, model, events}
"""

from dataclasses import dataclass
from enum import StrEnum

from .events import EventType, make_step_key
from .journal import EventLog
from .model import RunView, replay
from .policy import Finish, Policy
from .tools import ToolRegistry, ToolStatus, UnknownTool, run_tool


class StopReason(StrEnum):
    """本次 ``Engine.run`` 为什么停下来。

    注意它与 ``RunStatus`` 不是一回事：遇到 UNCERTAIN 时循环停了，但任务状态仍是
    RUNNING——事实不足，既不成功也不失败，等对账或人工判断（第 6 轮）。
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXHAUSTED = "EXHAUSTED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class EngineResult:
    """一次运行的结果。

    Attributes:
        view: 停止时的视图，由日志重放得到。
        stop_reason: 停止原因。
    """

    view: RunView
    stop_reason: StopReason


class Engine:
    """串行驱动一次任务运行。

    Args:
        policy: 决策来源。
        registry: 工具注册表。
        log: 事件日志（v0 为内存实现）。
    """

    def __init__(self, policy: Policy, registry: ToolRegistry, log: EventLog) -> None:
        self._policy = policy
        self._registry = registry
        self._log = log

    def _view(self, run_id: str) -> RunView:
        """从日志重放当前视图。

        Notes:
            每次都用 ``replay`` 而不是增量 ``apply``：v0 的步数很小，用“事实重建”
            可以让一致性无可争辩——视图永远等于日志的折叠结果。
        """
        return replay(self._log.read(run_id))

    def run(self, run_id: str, task: str, max_steps: int) -> EngineResult:
        """执行一次任务，直到成功、失败、耗尽或结果未知。

        Args:
            run_id: 任务标识，由调用方生成并保持稳定。
            task: 任务描述，写入 RUN_STARTED。
            max_steps: 最大决策步数（按 STEP_DECIDED 计数，重试不额外消耗）。

        Returns:
            EngineResult: 停止时的视图与原因。

        Raises:
            InvalidEvent: 事件序列非法（属于编程错误，不应在正常运行中出现）。

        Notes:
            每轮顺序（不可调换）：

            1. 已决策步数 ``len(view.steps) >= max_steps`` → 追加 RUN_EXHAUSTED，
               停止，原因 EXHAUSTED。**预算判断在问策略之前**，所以 ``max_steps=0``
               一次决策都不做。
            2. 调 ``policy.decide(view)``。返回 ``Finish`` → 追加 RUN_SUCCEEDED，
               停止，原因 SUCCEEDED。
            3. 返回 ``ToolCall`` → 从注册表取工具；``UnknownTool`` → 追加
               RUN_FAILED，停止，原因 FAILED（配置错误，不是业务失败）。
            4. 追加 STEP_DECIDED（``step = len(view.steps) + 1``，
               ``step_key = make_step_key(run_id, step)``）。
            5. 追加 STEP_STARTED（``attempt = 1``）。**顺序不能反**：先记决策，再记
               意图，最后才允许碰外部世界。
            6. 调 ``run_tool``，按结果追加 STEP_COMPLETED / STEP_FAILED /
               STEP_UNCERTAIN。
            7. 结果是 UNCERTAIN → 立即停止，原因 UNCERTAIN，视图保持 RUNNING，
               不再追加任何事件。其他结果回到第 1 步。

            实现时需要的导入：``EventType``、``apply`` 不需要；需要
            ``from .events import EventType``、``from .model import apply, replay``
            中的 replay 已在模块顶部导入；还需要
            ``from .policy import Finish, ToolCall``、``from .tools import
            ToolStatus, UnknownTool, run_tool``、``from .events import
            make_step_key``。按需增减，但不要让工具分类逻辑泄漏到这里——它已经在
            ``run_tool`` 里做完了。
        """
        self._log.append(run_id, EventType.RUN_STARTED, {"task": task, "max_steps": max_steps})
        while True:
            view = self._view(run_id)
            if len(view.steps) >= max_steps:
                self._log.append(run_id, EventType.RUN_EXHAUSTED, {"max_steps": max_steps})
                return EngineResult(view=self._view(run_id), stop_reason=StopReason.EXHAUSTED)
            action = self._policy.decide(view)
            if isinstance(action, Finish):
                self._log.append(run_id, EventType.RUN_SUCCEEDED, {"answer": action.answer})
                return EngineResult(view=self._view(run_id), stop_reason=StopReason.SUCCEEDED)
            try:
                tool = self._registry.get(action.tool)
            except UnknownTool:
                self._log.append(
                    run_id, EventType.RUN_FAILED, {"reason": f"未注册的工具：{action.tool!r}"}
                )
                return EngineResult(view=self._view(run_id), stop_reason=StopReason.FAILED)

            step = len(view.steps) + 1
            self._log.append(
                run_id,
                EventType.STEP_DECIDED,
                {
                    "step": step,
                    "tool": action.tool,
                    "args": dict(action.args),
                    "step_key": make_step_key(run_id, step),
                },
            )
            self._log.append(run_id, EventType.STEP_STARTED, {"step": step, "attempt": 1})

            outcome = run_tool(tool, action.args)
            if outcome.status == ToolStatus.SUCCEEDED:
                self._log.append(
                    run_id,
                    EventType.STEP_COMPLETED,
                    {"step": step, "result": dict(outcome.value or {})},
                )
            elif outcome.status == ToolStatus.FAILED:
                self._log.append(
                    run_id,
                    EventType.STEP_FAILED,
                    {"step": step, "error": dict(outcome.error or {})},
                )
            else:
                self._log.append(
                    run_id, EventType.STEP_UNCERTAIN, {"step": step, "reason": outcome.reason or ""}
                )
                return EngineResult(view=self._view(run_id), stop_reason=StopReason.UNCERTAIN)
