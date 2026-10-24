"""事件日志：分配序号、按顺序保存事件。

v0 是内存实现；第 5 轮换成 SQLite 时保持同一接口，engine 不需要改动。

依赖方向：
    engine → journal → events
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from .events import Event, EventType


class EventLog(Protocol):
    """事件日志的最小接口：只能追加和读取，不能修改已写事件。"""

    def append(self, run_id: str, type_: EventType, payload: Mapping[str, Any]) -> Event:
        """追加一条事件并返回它（含日志分配的 ``seq``、``at``）。"""
        ...

    def read(self, run_id: str) -> tuple[Event, ...]:
        """按 ``seq`` 升序返回该任务的全部事件；未知任务返回空元组。"""
        ...


class MemoryEventLog:
    """内存实现，仅用于 v0 与测试。

    Notes:
        ``seq`` 按 run 独立计数，从 1 开始连续递增。日志不判断事件是否合法——那是
        ``model.apply`` 的职责；日志只负责“写下来、别丢、别改”。
    """

    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, run_id: str, type_: EventType, payload: Mapping[str, Any]) -> Event:
        """追加一条事件。

        Args:
            run_id: 任务标识。
            type_: 事件类型。
            payload: 事件字段，写入时做一次浅拷贝，避免调用方事后修改影响日志。

        Returns:
            Event: 已写入的事件，``seq`` 为该 run 内的下一条序号，``at`` 为当前
                UTC 时间（仅展示用，不参与任何判定）。
        """
        seq = sum(1 for event in self._events if event.run_id == run_id) + 1
        event = Event(
            run_id=run_id,
            seq=seq,
            type=type_,
            payload=dict(payload),
            at=datetime.now(UTC).isoformat(),
        )
        self._events.append(event)
        return event

    def read(self, run_id: str) -> tuple[Event, ...]:
        """返回该任务的全部事件，按写入顺序排列。"""
        return tuple(event for event in self._events if event.run_id == run_id)
