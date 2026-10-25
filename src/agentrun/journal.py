"""事件日志：分配序号、保存事件、用租约与 epoch 约束并发写入者。

两个实现共用同一个 ``EventLog`` 协议：``MemoryEventLog`` 只用于测试与单进程场景，
``SqliteEventLog`` 负责跨进程持久化与租约。engine 不需要知道用的是哪一个。

依赖方向：
    engine → journal → events / codec
"""

import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .codec import DecodeError, decode_event, encode_event
from .events import Event, EventType


@dataclass(frozen=True, slots=True)
class Lease:
    """一次租约持有证明。

    Attributes:
        run_id: 任务标识。
        owner: 持有者标识，由调用方提供（进程名、worker id 都行）。
        epoch: 租约代数；每次**接管**加一，续租不变。旧 epoch 永远不再有效。
        expires_at: 到期时刻（``time.time()`` 的秒数）；过期即可被他人接管。
    """

    run_id: str
    owner: str
    epoch: int
    expires_at: float


class LeaseLost(Exception):
    """写入者已经不再持有所需的租约：被接管、已过期，或从未持有。

    抛出它表示**这次写入没有发生**。调用方应当停止工作，而不是重试——租约已经属于
    别人，重试只会制造第二个写入者。
    """


class LeaseBusy(Exception):
    """租约仍被其他持有者占用，本次没有拿到。

    它与 ``LeaseLost`` 的区别是时机：``Busy`` 发生在开始工作之前（什么都不用做，
    交给别人），``Lost`` 发生在工作过程中（必须立刻停手）。
    """


class EventLog(Protocol):
    """事件日志的最小接口：只能追加和读取，不能修改已写事件。

    Notes:
        ``append`` 的 ``lease`` 是可选栅栏：日志里有活跃租约时，不带租约或 epoch
        过期的写入一律 ``LeaseLost``；没有活跃租约时允许无栅栏写入（单 worker 模式）。
    """

    def append(
        self,
        run_id: str,
        type_: EventType,
        payload: Mapping[str, Any],
        *,
        lease: Lease | None = None,
    ) -> Event:
        """追加一条事件并返回它（含日志分配的 ``seq``、``at``）。"""
        ...

    def read(self, run_id: str) -> tuple[Event, ...]:
        """按 ``seq`` 升序返回该任务的全部事件；未知任务返回空元组。"""
        ...

    def acquire(self, run_id: str, owner: str, ttl: float) -> Lease | None:
        """尝试取得租约；被别人占用时返回 ``None``，同一 owner 续用原 epoch。"""
        ...

    def renew(self, lease: Lease, ttl: float) -> Lease:
        """续租并返回新到期时间；租约已失效抛 ``LeaseLost``。"""
        ...

    def release(self, lease: Lease) -> None:
        """主动释放；不是持有者时静默忽略。"""
        ...


