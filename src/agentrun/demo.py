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
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

_STATE_FILE = "ledger.json"
_RESERVE = 300  # 转出后 src 的最低余额


class LedgerRejected(Exception):
    """账本在业务上拒绝本次操作，且确定未产生任何副作用。

    调用方可以把它当作“这笔操作一定没生效”的证据；与“调用结果未知”是两回事，
    后者不能仅凭异常类型判断。
    """


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

    def __init__(self, root: Path, initial: dict[str, int] | None = None) -> None:
        """打开或创建 ``root`` 目录下的账本。

        Args:
            root: 账本独占的目录。不存在时创建；不得在其中存放引擎事件日志。
            initial: 首次创建时使用的初始余额；省略时视为空账本（所有账户余额 0）。
                账本已存在时该参数被忽略，不得用它重置余额。

        Notes:
            “首次创建”与“重新打开”必须区分：恢复场景下用同一个 ``root`` 重开账本
            是常态，把 ``initial`` 无条件写回去会让持久化变成假象。
        """
        root.mkdir(parents=True, exist_ok=True)
        self._path = root / _STATE_FILE
        if not self._path.exists():
            self._save({"balances": dict(initial or {}), "records": []})

    def _load(self) -> dict:
        """读取并返回 ``ledger.json`` 中的完整账本状态。

        Returns:
            dict: 包含两个键的状态字典：``"balances"``（账户名到余额的映射）和
                ``"records"``（按生效顺序排列的操作记录列表，每项为 ``Record``
                转成的字典）。

        Notes:
            每次调用都从磁盘重新读取，不做缓存，因此总能看到最近一次成功落盘的
            内容。返回的是新对象，修改它不会影响磁盘；需要持久化时必须调用
            ``_save``。
        """
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, state: dict) -> None:
        # 先写临时文件再原子替换：余额与记录要么一起可见，要么都不可见
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def balance(self, owner: str) -> int:
        """查询账户余额。

        Args:
            owner: 账户名。

        Returns:
            int: 当前余额；未知账户视为 0。

        Notes:
            只读操作，不产生记录、不改变账本，可以安全地重复调用。
        """
        return self._load()["balances"].get(owner, 0)

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

        Notes:
            本方法是非幂等的：相同的 ``src`` / ``dst`` / ``amount`` / ``request_id``
            调用两次会扣两次钱。即使调用方带了调用标识，去重也不是账本的义务。
            成功路径必须保证“余额变动 + 记录”整体可见，不允许只写一半。
        """
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

        Notes:
            首次写入生效，之后同一 ``request_id`` 的调用不覆盖已有内容、也不报错。
            这是本演示为了让“工具端原子去重”可观察而做的简化：真实系统遇到同键
            不同内容通常应报冲突，而不是静默保留旧值。
        """
        state = self._load()
        if any(r["kind"] == "receipt" and r["request_id"] == request_id for r in state["records"]):
            return
        state["records"].append(asdict(Record(kind="receipt", request_id=request_id, text=text)))
        self._save(state)

    def lookup(self, request_id: str) -> Record | None:
        """按 ``request_id`` 查询已生效的操作记录。

        Args:
            request_id: 调用标识。

        Returns:
            Record | None: 查到则返回对应记录；查不到返回 None。

        Notes:
            None 只表示“此刻查不到”，**不等于**“该调用以后也不会生效”。延迟到达
            的请求、仍在执行的调用、尚未刷盘的写入，都可能让查询暂时落空。这个区
            别是后续对账与人工复核的判定基础。
        """
        for r in self._load()["records"]:
            if r["request_id"] == request_id:
                return Record(**r)
        return None
