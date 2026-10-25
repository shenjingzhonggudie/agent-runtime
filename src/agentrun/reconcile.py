"""对账：给「已经开始、没有结果」的那一步补记结论。

这个模块只回答一个问题：那笔不知道有没有生效的调用，外部世界里到底留下了痕迹吗？

它不做判断之外的任何事：不执行原本的写操作，不重试，不猜测。查到了就补记成功，
查不到就什么也不写——``found=False`` 只说明「此刻查不到」，不构成「没有生效」的证据。

依赖方向：
    reconcile → {journal, model, tools, events}
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .events import Event, EventType
from .journal import EventLog
from .model import RunView, replay
from .tools import Tool, ToolContext, ToolStatus, run_tool


class NothingToReconcile(Exception):
    """没有处于「已开始、无结果」状态的步骤，无需对账。

    抛出它表示调用方对账的时机不对，而不是对账失败。
    """


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """一次对账尝试的结果。

    Attributes:
        recorded: 是否补记了事件。``False`` 表示外部世界没有给出足够证据。
        event: 补记的事件；没有补记时为 ``None``。
        detail: 给人看的原因说明，可直接打印。
    """

    recorded: bool
    event: Event | None
    detail: str


def pending_step(view: RunView) -> int | None:
    """找出第一个「已开始但没有结果」的步骤。

    Args:
        view: 从日志重放得到的视图。

    Returns:
        int | None: 步骤号（从 1 开始）；没有这样的步骤时返回 ``None``。

    Notes:
        正常情况下最多只有一个：引擎串行执行，一次只会有一笔调用悬在半空。
        同时出现多个说明日志被外部改过，此时补第一个仍然安全，其余留给下一次调用。
    """
    for step in view.steps:
        if step.attempts > 0 and step.outcome is None:
            return step.step
    return None


def reconcile(log: EventLog, run_id: str, lookup: Tool) -> ReconcileResult:
    """查询外部世界，并据此为待对账的那一步补记结论。

    Args:
        log: 事件日志；补记会追加到它。
        run_id: 任务标识。
        lookup: 只读查询工具，接收 ``{"request_id": step_key}``，返回含 ``found``
            的映射。通常就是 ``ledger_tools.LookupRequest``。

    Returns:
        ReconcileResult: 是否补记、补记了哪条事件、以及原因说明。

    Raises:
        NothingToReconcile: 日志为空，或没有待对账的步骤。

    Notes:
        补记的 ``STEP_COMPLETED`` 携带的是**账本记录**，不是原工具的返回值——后者随
        进程一起丢了。这是对账的语义：用外部世界的事实回填日志，而不是重放一次调用。

        查询结果的三种走向：

        - 查到（``found`` 为 ``True``）→ 追加 ``STEP_COMPLETED``，该步变为成功。
        - 查不到 → **不写任何事件**。查不到只说明此刻查不到，不能据此断言未生效；
          保持 ``UNCERTAIN``，由人继续查。
        - 查询本身失败 → 同样不写，把原因带回给调用方。
    """
    events = log.read(run_id)
    if not events:
        raise NothingToReconcile(f"没有可对账的任务：{run_id!r}")

    view = replay(events)
    step = pending_step(view)
    if step is None:
        raise NothingToReconcile(f"{run_id!r} 没有待对账的步骤")

    target = view.steps[step - 1]
    ctx = ToolContext(
        run_id=run_id,
        step=step,
        attempt=target.attempts,
        step_key=target.step_key,
    )
    outcome = run_tool(lookup, {"request_id": target.step_key}, ctx)
    if outcome.status is not ToolStatus.SUCCEEDED:
        reason = outcome.reason or str(outcome.error or {})
        return ReconcileResult(recorded=False, event=None, detail=f"对账查询失败：{reason}")

    value: Mapping[str, Any] = outcome.value or {}
    if value.get("found") is not True:
        return ReconcileResult(
            recorded=False,
            event=None,
            detail=f"账本查不到 {target.step_key}，无法确认是否生效；保持 UNCERTAIN",
        )

    event = log.append(
        run_id,
        EventType.STEP_COMPLETED,
        {"step": step, "result": dict(value)},
    )
    return ReconcileResult(recorded=True, event=event, detail=f"已补记 step {step} 为成功")
