"""对账与人工复核：给「已经开始、没有结果」的那一步找到结论。

这个模块只回答一个问题：那笔不知道有没有生效的调用，外部世界里到底留下了痕迹吗？

三条走向，证据强度依次降低：

1. 查到记录 → 直接补记成功。
2. 查不到 → 提交人工复核。**不能**据此断言未生效，也不能自动重做。
3. 人工给出结论 → ``applied`` 补记、``not_applied`` 允许重做、``abort`` 终止任务。

它不执行原本的写操作、不重试、不猜测，也不替人做决定。

依赖方向：
    reconcile → {journal, model, tools, events}
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .events import Event, EventType
from .journal import EventLog
from .model import REVIEW_DECISIONS, Outcome, RunStatus, RunView, replay
from .tools import Tool, ToolContext, ToolStatus, run_tool


class NothingToReconcile(Exception):
    """没有处于「已开始、无结果」状态的步骤，无需对账。

    抛出它表示调用方对账的时机不对，而不是对账失败。终态任务与等待复核的任务都
    属于「时机不对」：前者已经结束，后者需要的是 ``resolve``。
    """


class NothingToResolve(Exception):
    """没有待复核的步骤：任务不在 ``WAITING_REVIEW``，或结论已经写过。

    抛出它表示调用方 ``resolve`` 的时机不对。重复复核不会覆盖第一次的结论。
    """


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """一次对账尝试的结果。

    Attributes:
        recorded: 是否补记了成功结果。
        event: 本次追加的事件（``STEP_COMPLETED`` 或 ``REVIEW_REQUESTED``）；
            什么都没写时为 ``None``。
        detail: 给人看的原因说明，可直接打印。
        requested: 是否提交了人工复核。``recorded=False and requested=True`` 表示
            自动对账无法定论，任务已停在 ``WAITING_REVIEW``。
    """

    recorded: bool
    event: Event | None
    detail: str
    requested: bool = False


def pending_step(view: RunView) -> int | None:
    """找出第一个「已开始但结论未定」的步骤。

    Args:
        view: 从日志重放得到的视图。

    Returns:
        int | None: 步骤号（从 1 开始）；没有这样的步骤时返回 ``None``。

    Notes:
        「结论未定」包含两种：``outcome is None``（结果事件没来得及写）与
        ``outcome is UNCERTAIN``（写了「不知道」，但还没收敛）。两者都要对账或复核，
        区别只在日志里留下的证据，不在下一步该做什么。

        正常情况下最多只有一个：引擎串行执行，一次只会有一笔调用悬在半空。
        同时出现多个说明日志被外部改过，此时处理第一个仍然安全，其余留给下一次调用。
    """
    for step in view.steps:
        if step.attempts > 0 and (step.outcome is None or step.outcome is Outcome.UNCERTAIN):
            return step.step
    return None


def reconcile(log: EventLog, run_id: str, lookup: Tool) -> ReconcileResult:
    """查询外部世界，并据此为待对账的那一步补记结论或提交复核。

    Args:
        log: 事件日志；补记或复核请求会追加到它。
        run_id: 任务标识。
        lookup: 只读查询工具，接收 ``{"request_id": step_key}``，返回含 ``found``
            的映射。通常就是 ``ledger_tools.LookupRequest``。

    Returns:
        ReconcileResult: 是否补记、是否提交复核、追加了哪条事件、原因说明。

    Raises:
        NothingToReconcile: 日志为空、任务不在 ``RUNNING``、或没有待对账的步骤。

    Notes:
        补记的 ``STEP_COMPLETED`` 携带的是**账本记录**，不是原工具的返回值——后者随
        进程一起丢了。这是对账的语义：用外部世界的事实回填日志，而不是重放一次调用。

        查不到时追加 ``REVIEW_REQUESTED``，任务进入 ``WAITING_REVIEW``。这是与第 6 轮
        的差别：以前查不到就什么也不写，把「不知道」留在原地；现在把它升级成「需要人
        给结论」，因为自动手段已经用尽，而继续等待不会有新信息。
    """
    events = log.read(run_id)
    if not events:
        raise NothingToReconcile(f"没有可对账的任务：{run_id!r}")

    view = replay(events)
    if view.status is RunStatus.WAITING_REVIEW:
        raise NothingToReconcile(f"{run_id!r} 正在等待人工复核：先 resolve，不要再自动对账")
    if view.status is not RunStatus.RUNNING:
        raise NothingToReconcile(f"{run_id!r} 已是终态 {view.status}，无需对账")

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
        reason = f"账本查不到 {target.step_key}，自动对账无法确认"
        event = log.append(
            run_id,
            EventType.REVIEW_REQUESTED,
            {"step": step, "reason": reason},
        )
        return ReconcileResult(
            recorded=False,
            event=event,
            detail=f"{reason}；已提交人工复核",
            requested=True,
        )

    event = log.append(
        run_id,
        EventType.STEP_COMPLETED,
        {"step": step, "result": dict(value)},
    )
    return ReconcileResult(recorded=True, event=event, detail=f"已补记 step {step} 为成功")


def resolve(
    log: EventLog,
    run_id: str,
    *,
    decision: str,
    note: str,
    result: Mapping[str, Any] | None = None,
) -> RunView:
    """写下人工复核结论，返回复核后的视图。

    Args:
        log: 事件日志；结论会追加到它。
        run_id: 任务标识。
        decision: ``applied`` / ``not_applied`` / ``abort``，见 ``REVIEW_DECISIONS``。
        note: 复核依据：谁、看了什么证据、为什么这样判断。去空白后不得为空。
        result: 仅 ``applied`` 需要，记录查实的外部事实（通常来自账本）。

    Returns:
        RunView: 写入结论之后的视图。``applied`` / ``not_applied`` 回到 ``RUNNING``，
        ``abort`` 进入终态 ``ABORTED``。

    Raises:
        ValueError: 结论不在允许集合、依据为空、或 ``result`` 与结论不匹配。
        NothingToResolve: 任务不在 ``WAITING_REVIEW``，或结论已经写过。

    Notes:
        ``not_applied`` 只是记录「这一步确实没生效」，并不自动重做；是否重做由策略在
        下一次决策里决定，重做会是一个新步骤、新的 ``step_key``。同一步骤的 ``step_key``
        不能复用——那会把「第二次调用」伪装成「第一次的重试」。

        ``abort`` 不改变该步的 ``outcome``：没有证据就是没有证据，终止状态本身已经
        表达了「不再继续」。
    """
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"未知的复核结论：{decision!r}，可选 {REVIEW_DECISIONS}")
    if not note.strip():
        raise ValueError("复核依据不能为空")
    if decision == "applied":
        if not isinstance(result, Mapping):
            raise ValueError("applied 必须提供 result（查实的外部事实）")
        payload_result = dict(result)
    elif result is not None:
        raise ValueError("只有 applied 可以携带 result")
    else:
        payload_result = None

    events = log.read(run_id)
    if not events:
        raise NothingToResolve(f"没有这个任务：{run_id!r}")
    view = replay(events)
    if view.status is not RunStatus.WAITING_REVIEW or view.review is None:
        raise NothingToResolve(f"{run_id!r} 没有待复核的步骤（当前状态 {view.status}）")

    payload: dict[str, Any] = {
        "step": view.review.step,
        "decision": decision,
        "note": note,
    }
    if payload_result is not None:
        payload["result"] = payload_result
    log.append(run_id, EventType.REVIEW_RESOLVED, payload)
    return replay(log.read(run_id))
