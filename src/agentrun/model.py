"""运行视图与事件折叠。

``model`` 只依赖 ``events``：不做 I/O、不调用工具、不知道数据库和模型服务。
它的唯一职责是回答两个问题：这条事件现在合法吗？折叠之后的状态是什么？

依赖方向：
    engine → model → events
    journal → events
"""

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .events import Event, EventType


class RunStatus(StrEnum):
    """任务级状态。终态只有三种，且都不可再回到 RUNNING。"""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXHAUSTED = "EXHAUSTED"


class Outcome(StrEnum):
    """单步执行结果。

    UNCERTAIN 表示“无法确认是否生效”，与 FAILED（确定失败）必须分开：
    前者不能自动重做非幂等调用，后者可以。
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class InvalidEvent(Exception):
    """事件在当前位置不合法：字段缺失、类型不符、顺序错误或状态不允许。

    抛出它表示“这条事件不能进入日志”，而不是“工具执行失败”。
    """


@dataclass(frozen=True, slots=True)
class StepView:
    """一步的当前状态，完全由事件推出。

    Attributes:
        step: 从 1 开始的步骤序号。
        tool: 被选中的工具名。
        args: 决策时给出的参数，已与事件 payload 深拷贝隔离；观察者修改它不会污染
            日志，也不会影响重放结果。
        step_key: 跨重试稳定的调用键，见 ``events.make_step_key``。
        attempts: 已经开始的尝试次数；0 表示“已决策但尚未提交执行意图”。
        outcome: 执行结果；None 表示还没有终局结果（未开始或进行中）。
        result: 成功时的工具返回值；其他情况为 None。
        error: 失败时的错误信息；其他情况为 None。
        reason: 结果为 UNCERTAIN 时的说明（来自 STEP_UNCERTAIN 的 payload）；
            其他情况为 None。它记录“为什么无法确认”，不能当作错误原因使用。
    """

    step: int
    tool: str
    args: Mapping[str, Any]
    step_key: str
    attempts: int = 0
    outcome: Outcome | None = None
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RunView:
    """任务的当前状态，完全由事件推出；不含任何无法从事件重建的字段。

    Attributes:
        run_id: 任务标识。
        task: 任务描述，来自 RUN_STARTED。
        max_steps: 最大决策步数；预算由 engine 执行，model 只记录。
        status: 任务级状态。
        last_seq: 已折叠的最后一条事件的 seq；空视图为 0。下一条事件必须是
            ``last_seq + 1``。它不能用 ``steps[-1].step`` 代替：一步会产生多条事件，
            两个编号不同步。
        steps: 已决策的步骤，按 step 升序。
        answer: 成功时的最终答案；其他情况为 None。
    """

    run_id: str
    task: str
    max_steps: int
    status: RunStatus
    last_seq: int = 0
    steps: tuple[StepView, ...] = ()
    answer: str | None = None


def _field(event: Event, name: str, kind: type | tuple[type, ...]) -> Any:
    """读取 payload 字段并校验类型；缺失或类型不符统一转成 InvalidEvent。

    bool 是 int 的子类，但 ``step`` / ``max_steps`` 这类整数字段不接受 True/False。
    """
    try:
        value = event.payload[name]
    except KeyError, TypeError:
        raise InvalidEvent(f"{event.type} 缺少必需字段 {name!r}") from None
    if isinstance(value, bool) and kind is int:
        raise InvalidEvent(f"{event.type} 的字段 {name!r} 类型不符")
    if not isinstance(value, kind):
        raise InvalidEvent(f"{event.type} 的字段 {name!r} 类型不符")
    return value


def _step_at(view: RunView, step: int) -> StepView:
    """取已决策的第 ``step`` 步；不存在则抛 InvalidEvent。"""
    if not 1 <= step <= len(view.steps):
        raise InvalidEvent(f"步骤 {step} 尚未决策")
    return view.steps[step - 1]


def _with_step(view: RunView, seq: int, new_step: StepView) -> RunView:
    steps = view.steps[: new_step.step - 1] + (new_step,) + view.steps[new_step.step :]
    return replace(view, last_seq=seq, steps=steps)


def apply(view: RunView | None, event: Event) -> RunView:
    """把一条事件折叠进视图，返回新的视图；不修改传入的 view。

    Args:
        view: 当前视图；None 表示还没有任何事件。
        event: 待折叠的事件。

    Returns:
        RunView: 折叠后的新视图。原 view 及其中对象不得被修改。

    Raises:
        InvalidEvent: 出现下列任一情况时抛出，且不得返回部分更新：
            首条事件不是 RUN_STARTED；``seq`` 不等于上一条 +1（首条必须为 1）；
            ``max_steps`` 为负；payload 缺少必需字段或类型不符；在终态之后追加
            事件；``step`` 与已有步骤不连续；``STEP_STARTED`` 的 ``attempt``
            不等于 ``attempts + 1``；结果事件之前没有对应的 ``STEP_STARTED``；
            同一步骤出现第二个结果；``RUN_SUCCEEDED`` 的 answer 去空白后为空。

    Notes:
        预算耗尽（RUN_EXHAUSTED）不是成功，也不能写入 answer；它和 RUN_FAILED
        一样是独立的终态。工具执行与否由 engine 决定，model 只判断事件形状与顺序。
    """
    if view is None:
        if event.type != "RUN_STARTED":
            raise InvalidEvent("首条事件必须是 RUN_STARTED")
        if event.seq != 1:
            raise InvalidEvent("首条事件的 seq 必须为 1")
        task = _field(event, "task", str)
        max_steps = _field(event, "max_steps", int)
        if max_steps < 0:
            raise InvalidEvent("max_steps must be non-negative")
        return RunView(
            run_id=event.run_id,
            task=task,
            max_steps=max_steps,
            status=RunStatus.RUNNING,
            last_seq=event.seq,
            steps=(),
            answer=None,
        )
    if view.status != RunStatus.RUNNING:
        raise InvalidEvent(f"任务已处于终态 {view.status}，不能再追加事件：{event.type}")
    if event.seq != view.last_seq + 1:
        raise InvalidEvent(f"事件的 seq 必须是上一条的 seq + 1，当前为 {event.seq}")

    if event.type == EventType.STEP_DECIDED:
        step = _field(event, "step", int)
        tool = _field(event, "tool", str)
        args = _field(event, "args", Mapping)
        step_key = _field(event, "step_key", str)
        if step != len(view.steps) + 1:
            raise InvalidEvent(f"step 必须是 {len(view.steps) + 1}，当前为 {step}")
        # 深拷贝：args 里允许嵌套 dict/list，浅拷贝挡不住观察者改内层
        new_step = StepView(step=step, tool=tool, args=deepcopy(dict(args)), step_key=step_key)
        return replace(view, last_seq=event.seq, steps=view.steps + (new_step,))

    if event.type == EventType.STEP_STARTED:
        current = _step_at(view, _field(event, "step", int))
        attempt = _field(event, "attempt", int)
        if current.outcome is not None:
            raise InvalidEvent(f"步骤 {current.step} 已有结果，不能再开始")
        if attempt != current.attempts + 1:
            raise InvalidEvent(f"attempt 必须是 {current.attempts + 1}，当前为 {attempt}")
        return _with_step(view, event.seq, replace(current, attempts=attempt))

    if event.type in (EventType.STEP_COMPLETED, EventType.STEP_FAILED, EventType.STEP_UNCERTAIN):
        current = _step_at(view, _field(event, "step", int))
        if event.type == EventType.STEP_COMPLETED:
            changes: dict[str, Any] = {
                "outcome": Outcome.SUCCEEDED,
                "result": _field(event, "result", Mapping),
            }
        elif event.type == EventType.STEP_FAILED:
            changes = {"outcome": Outcome.FAILED, "error": _field(event, "error", Mapping)}
        else:
            changes = {"outcome": Outcome.UNCERTAIN, "reason": _field(event, "reason", str)}
        if current.attempts == 0:
            raise InvalidEvent(f"步骤 {current.step} 尚未 STEP_STARTED，不能有结果")
        if current.outcome is not None:
            raise InvalidEvent(f"步骤 {current.step} 已有结果，不能出现第二个")
        return _with_step(view, event.seq, replace(current, **changes))

    if event.type == EventType.RUN_SUCCEEDED:
        answer = _field(event, "answer", str)
        if not answer.strip():
            raise InvalidEvent("RUN_SUCCEEDED 的 answer 去空白后为空")
        return replace(view, last_seq=event.seq, status=RunStatus.SUCCEEDED, answer=answer)

    if event.type == EventType.RUN_FAILED:
        _field(event, "reason", str)
        return replace(view, last_seq=event.seq, status=RunStatus.FAILED, answer=None)

    if event.type == EventType.RUN_EXHAUSTED:
        _field(event, "max_steps", int)
        return replace(view, last_seq=event.seq, status=RunStatus.EXHAUSTED, answer=None)

    raise InvalidEvent(f"当前位置不允许事件 {event.type}")


def replay(events: Iterable[Event]) -> RunView:
    """按顺序折叠一串事件，重建视图。

    Args:
        events: 事件序列，调用方保证按 ``seq`` 升序提供。

    Returns:
        RunView: 折叠结果，等价于依次调用 ``apply``。

    Raises:
        InvalidEvent: 事件序列为空或其中任一事件不合法。
    """
    view: RunView | None = None
    for event in events:
        view = apply(view, event)
    if view is None:
        raise InvalidEvent("事件序列为空，无法重建视图")
    return view
