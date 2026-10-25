"""账本工具：把 ``demo.Ledger`` 包装成满足 ``Tool`` 协议的四个具体工具。

``ledger_tools`` 依赖 ``{demo, tools}``：只做参数校验、一次账本调用和异常映射，
不写事件、不分配 ``seq``、不重试、不判断预算。

依赖方向：
    ledger_tools → {demo, tools}
"""

from collections.abc import Mapping
from typing import Any

from .demo import Ledger, LedgerBusy, LedgerCorrupted, LedgerRejected
from .tools import InvalidArgs, SideEffect, ToolContext, ToolRejected


class GetBalance:
    """查询账户余额（只读）。

    Attributes:
        name: 工具名，注册表与日志使用。
        side_effect: 只读；重复调用不改变外部世界，可用于查询与对账。
    """

    name: str = "get_balance"
    side_effect: SideEffect = SideEffect.READ_ONLY

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """校验参数；不合法时抛 ``InvalidArgs``，且不得产生任何副作用。

        Args:
            args: 待校验的参数映射。
            ctx: 本次调用的上下文；校验阶段不使用。

        Raises:
            InvalidArgs: 出现下列任一情况时抛出：
                缺少 ``owner``；``owner`` 不是 ``str``；去空白后为空。
        """
        owner = args.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            raise InvalidArgs("Invalid owner")

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        """执行一次余额查询。

        Args:
            args: 已通过 ``validate`` 的参数映射。
            ctx: 本次调用的上下文。

        Returns:
            Mapping[str, Any]: ``{"owner": ..., "balance": int}``。未知账户余额为 0。

        Raises:
            ToolRejected: 出现下列任一情况时抛出，且确定没有产生任何效果：
                账本抛                 ``LedgerCorrupted``——存储不可信；本工具只读，读盘发生在
                写盘之前，能证明没有生效。

        Notes:
            未知账户返回余额 0，不视为错误：账本把"从未出现的账户"和"余额为 0
            的账户"当作同一件事。
        """
        owner = args["owner"]
        try:
            balance = self._ledger.balance(owner)
        except LedgerBusy as exc:
            raise ToolRejected(str(exc)) from exc
        except LedgerCorrupted as exc:
            raise ToolRejected(str(exc)) from exc
        return {"owner": owner, "balance": balance}


class Transfer:
    """转账（非幂等）。

    Attributes:
        name: 工具名，注册表与日志使用。
        side_effect: 非幂等；重复调用会重复生效，结果未知时不得自动重做。
    """

    name: str = "transfer"
    side_effect: SideEffect = SideEffect.NON_IDEMPOTENT

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """校验参数；不合法时抛 ``InvalidArgs``，且不得产生任何副作用。

        Args:
            args: 待校验的参数映射。
            ctx: 本次调用的上下文；校验阶段不使用。

        Raises:
            InvalidArgs: 出现下列任一情况时抛出：
                缺少 ``src``、``dst``、``amount`` 中任一字段；``src`` 或 ``dst``
                不是 ``str`` 或去空白后为空；``amount`` 不是 ``int``，或是
                ``bool``（bool 是 int 的子类，不能当作 int 通过），或小于等于 0。
        """
        src = args.get("src")
        dst = args.get("dst")
        amount = args.get("amount")
        if not isinstance(src, str) or not src.strip():
            raise InvalidArgs("Invalid src")
        if not isinstance(dst, str) or not dst.strip():
            raise InvalidArgs("Invalid dst")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise InvalidArgs("Invalid amount")

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        """执行一次转账。

        Args:
            args: 已通过 ``validate`` 的参数映射。
            ctx: 本次调用的上下文；``step_key`` 用作幂等键。

        Returns:
            Mapping[str, Any]: ``{"request_id": ctx.step_key, "src": ...,
            "dst": ..., "amount": ...}``。

        Raises:
            ToolRejected: 出现下列任一情况时抛出，且确定没有产生任何效果：
                账本抛 ``LedgerBusy``——没拿到锁，锁在读写之前取得，一笔都没动；
                账本抛 ``LedgerRejected``——业务明确拒绝（金额非正、余额不足、
                触碰储备线），账本保证零副作用；账本抛 ``LedgerCorrupted``——
                读盘发生在写盘之前，能证明没有生效。

        Notes:
            幂等键不取自 ``args``，只取自本次调用的 ``ctx.step_key``；即使调用方
            在 ``args`` 里塞了同名键也必须忽略，否则就能绕过步骤级去重。转账本身
            仍是非幂等的，传入的键只被账本记录下来供事后对账。
        """
        src = args["src"]
        dst = args["dst"]
        amount = args["amount"]
        try:
            self._ledger.transfer(src, dst, amount, request_id=ctx.step_key)
        except LedgerBusy as exc:
            # 没拿到锁 = 一笔都没动，属于「确定未生效」，可以重试
            raise ToolRejected(f"账本忙：{exc}") from exc
        except LedgerRejected as exc:
            raise ToolRejected(str(exc)) from exc
        except LedgerCorrupted as exc:
            raise ToolRejected(str(exc)) from exc
        return {"request_id": ctx.step_key, "src": src, "dst": dst, "amount": amount}


