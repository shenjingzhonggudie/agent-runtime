"""事件时间线：把日志渲染成人能读的形状，并可导出为 JSONL。

这个模块是**只读观察者**：不写事件、不分配 ``seq``、不改变任何状态，也不判断事件是否
合法（那是 ``model.apply`` 的事）。它拿到的每个 ``TraceLine`` 都是新对象，改它不会
影响原事件。

依赖方向：
    trace → {events, codec}
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .codec import encode_event
from .events import Event, EventType


@dataclass(frozen=True, slots=True)
class TraceLine:
    """时间线上的一行，完全由一条事件推出。

    Attributes:
        seq: 事件序号，与日志一致。
        at: ISO-8601 UTC 时间字符串，来自事件本身，仅用于展示。
        kind: 事件类型的字符串值。
        summary: 一句话说明发生了什么；字段缺失时用空串占位，不抛异常。
    """

    seq: int
    at: str
    kind: str
    summary: str


def summarize(event: Event) -> str:
    """把一条事件压成一句话。

    Args:
        event: 待描述的事件。

    Returns:
        str: 人类可读的说明。

    Notes:
        取值一律用 ``payload.get``，不假设字段齐全。时间线是观察工具，遇到残缺事件
        应当仍然打得出来——把异常留给 ``model.apply``，那里才有足够上下文判断对错。
    """
    payload = event.payload
    kind = event.type

    if kind is EventType.RUN_STARTED:
        return f"任务「{payload.get('task', '')}」开始，预算 {payload.get('max_steps')} 步"
    if kind is EventType.STEP_DECIDED:
        return f"决定第 {payload.get('step')} 步：{payload.get('tool')} {payload.get('args')}"
    if kind is EventType.STEP_STARTED:
        return f"第 {payload.get('step')} 步第 {payload.get('attempt')} 次尝试"
    if kind is EventType.STEP_COMPLETED:
        return f"第 {payload.get('step')} 步完成：{payload.get('result')}"
    if kind is EventType.STEP_FAILED:
        return f"第 {payload.get('step')} 步失败：{payload.get('error')}"
    if kind is EventType.STEP_UNCERTAIN:
        return f"第 {payload.get('step')} 步结果未知：{payload.get('reason', '')}"
    if kind is EventType.RUN_SUCCEEDED:
        return f"任务成功：{payload.get('answer', '')}"
    if kind is EventType.RUN_FAILED:
        return f"任务失败：{payload.get('reason', '')}"
    if kind is EventType.REVIEW_REQUESTED:
        return f"第 {payload.get('step')} 步提交人工复核：{payload.get('reason', '')}"
    if kind is EventType.REVIEW_RESOLVED:
        return (
            f"第 {payload.get('step')} 步复核结论："
            f"{payload.get('decision')}（{payload.get('note', '')}）"
        )
    return f"预算耗尽（上限 {payload.get('max_steps')} 步）"


def timeline(events: Iterable[Event]) -> tuple[TraceLine, ...]:
    """把事件序列渲染成时间线。

    Args:
        events: 按 ``seq`` 升序的事件。

    Returns:
        tuple[TraceLine, ...]: 与输入等长的时间线，顺序不变。

    Notes:
        不校验顺序，也不排序：时间线展示的是**日志里实际的顺序**，乱序本身就是需要
        被看见的问题，替调用方排好反而把问题藏了。
    """
    return tuple(
        TraceLine(seq=event.seq, at=event.at, kind=event.type.value, summary=summarize(event))
        for event in events
    )


def export_jsonl(events: Sequence[Event], path: Path) -> int:
    """把事件导出成 JSONL，一行一条。

    Args:
        events: 要导出的事件。
        path: 目标文件；父目录不存在时创建。

    Returns:
        int: 写出的行数。

    Raises:
        EncodeError: 某条事件含有无法无损落盘的值。此时文件可能只写了一半，
            调用方应当把它当失败处理，不要读回半份数据。

    Notes:
        每行就是 ``encode_event`` 的输出，因此导出的文件可以被 ``decode_event``
        逐行读回。换行固定用 ``\\n``，不随平台变化——JSONL 的消费方通常是别的工具。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(encode_event(event))
            handle.write("\n")
            count += 1
    return count
