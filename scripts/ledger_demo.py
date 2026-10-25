"""账本工具演示：跑一次真实任务，再演示重启后靠对账避免重复扣款。

两个场景：

1. 第一次运行：``get_balance`` → ``transfer`` → ``save_receipt`` → ``Finish``。
   事件流按顺序追加，视图由日志折叠得到。
2. 第二次运行：引擎日志换成新的（模拟重启后日志丢失），但账本目录不变。
   策略先 ``lookup_request``，查到上一次的生效记录就直接结束，不再转账。

只用于观察，不参与引擎实现。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\ledger_demo.py
"""

from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agentrun.demo import Ledger
from agentrun.engine import Engine, EngineResult
from agentrun.events import Event, EventType, make_step_key
from agentrun.journal import MemoryEventLog
from agentrun.ledger_tools import GetBalance, LookupRequest, SaveReceipt, Transfer
from agentrun.model import RunStatus, RunView
from agentrun.policy import Action, Finish, ScriptedPolicy, ToolCall
from agentrun.tools import ToolRegistry

ALICE = "alice"
BOB = "bob"
AMOUNT = 250
INITIAL = {ALICE: 1000, BOB: 0}
RESERVE = 300
RUN_ID = "run-1"
TASK = f"{ALICE} 向 {BOB} 转 {AMOUNT}"
MAX_STEPS = 10

# 第一次运行中 transfer 是第 2 步，幂等键即 run-1:2。
# 第二次运行必须用同一个键去查，才能问出“那一笔到底生效没有”。
TRANSFER_STEP = 2


def describe(event: Event) -> str:
    """把一个事件压成一行，只保留判定需要看的字段。

    Args:
        event: 日志中的一条事件。

    Returns:
        str: 适合直接打印的简短描述。
    """
    payload = event.payload
    kind = event.type
    if kind is EventType.RUN_STARTED:
        return f"task={payload['task']} max_steps={payload['max_steps']}"
    if kind is EventType.STEP_DECIDED:
        return f"step={payload['step']} tool={payload['tool']} key={payload['step_key']}"
    if kind is EventType.STEP_STARTED:
        return f"step={payload['step']} attempt={payload['attempt']}"
    if kind is EventType.STEP_COMPLETED:
        return f"step={payload['step']} result={payload['result']}"
    if kind is EventType.STEP_FAILED:
        return f"step={payload['step']} error={payload['error']}"
    if kind is EventType.STEP_UNCERTAIN:
        return f"step={payload['step']} reason={payload['reason']}"
    if kind is EventType.RUN_SUCCEEDED:
        return f"answer={payload['answer']}"
    if kind is EventType.RUN_FAILED:
        return f"reason={payload['reason']}"
    return f"max_steps={payload['max_steps']}"


def section(title: str) -> None:
    """打印一节的分隔标题。"""
    print(f"\n=== {title} ===")


def show_balances(ledger: Ledger, title: str) -> None:
    """打印两个账户的当前余额。

    Args:
        ledger: 要查询的账本。
        title: 打印在余额前面的说明文字。
    """
    print(f"{title}：{ALICE}={ledger.balance(ALICE)}  {BOB}={ledger.balance(BOB)}")


def show_events(log: MemoryEventLog, run_id: str) -> None:
    """按 seq 顺序逐条打印事件。

    Args:
        log: 事件日志。
        run_id: 要打印的任务标识。
    """
    for event in log.read(run_id):
        print(f"  {event.seq:>2} {event.type:<14} {describe(event)}")


def build_registry(ledger: Ledger) -> ToolRegistry:
    """把四个账本工具注册到同一个注册表，共用同一个账本实例。

    Args:
        ledger: 四个工具共享的账本。

    Returns:
        ToolRegistry: 已注册 ``get_balance`` / ``transfer`` / ``save_receipt`` /
        ``lookup_request`` 的注册表。
    """
    registry = ToolRegistry()
    for tool in (GetBalance(ledger), Transfer(ledger), SaveReceipt(ledger), LookupRequest(ledger)):
        registry.register(tool)
    return registry


def run_first(ledger: Ledger, registry: ToolRegistry, log: MemoryEventLog) -> EngineResult:
    """第一次运行：查余额、转账、存回执、结束。

    Args:
        registry: 已注册账本工具的注册表。
        log: 本次运行使用的事件日志。

    Returns:
        EngineResult: 停止时的视图与原因。
    """
    policy = ScriptedPolicy(
        [
            ToolCall("get_balance", {"owner": ALICE}),
            ToolCall("transfer", {"src": ALICE, "dst": BOB, "amount": AMOUNT}),
            ToolCall("save_receipt", {"text": f"{ALICE} 向 {BOB} 转 {AMOUNT}"}),
            Finish(f"已完成 {ALICE} 向 {BOB} 的 {AMOUNT} 转账"),
        ]
    )
    engine = Engine(policy, registry, log)
    return engine.run(RUN_ID, TASK, max_steps=MAX_STEPS)