class SaveReceipt:
    """保存回执（幂等）。

    Attributes:
        name: 工具名，注册表与日志使用。
        side_effect: 幂等；同一幂等键重复调用只有一次效果。
    """

    name: str = "save_receipt"
    side_effect: SideEffect = SideEffect.IDEMPOTENT

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """校验参数；不合法时抛 ``InvalidArgs``，且不得产生任何副作用。

        Args:
            args: 待校验的参数映射。
            ctx: 本次调用的上下文；校验阶段不使用。

        Raises:
            InvalidArgs: 出现下列任一情况时抛出：
                缺少 ``text``；``text`` 不是 ``str``；去空白后为空。
        """
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidArgs("Invalid text")

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        """保存一次回执。

        Args:
            args: 已通过 ``validate`` 的参数映射。
            ctx: 本次调用的上下文；``step_key`` 用作幂等键。

        Returns:
            Mapping[str, Any]: ``{"request_id": ctx.step_key, "text": ...}``。

        Raises:
            ToolRejected: 出现下列任一情况时抛出，且确定没有产生任何效果：
                账本抛 ``LedgerBusy``——没拿到锁；账本抛 ``LedgerRejected``；
                账本抛 ``LedgerCorrupted``——读盘在写盘之前，能证明没有生效。

        Notes:
            幂等键不取自 ``args``，只取自本次调用的 ``ctx.step_key``。去重由账本
            端完成：同一 ``step_key`` 的后续调用不覆盖首次内容、也不报错。
        """
        text = args["text"]
        try:
            self._ledger.save_receipt(ctx.step_key, text)
        except LedgerBusy as exc:
            raise ToolRejected(f"账本忙：{exc}") from exc
        except LedgerRejected as exc:
            raise ToolRejected(str(exc)) from exc
        except LedgerCorrupted as exc:
            raise ToolRejected(str(exc)) from exc
        return {"request_id": ctx.step_key, "text": text}


class LookupRequest:
    """按 ``request_id`` 查询已生效操作（只读）。

    Attributes:
        name: 工具名，注册表与日志使用。
        side_effect: 只读；重复调用不改变外部世界，可用于对账。
    """

    name: str = "lookup_request"
    side_effect: SideEffect = SideEffect.READ_ONLY

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """校验参数；不合法时抛 ``InvalidArgs``，且不得产生任何副作用。

        Args:
            args: 待校验的参数映射。
            ctx: 本次调用的上下文；校验阶段不使用。

        Raises:
            InvalidArgs: 出现下列任一情况时抛出：
                缺少 ``request_id``；``request_id`` 不是 ``str``；去空白后为空。
        """
        request_id = args.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise InvalidArgs("Invalid request_id")

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        """按 ``request_id`` 查询一次已生效操作。

        Args:
            args: 已通过 ``validate`` 的参数映射。
            ctx: 本次调用的上下文。

        Returns:
            Mapping[str, Any]: 查到时为 ``{"found": True, "kind": ...,
            "amount": ...}``（来自 ``Record``）；没查到时为 ``{"found": False}``。
            ``None`` 只表示"此刻查不到"，不表示操作一定没发生。

        Raises:
            ToolRejected: 出现下列任一情况时抛出，且确定没有产生任何效果：
                账本抛                 ``LedgerCorrupted``——存储不可信；本工具只读，读盘
                发生在写盘之前，能证明没有生效。

        Notes:
            本工具只回答"账本里现在有没有这条记录"，不回答"调用方当初有没有发起
            过"。``found=False`` 不能当作"没生效"的证据。
        """
        request_id = args["request_id"]
        try:
            record = self._ledger.lookup(request_id)
        except LedgerBusy as exc:
            raise ToolRejected(f"账本忙：{exc}") from exc
        except LedgerCorrupted as exc:
            raise ToolRejected(str(exc)) from exc
        if record is None:
            return {"found": False}
        return {"found": True, "kind": record.kind, "amount": record.amount}