class MemoryEventLog:
    """内存实现，用于测试与单进程场景。

    Notes:
        ``seq`` 按 run 独立计数，从 1 开始连续递增。日志不判断事件是否合法——那是
        ``model.apply`` 的职责；日志只负责“写下来、别丢、别改”。

        租约同样在内存里：它证明的是「写入顺序」，不是「跨进程可见性」。需要后者时
        用 ``SqliteEventLog``。
    """

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._leases: dict[str, Lease] = {}
        self._epochs: dict[str, int] = {}

    def append(
        self,
        run_id: str,
        type_: EventType,
        payload: Mapping[str, Any],
        *,
        lease: Lease | None = None,
    ) -> Event:
        """追加一条事件。

        Args:
            run_id: 任务标识。
            type_: 事件类型。
            payload: 事件字段，写入时做一次浅拷贝，避免调用方事后修改影响日志。
            lease: 可选的持有证明；日志里有活跃租约时必须匹配。

        Returns:
            Event: 已写入的事件，``seq`` 为该 run 内的下一条序号，``at`` 为当前
                UTC 时间（仅展示用，不参与任何判定）。

        Raises:
            LeaseLost: 该 run 有活跃租约，而本次写入没有携带匹配的 owner 与 epoch。
        """
        current = self._leases.get(run_id)
        if current is not None and (
            lease is None or lease.owner != current.owner or lease.epoch != current.epoch
        ):
            raise LeaseLost(f"{run_id!r} 的租约属于 {current.owner}（epoch {current.epoch}）")

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

    def acquire(self, run_id: str, owner: str, ttl: float) -> Lease | None:
        """取得或续用租约。

        Args:
            run_id: 任务标识。
            owner: 持有者标识。
            ttl: 租约时长（秒）。

        Returns:
            Lease | None: 取得时返回租约；被其他 owner 占用且未过期时返回 ``None``。

        Notes:
            同一 owner 再次 ``acquire`` 不增加 epoch，直接返回现有租约——否则每次
            重启都会把自己的旧写入判成过期。
        """
        now = time.time()
        current = self._leases.get(run_id)
        if current is not None and current.expires_at > now:
            if current.owner != owner:
                return None
            return current

        epoch = self._epochs.get(run_id, 0) + 1
        self._epochs[run_id] = epoch
        lease = Lease(run_id, owner, epoch, now + ttl)
        self._leases[run_id] = lease
        return lease

    def renew(self, lease: Lease, ttl: float) -> Lease:
        """续租。

        Raises:
            LeaseLost: 租约不存在、owner/epoch 不匹配，或已经过期。
        """
        current = self._leases.get(lease.run_id)
        now = time.time()
        if (
            current is None
            or current.owner != lease.owner
            or current.epoch != lease.epoch
            or current.expires_at <= now
        ):
            raise LeaseLost(f"{lease.run_id!r} 的租约已失效（epoch {lease.epoch}）")
        renewed = Lease(lease.run_id, lease.owner, lease.epoch, now + ttl)
        self._leases[lease.run_id] = renewed
        return renewed

    def release(self, lease: Lease) -> None:
        """主动释放；不是当前持有者时什么也不做。"""
        current = self._leases.get(lease.run_id)
        if current is not None and current.owner == lease.owner and current.epoch == lease.epoch:
            del self._leases[lease.run_id]


