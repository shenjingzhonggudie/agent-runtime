"""隔离的本地模拟账本，充当整个项目的“外部世界”。

账本只负责保管余额和操作记录：它不知道 run_id，不知道 Agent 存在，不做重试，
也不判断任务是否完成。账本目录由调用方指定，必须与引擎的事件日志分开存储，
两者不共享事务——这是本项目后续所有“结果未知 / 对账 / 人工复核”练习的前提。

副作用承诺（第 1A 轮契约）：

- ``transfer`` 非幂等：同一 request_id 调用两次会扣两次钱，账本不做去重。
- ``save_receipt`` 幂等：同一 request_id 只保留首次写入的内容，重复调用不报错、
  不覆盖。
- 业务拒绝（``LedgerRejected``）表示**确定未生效**：余额与记录都不改变。
- 成功返回即表示已经落盘：重新打开账本仍能读到。

并发承诺（第 15 轮契约）：

- 所有公开方法都在同一把**跨进程独占锁**里读改写：要么看到一次完整写入，要么等。
- 拿不到锁（超时）抛 ``LedgerBusy``，且**确定未生效**——写操作在拿到锁之后才开始。
- 锁由内核持有，进程被杀时自动释放：不会留下需要超时清理的死锁文件。
"""

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any, cast

if sys.platform == "win32":  # pragma: no cover - 平台分支，本机只跑 Windows 这一侧
    import msvcrt
else:  # pragma: no cover - 本机未验证
    # 不要在这里加 `type: ignore`：mypy 会裁剪 `sys.platform` 分支，Windows 上这行
    # 根本不会被分析；到了 Linux 上它变成活代码，忽略标记会被 warn_unused_ignores
    # 判成多余，CI 直接红（第 17 轮实测）。
    import fcntl

_STATE_FILE = "ledger.json"
_LOCK_FILE = "ledger.lock"
_RESERVE = 300  # 转出后 src 的最低余额


class LedgerRejected(Exception):
    """账本在业务上拒绝本次操作，且确定未产生任何副作用。

    调用方可以把它当作“这笔操作一定没生效”的证据；与“调用结果未知”是两回事，
    后者不能仅凭异常类型判断。
    """


class LedgerCorrupted(Exception):
    """账本存储内容不合法，读不出可信状态。

    它与 ``LedgerRejected`` 必须分开：后者表示“确定没生效”，前者表示“现在既不
    能断言生效，也不能断言未生效”。把它误当成“没生效”会直接导致重复副作用。
    """


class LedgerBusy(Exception):
    """在超时之前没有拿到账本锁，本次操作没有开始。

    它表示**确定未生效**：锁在读写之前取得，没拿到就什么都没做。这与
    ``LedgerCorrupted``（不知道）和 ``LedgerRejected``（业务拒绝）都不是一回事。
    调用方可以安全地稍后重试——同一笔业务重试需要新的 ``request_id`` 吗？不需要：
    转账本身非幂等，但**这次没有发生**，用同一个 ``request_id`` 重试不会重复扣款。
    """


