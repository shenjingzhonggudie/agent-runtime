"""事件日志：分配序号、按顺序保存事件。

两个实现共用同一个 ``EventLog`` 协议：``MemoryEventLog`` 只用于测试与 v0，
``SqliteEventLog`` 负责跨进程持久化。engine 不需要知道用的是哪一个。

依赖方向：
    engine → journal → events / codec
"""

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .codec import DecodeError, decode_event, encode_event
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


class SqliteEventLog:
    """基于 SQLite 的事件日志，跨进程持久化。

    Args:
        path: 数据库文件路径。父目录不存在时由实现负责创建。

    Notes:
        与 ``MemoryEventLog`` 实现同一个 ``EventLog`` 协议，``Engine`` 不需要知道用的是
        哪一个。``seq`` 必须按 ``run_id`` 独立计数且从 1 连续递增：恢复时 ``replay``
        依赖这个连续性来发现缺口，重号或跳号都会让“事件是唯一事实来源”失效。
    """

    def __init__(self, path: Path) -> None:
        """记录数据库位置；不建立连接。

        Args:
            path: 数据库文件路径。

        Notes:
            构造时不连接、不建表。建表放在首次连接时，因此同一个实例可以在文件被
            删除后重新创建，“重启”在测试里就等于换一个新实例。
        """
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        """打开一个连接，并确保表结构存在。

        Returns:
            sqlite3.Connection: 已打开、且 ``runs`` / ``events`` 两张表存在的连接。

        Notes:
            表结构（本轮契约，测试会直接检查这两张表）：

            - ``runs(run_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)``
            - ``events(run_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL,
              PRIMARY KEY (run_id, seq))``

            ``body`` 存 ``encode_event`` 的完整输出。不要手工拼 JSON，也不要给
            ``json.dumps`` 传 ``default=str``：``codec`` 必须是唯一格式来源，否则
            写进去的和读回来的不是同一套规则。

            调用方负责关闭连接。``sqlite3.Connection`` 作为上下文管理器只提交或回滚
            事务，不关闭连接。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "run_id TEXT NOT NULL, "
            "seq INTEGER NOT NULL, "
            "body TEXT NOT NULL, "
            "PRIMARY KEY (run_id, seq))"
        )
        return conn

    def append(self, run_id: str, type_: EventType, payload: Mapping[str, Any]) -> Event:
        """追加一条事件并返回它。

        Args:
            run_id: 任务标识。
            type_: 事件类型。
            payload: 事件字段，写入前做一次浅拷贝。

        Returns:
            Event: 已写入的事件，``seq`` 为该 run 的下一条序号，``at`` 为当前 UTC
            时间（仅展示用，不参与任何判定）。

        Raises:
            EncodeError: ``payload`` 含有无法无损落盘的值。此时必须**整条回滚**：
                不能留下 ``runs.last_seq`` 已经加过的痕迹，否则下一次正常写入会出现
                序号缺口，恢复时会被误判为“丢了一条事件”。
            sqlite3.Error: 数据库层面失败，同样要求整条回滚。

        Notes:
            “读当前 seq + 写入 seq+1”是两步，必须在同一个事务里完成。``sqlite3`` 默认
            的隐式 BEGIN 是 DEFERRED，两个连接会读到同一个 ``last_seq``；要拿到
            IMMEDIATE 写锁必须用 ``isolation_level=None`` 手动控制
            ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT last_seq FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            seq = 1 if row is None else int(row[0]) + 1
            event = Event(
                run_id=run_id,
                seq=seq,
                type=type_,
                payload=dict(payload),
                at=datetime.now(UTC).isoformat(),
            )
            body = encode_event(event)
            if row is None:
                conn.execute("INSERT INTO runs (run_id, last_seq) VALUES (?, ?)", (run_id, seq))
            else:
                conn.execute("UPDATE runs SET last_seq = ? WHERE run_id = ?", (seq, run_id))
            conn.execute(
                "INSERT INTO events (run_id, seq, body) VALUES (?, ?, ?)",
                (run_id, seq, body),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return event

    def read(self, run_id: str) -> tuple[Event, ...]:
        """按 ``seq`` 升序返回该任务的全部事件。

        Args:
            run_id: 任务标识。

        Returns:
            tuple[Event, ...]: 该任务的事件，按 ``seq`` 升序；未知任务返回空元组。

        Raises:
            DecodeError: 库里的记录不是合法的事件文本。此时不能“读到多少算多少”：
                残缺的事件序列会让 ``replay`` 得出一个看起来合法、实际错误的视图。

        Notes:
            解码统一走 ``decode_event``。``events.seq`` 列与 ``body`` 里编码的 ``seq``
            应当一致；不一致说明记录被改过，按解码失败处理，不要以列为准继续。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT seq, body FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        finally:
            conn.close()

        events: list[Event] = []
        for seq, body in rows:
            event = decode_event(body)
            if event.seq != seq:
                raise DecodeError(
                    f"记录 seq 与内容不一致：列为 {seq}，内容为 {event.seq}（run_id={run_id!r}）"
                )
            events.append(event)
        return tuple(events)