class SqliteEventLog:
    """基于 SQLite 的事件日志，跨进程持久化并支持租约栅栏。

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
            sqlite3.Connection: 已打开、且表结构为当前版本的连接。

        Notes:
            表结构（本轮契约，测试会直接检查）：

            - ``runs(run_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL,
              owner TEXT, epoch INTEGER NOT NULL DEFAULT 0, lease_expires_at REAL)``
            - ``events(run_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL,
              PRIMARY KEY (run_id, seq))``

            旧库通过 ``PRAGMA table_info`` 判断缺哪列再 ``ALTER TABLE``，不重建表：
            重建会丢掉事件，也可能把正在运行的任务变成孤儿。

            ``body`` 存 ``encode_event`` 的完整输出。不要手工拼 JSON，也不要给
            ``json.dumps`` 传 ``default=str``：``codec`` 必须是唯一格式来源。

            调用方负责关闭连接。``sqlite3.Connection`` 作为上下文管理器只提交或回滚
            事务，不关闭连接。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "run_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL, "
            "owner TEXT, epoch INTEGER NOT NULL DEFAULT 0, lease_expires_at REAL)"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "owner" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN owner TEXT")
        if "epoch" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0")
        if "lease_expires_at" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN lease_expires_at REAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "run_id TEXT NOT NULL, "
            "seq INTEGER NOT NULL, "
            "body TEXT NOT NULL, "
            "PRIMARY KEY (run_id, seq))"
        )
        return conn

    def append(
        self,
        run_id: str,
        type_: EventType,
        payload: Mapping[str, Any],
        *,
        lease: Lease | None = None,
    ) -> Event:
        """追加一条事件并返回它。

        Args:
            run_id: 任务标识。
            type_: 事件类型。
            payload: 事件字段，写入前做一次浅拷贝。
            lease: 可选的持有证明。

        Returns:
            Event: 已写入的事件，``seq`` 为该 run 的下一条序号，``at`` 为当前 UTC
            时间（仅展示用，不参与任何判定）。

        Raises:
            LeaseLost: 该 run 有活跃租约，而本次写入没有携带匹配的 owner 与 epoch。
                整条事务回滚，``last_seq`` 不变。
            EncodeError: ``payload`` 含有无法无损落盘的值。此时必须**整条回滚**：
                不能留下 ``runs.last_seq`` 已经加过的痕迹，否则下一次正常写入会出现
                序号缺口，恢复时会被误判为“丢了一条事件”。
            sqlite3.Error: 数据库层面失败，同样要求整条回滚。

        Notes:
            “读当前 seq + 写入 seq+1”是两步，必须在同一个事务里完成。``sqlite3`` 默认
            的隐式 BEGIN 是 DEFERRED，两个连接会读到同一个 ``last_seq``；要拿到
            IMMEDIATE 写锁必须用 ``isolation_level=None`` 手动控制
            ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``。

            租约校验与序号分配在**同一个事务**里：先校验租约再写，否则接管者与旧
            worker 可能各写一条，日志出现两个“下一步”。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_seq, owner, epoch, lease_expires_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is not None:
                _, owner, epoch, expires_at = row
                if owner is not None and (
                    lease is None or lease.owner != owner or lease.epoch != int(epoch)
                ):
                    raise LeaseLost(f"{run_id!r} 的租约属于 {owner}（epoch {epoch}）")

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
                conn.execute(
                    "INSERT INTO runs (run_id, last_seq) VALUES (?, ?)",
                    (run_id, seq),
                )
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

    def acquire(self, run_id: str, owner: str, ttl: float) -> Lease | None:
        """取得或续用租约。

        Args:
            run_id: 任务标识。
            owner: 持有者标识。
            ttl: 租约时长（秒）。

        Returns:
            Lease | None: 取得时返回租约；被其他 owner 占用且未过期时返回 ``None``。

        Notes:
            同一 owner 再次 ``acquire`` 不增加 epoch，直接返回现有租约。**接管**才
            增加 epoch；这一步在 ``BEGIN IMMEDIATE`` 里完成，否则两个 worker 会同时
            认为自己拿到了新 epoch。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner, epoch, lease_expires_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            now = time.time()
            if row is None:
                epoch = 1
                conn.execute(
                    "INSERT INTO runs (run_id, last_seq, owner, epoch, lease_expires_at) "
                    "VALUES (?, 0, ?, ?, ?)",
                    (run_id, owner, epoch, now + ttl),
                )
                lease = Lease(run_id, owner, epoch, now + ttl)
            else:
                current_owner, current_epoch, expires_at = row[0], int(row[1]), row[2]
                expired = expires_at is None or float(expires_at) <= now
                if current_owner is not None and not expired:
                    if current_owner != owner:
                        conn.execute("COMMIT")
                        return None
                    lease = Lease(run_id, owner, current_epoch, float(expires_at))
                else:
                    epoch = current_epoch + 1
                    lease = Lease(run_id, owner, epoch, now + ttl)
                    conn.execute(
                        "UPDATE runs SET owner = ?, epoch = ?, lease_expires_at = ? "
                        "WHERE run_id = ?",
                        (owner, epoch, now + ttl, run_id),
                    )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return lease

    def renew(self, lease: Lease, ttl: float) -> Lease:
        """续租并返回新到期时间。

        Raises:
            LeaseLost: owner、epoch 不匹配，或租约已经过期。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = conn.execute(
                "SELECT owner, epoch, lease_expires_at FROM runs WHERE run_id = ?",
                (lease.run_id,),
            ).fetchone()
            expired = (
                row is None
                or row[0] != lease.owner
                or int(row[1]) != lease.epoch
                or row[2] is None
                or float(row[2]) <= now
            )
            if expired:
                raise LeaseLost(f"{lease.run_id!r} 的租约已失效（epoch {lease.epoch}）")
            conn.execute(
                "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
                (now + ttl, lease.run_id),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return Lease(lease.run_id, lease.owner, lease.epoch, now + ttl)

    def release(self, lease: Lease) -> None:
        """主动释放；owner 或 epoch 不匹配时静默忽略。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE runs SET owner = NULL, lease_expires_at = NULL "
                "WHERE run_id = ? AND owner = ? AND epoch = ?",
                (lease.run_id, lease.owner, lease.epoch),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