if sys.platform == "win32":

    def _try_lock(handle: IO[bytes]) -> bool:
        """非阻塞地锁住文件的第 0 个字节；已被别人锁住时返回 False。

        Args:
            handle: 已经 ``seek(0)`` 的二进制文件对象。

        Returns:
            bool: 拿到锁返回 True。

        Notes:
            Windows 的字节范围锁与文件句柄绑定；同一进程开两个句柄也会互斥，
            所以不要在持锁代码里再次进入 ``ledger_lock``。
        """
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        """释放第 0 个字节上的锁。

        Args:
            handle: 与加锁时同一个句柄，且已 ``seek(0)``。
        """
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - POSIX 分支未在本机验证

    def _try_lock(handle: IO[bytes]) -> bool:
        """非阻塞地取得 ``flock`` 独占锁。

        Args:
            handle: 已打开的文件对象。

        Returns:
            bool: 拿到锁返回 True；被占用返回 False。
        """
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        """释放 ``flock`` 独占锁。

        Args:
            handle: 与加锁时同一个文件对象。
        """
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def ledger_lock(root: Path, *, timeout: float = 5.0) -> Iterator[None]:
    """在账本目录上取得跨进程独占锁，退出时自动释放。

    Args:
        root: 账本目录；不存在时创建。
        timeout: 最长等待秒数。到点仍未拿到锁抛 ``LedgerBusy``。

    Yields:
        None: 持锁期间执行读改写。

    Raises:
        LedgerBusy: 超时仍被其他进程占用。

    Notes:
        锁文件里只有 1 个字节，内容无意义；锁的是"这个字节属于谁"。进程退出或被杀时
        由内核释放，因此**不需要**超时清理陈旧锁——这是它与租约的关键差别：租约靠
        时间推断对方死没死，文件锁由操作系统直接作证。

        等锁用固定步长轮询（5ms），不加指数退避：账本操作是毫秒级的，退避只会让
        等待更长，且会让"谁先拿到"更依赖调度。
    """
    path = root / _LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)

        deadline = time.monotonic() + timeout
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise LedgerBusy(f"账本被占用超过 {timeout}s：{root}")
            time.sleep(0.005)
        try:
            yield
        finally:
            handle.seek(0)
            _unlock(handle)
    finally:
        handle.close()


@dataclass(frozen=True)
class Record:
    """一次已生效操作的记录，由 ``Ledger.lookup`` 返回。

    Attributes:
        kind: 记录类型，``"transfer"`` 或 ``"receipt"``。
        request_id: 调用方提供的幂等键 / 调用标识，用于事后对账。
        src: 转出账户；仅 ``kind == "transfer"`` 时有意义。
        dst: 转入账户；仅 ``kind == "transfer"`` 时有意义。
        amount: 转移金额；仅 ``kind == "transfer"`` 时有意义。
        text: 回执正文；仅 ``kind == "receipt"`` 时有意义。
    """

    kind: str
    request_id: str
    src: str = ""
    dst: str = ""
    amount: int = 0
    text: str = ""