class ReconcilePolicy:
    """先对账，再决定要不要重做转账。

    Args:
        run_id: 任务标识，用于还原上一次的 ``step_key``。
        transfer_step: 上一次转账发生在第几步。
        src: 转出账户。
        dst: 转入账户。
        amount: 转移金额。

    Notes:
        不保存内部游标：第几步该做什么由 ``len(view.steps)`` 现算，重启后仍然正确。
        读到 ``found=True`` 就结束，不再碰非幂等工具；读到 ``found=False`` 才补做。
        账本只在 ``lookup`` 与 ``transfer`` 之间被观察，两次决策之间没有共享状态。
    """

    def __init__(self, run_id: str, transfer_step: int, src: str, dst: str, amount: int) -> None:
        self._key = make_step_key(run_id, transfer_step)
        self._src = src
        self._dst = dst
        self._amount = amount

    def decide(self, view: RunView) -> Action:
        """先查账；查不到才转账。

        Args:
            view: 从事件重建的当前视图。

        Returns:
            Action: 查账、转账或结束。

        Raises:
            RuntimeError: 第 1 步不是对账查询，或它还没有结果。
        """
        index = len(view.steps)
        if index == 0:
            return ToolCall("lookup_request", {"request_id": self._key})
        if self._already_applied(view):
            return Finish(f"账本已有 {self._key} 的记录，不再重复转账")
        if index == 1:
            return ToolCall(
                "transfer", {"src": self._src, "dst": self._dst, "amount": self._amount}
            )
        return Finish(f"账本原先没有 {self._key}，已补做转账")

    @staticmethod
    def _already_applied(view: RunView) -> bool:
        """读取第 1 步的对账结果。

        Args:
            view: 当前视图，第 1 步必须已经完成。

        Returns:
            bool: 账本中是否已有该 ``step_key`` 的记录。

        Raises:
            RuntimeError: 第 1 步还没有结果，说明策略在错误的时刻被调用。
        """
        result: Mapping[str, Any] | None = view.steps[0].result
        if result is None:
            raise RuntimeError("第 1 步必须是对账查询，且必须已有结果")
        return result.get("found") is True


def run_resume(ledger: Ledger, registry: ToolRegistry, log: MemoryEventLog) -> EngineResult:
    """第二次运行：引擎日志是空的，但账本还留着上一次的记录。

    Args:
        registry: 已注册账本工具的注册表。
        log: 新的空日志，模拟重启后引擎记忆丢失。

    Returns:
        EngineResult: 停止时的视图与原因。
    """
    policy = ReconcilePolicy(RUN_ID, TRANSFER_STEP, ALICE, BOB, AMOUNT)
    engine = Engine(policy, registry, log)
    return engine.run(RUN_ID, TASK, max_steps=MAX_STEPS)


def main() -> None:
    """依次跑两次运行，并核对余额没有被重复扣减。"""
    with TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger"
        ledger = Ledger(ledger_path, initial=INITIAL)
        registry = build_registry(ledger)

        section("第一次运行：查余额 → 转账 → 存回执 → 结束")
        show_balances(ledger, "运行前")
        first_log = MemoryEventLog()
        first = run_first(ledger, registry, first_log)
        print("\n事件流：")
        show_events(first_log, RUN_ID)
        show_balances(ledger, "运行后")
        print(f"停止原因：{first.stop_reason}")
        print(f"任务状态：{first.view.status}")
        print(f"最终答案：{first.view.answer}")

        section("第二次运行：日志已丢，账本还在（同一个 run_id）")
        show_balances(ledger, "运行前")
        resume_log = MemoryEventLog()
        second = run_resume(ledger, registry, resume_log)
        print("\n事件流：")
        show_events(resume_log, RUN_ID)
        show_balances(ledger, "运行后")
        print(f"停止原因：{second.stop_reason}")
        print(f"任务状态：{second.view.status}")
        print(f"最终答案：{second.view.answer}")

        section("结论")
        alice_now = ledger.balance(ALICE)
        bob_now = ledger.balance(BOB)
        expected = (INITIAL[ALICE] - AMOUNT, INITIAL[BOB] + AMOUNT)
        ok = (
            first.view.status is RunStatus.SUCCEEDED
            and second.view.status is RunStatus.SUCCEEDED
            and (alice_now, bob_now) == expected
            and alice_now >= RESERVE
        )
        print(f"余额停在 {ALICE}={alice_now}  {BOB}={bob_now}，期望 {expected}")
        print("非幂等调用没有被执行第二次。" if ok else "不符合预期，请检查第二次运行的决策。")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