class Ledger:
    """文件持久化的模拟账本，所有状态都落在 ``root`` 目录内。

    实现者自行决定 ``root`` 下的文件格式（JSON、JSONL、SQLite 均可）；对外只暴露
    本类的方法，测试不检查内部格式。
    """

    def __init__(
        self,
        root: Path,
        initial: dict[str, int] | None = None,
        *,
        lock_timeout: float = 5.0,
    ) -> None:
        """打开或创建 ``root`` 目录下的账本。

        Args:
            root: 账本独占的目录。不存在时创建；不得在其中存放引擎事件日志。
            initial: 首次创建时使用的初始余额；省略时视为空账本（所有账户余额 0）。
                账本已存在时该参数被忽略，不得用它重置余额。
            lock_timeout: 每次操作等锁的最长秒数；超时抛 ``LedgerBusy``。

        Raises:
            LedgerBusy: 创建账本时拿不到锁（另一个进程正在初始化同一个目录）。

        Notes:
            “首次创建”与“重新打开”必须区分：恢复场景下用同一个 ``root`` 重开账本
            是常态，把 ``initial`` 无条件写回去会让持久化变成假象。

            构造本身也会写文件（首次创建），所以它同样在锁内进行；账本已存在时
            不取锁，避免"打开一个已有账本"被写者拖住。
        """
        root.mkdir(parents=True, exist_ok=True)
        self._path = root / _STATE_FILE
        self._root = root
        self._lock_timeout = lock_timeout
        if not self._path.exists():
            with self.lock():
                if not self._path.exists():
                    self._save({"balances": dict(initial or {}), "records": []})

    def lock(self) -> contextlib.AbstractContextManager[None]:
        """取得本账本的跨进程独占锁。

        Returns:
            AbstractContextManager[None]: ``with`` 块内持有锁，退出时释放。

        Raises:
            LedgerBusy: 超过 ``lock_timeout`` 仍被其他进程占用。

        Notes:
            公开方法内部已经取锁，调用方只在需要"多步原子"时才自己取；持锁期间
            不要再调用本类的公开方法——同一进程的第二个句柄也会被自己的锁挡住。
        """
        return ledger_lock(self._root, timeout=self._lock_timeout)

    def _load(self) -> dict[str, Any]:
        """读取并校验 ``ledger.json``，返回完整的账本状态。

        Returns:
            dict[str, Any]: 包含两个键的状态字典：``"balances"``（账户名到余额的
                映射）和 ``"records"``（按生效顺序排列的操作记录列表，每项为
                ``Record`` 转成的字典）。

        Raises:
            LedgerCorrupted: 文件不是合法 JSON、顶层不是对象，或 ``balances`` /
                ``records`` 的类型不符。此时不能把账本当成“什么都没发生”。

        Notes:
            每次调用都从磁盘重新读取，不做缓存，因此总能看到最近一次成功落盘的
            内容。返回的是新对象，修改它不会影响磁盘；需要持久化时必须调用
            ``_save``。校验只覆盖读取所需的形状，字段级语义由调用方按需判断。
        """
        try:
            raw: object = json.loads(self._path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LedgerCorrupted(f"账本不是合法 JSON：{self._path}") from exc
        if not isinstance(raw, dict):
            raise LedgerCorrupted("账本顶层必须是对象")
        balances = raw.get("balances")
        records = raw.get("records")
        if not isinstance(balances, dict) or not isinstance(records, list):
            raise LedgerCorrupted("账本缺少 balances(对象) 或 records(数组)")
        if not all(
            isinstance(value, int) and not isinstance(value, bool) for value in balances.values()
        ):
            raise LedgerCorrupted("balances 必须是从账户名到整数的映射")
        return raw

    def _save(self, state: dict[str, Any]) -> None:
        """把完整状态原子落盘。

        Args:
            state: 完整账本状态（``balances`` + ``records``）。

        Raises:
            OSError: 磁盘写入失败。此时 ``ledger.json`` 保持原样，临时文件被清理。

        Notes:
            先写临时文件再 ``os.replace``：余额与记录要么一起可见，要么都不可见。
            临时文件名带进程号——固定名字会被另一个进程同时写，Windows 上还会让
            ``os.replace`` 报 ``WinError 32``（源文件正被占用）。

            调用方必须已经持锁；本函数不做互斥。
        """
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        finally:
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()

    def balance(self, owner: str) -> int:
        """查询账户余额。

        Args:
            owner: 账户名。

        Returns:
            int: 当前余额；未知账户视为 0。

        Raises:
            LedgerCorrupted: 账本文件不可读，或余额字段不是整数。此时不能假定
                余额为 0——那会把“读不出来”误当成“没有钱”。
            LedgerBusy: 等锁超时，这次没有读到任何东西。

        Notes:
            只读操作，不产生记录、不改变账本，可以安全地重复调用。

            读也要排队：``os.replace`` 在 Windows 上会与"别人正打开目标文件"冲突，
            不排队就得靠重试掩盖竞态；统一取锁之后，读到的一定是某一次完整写入。
        """
        # 余额类型已由 _load 校验（balances 的值必须是 int），这里只是告知类型检查器
        with self.lock():
            return cast(int, self._load()["balances"].get(owner, 0))

    def transfer(
        self,
        src: str,
        dst: str,
        amount: int,
        request_id: str,
    ) -> None:
        """从 ``src`` 向 ``dst`` 转移 ``amount``。

        Args:
            src: 转出账户。
            dst: 转入账户。
            amount: 转移金额，单位为测试单位，必须为正整数。
            request_id: 调用方提供的调用标识，写入记录供事后对账使用；
                **账本不按它去重**。

        Returns:
            None: 成功返回即表示余额变动与记录已经整体落盘。

        Raises:
            LedgerRejected: 违反下列任一业务规则时抛出，且不留下任何痕迹
                （余额不变、``lookup(request_id)`` 仍为 None）：
                ``amount > 0``；``amount <= src`` 的当前余额；转移后 ``src`` 的
                余额不低于 300（最低储备线）。
            LedgerBusy: 等锁超时。此时**一笔都没动**，用同一个 ``request_id`` 重试
                安全。

        Notes:
            本方法是非幂等的：相同的 ``src`` / ``dst`` / ``amount`` / ``request_id``
            调用两次会扣两次钱。即使调用方带了调用标识，去重也不是账本的义务。
            成功路径必须保证“余额变动 + 记录”整体可见，不允许只写一半。

            校验、扣款、写记录、落盘全在同一把锁里完成：否则两个进程会各读一份旧
            余额，"检查通过"之后互相覆盖，账本上的记录比实际发生的少，而两边都
            认为自己成功了。
        """
        with self.lock():
            state = self._load()
            balances = state["balances"]
            src_balance = balances.get(src, 0)
            if amount <= 0:
                raise LedgerRejected(f"amount must be positive, got {amount}")
            if amount > src_balance:
                raise LedgerRejected(f"insufficient balance: {src_balance} < {amount}")
            if src_balance - amount < _RESERVE:
                raise LedgerRejected(f"reserve of {_RESERVE} would be broken")

            balances[src] = src_balance - amount
            balances[dst] = balances.get(dst, 0) + amount
            state["records"].append(
                asdict(
                    Record(
                        kind="transfer",
                        request_id=request_id,
                        src=src,
                        dst=dst,
                        amount=amount,
                    )
                )
            )
            self._save(state)

    def save_receipt(self, request_id: str, text: str) -> None:
        """按 ``request_id`` 幂等地保存一份回执。

        Args:
            request_id: 幂等键。同一键重复调用视为同一笔业务。
            text: 回执正文。

        Returns:
            None: 首次写入后落盘；重复调用同样正常返回。

        Raises:
            LedgerBusy: 等锁超时，这次没有写入（回执仍不存在）。

        Notes:
            首次写入生效，之后同一 ``request_id`` 的调用不覆盖已有内容、也不报错。
            这是本演示为了让“工具端原子去重”可观察而做的简化：真实系统遇到同键
            不同内容通常应报冲突，而不是静默保留旧值。
        """
        with self.lock():
            state = self._load()
            if any(
                r["kind"] == "receipt" and r["request_id"] == request_id for r in state["records"]
            ):
                return
            state["records"].append(
                asdict(Record(kind="receipt", request_id=request_id, text=text))
            )
            self._save(state)

    def lookup(self, request_id: str) -> Record | None:
        """按 ``request_id`` 查询已生效的操作记录。

        Args:
            request_id: 调用标识。

        Returns:
            Record | None: 查到则返回对应记录；查不到返回 None。

        Raises:
            LedgerCorrupted: 记录项不是对象，或字段与 ``Record`` 不符。此时“查不到”
                不能被当作“没生效”。
            LedgerBusy: 等锁超时，这次没有读到任何东西——同样不能当作“没生效”。

        Notes:
            None 只表示“此刻查不到”，**不等于**“该调用以后也不会生效”。延迟到达
            的请求、仍在执行的调用、尚未刷盘的写入，都可能让查询暂时落空。这个区
            别是后续对账与人工复核的判定基础。
        """
        with self.lock():
            for item in self._load()["records"]:
                if not isinstance(item, dict):
                    raise LedgerCorrupted("账本记录项必须是对象")
                try:
                    record = Record(**item)
                except TypeError as exc:
                    raise LedgerCorrupted(f"账本记录字段不符：{item!r}") from exc
                if record.request_id == request_id:
                    return record
            return None
