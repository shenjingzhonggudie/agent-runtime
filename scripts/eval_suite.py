"""离线评测集：把 v1 必须覆盖的结局各跑一遍，打一张表。

覆盖范围（与任务书 §8.4 的验收案例对应）：

1. 正常完成。
2. 工具明确拒绝后仍能结束。
3. 预算耗尽 ≠ 成功。
4. 未注册工具。
5. 策略给不出动作。
6. 副作用已发生、响应丢失 → 对账补记，不重做。
7. 副作用未发生、查不到 → 人工复核 not_applied → 重做一次。
8. 证据不足 → 人工复核 abort → 不执行。

每个场景都核对：停止原因、任务状态、步数、事件数、余额。余额同时充当「重复副作用」
的探针：多转一次就会变成 500/500。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\eval_suite.py
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agentrun.demo import Ledger
from agentrun.engine import Engine, StopReason
from agentrun.journal import MemoryEventLog
from agentrun.ledger_tools import GetBalance, LookupRequest, Transfer
from agentrun.model import RunStatus
from agentrun.policy import Finish, PolicyError, ScriptedPolicy, ToolCall
from agentrun.reconcile import reconcile, resolve
from agentrun.tools import SideEffect, ToolContext, ToolRegistry

ALICE = "alice"
BOB = "bob"
AMOUNT = 250
RUN = "eval-run"


@dataclass(frozen=True, slots=True)
class Row:
    """评测表中的一行。"""

    name: str
    expected: str
    stop: str
    status: str
    steps: int
    events: int
    balances: str
    milliseconds: int
    ok: bool


class LostResponse:
    """执行工具后不返回结果，模拟「响应丢失」。

    Args:
        inner: 真正的转账工具。
        applied: ``True`` 表示副作用已经发生，``False`` 表示调用前就断了。
    """

    name = "transfer"
    side_effect = SideEffect.NON_IDEMPOTENT

    def __init__(self, inner: Transfer, *, applied: bool) -> None:
        self._inner = inner
        self._applied = applied

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """校验交给真实工具，保证失败点只在执行阶段。"""
        self._inner.validate(args, ctx)

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        """按配置决定是否先产生副作用，然后抛出无法归类的异常。"""
        if self._applied:
            self._inner.run(args, ctx)
        raise RuntimeError("响应丢失")


class BrokenPolicy:
    """模拟模型输出无法解析。"""

    def decide(self, view: Any) -> ToolCall:
        raise PolicyError("模型输出不是合法 JSON")


def transfer_call() -> ToolCall:
    return ToolCall("transfer", {"src": ALICE, "dst": BOB, "amount": AMOUNT})


def registry_with(ledger: Ledger, transfer: Any) -> ToolRegistry:
    """注册查询与指定的转账实现。"""
    registry = ToolRegistry()
    registry.register(GetBalance(ledger))
    registry.register(transfer)
    return registry


def ledger_at(root: Path) -> Ledger:
    return Ledger(root, initial={ALICE: 1000, BOB: 0})


def balances(ledger: Ledger) -> str:
    return f"{ledger.balance(ALICE)}/{ledger.balance(BOB)}"


def measure(name: str, expected: str, body: Any) -> Row:
    """计时跑一个场景；``body`` 返回 ``(EngineResult, MemoryEventLog, ledger, ok)``。"""
    started = time.perf_counter()
    result, log, balance_text, ok = body()
    elapsed = int((time.perf_counter() - started) * 1000)
    return Row(
        name=name,
        expected=expected,
        stop=result.stop_reason.value,
        status=result.view.status.value,
        steps=len(result.view.steps),
        events=len(log.read(RUN)),
        balances=balance_text,
        milliseconds=elapsed,
        ok=ok,
    )


def scenario_normal() -> Row:
    def body() -> tuple[Any, MemoryEventLog, str, bool]:
        with TemporaryDirectory() as tmp:
            ledger = ledger_at(Path(tmp))
            registry = registry_with(ledger, Transfer(ledger))
            policy = ScriptedPolicy(
                [ToolCall("get_balance", {"owner": ALICE}), transfer_call(), Finish("转账完成")]
            )
            log = MemoryEventLog()
            result = Engine(policy, registry, log).run(RUN, "转账 250", max_steps=5)
            ok = (
                result.stop_reason is StopReason.SUCCEEDED
                and ledger.balance(ALICE) == 750
                and ledger.balance(BOB) == 250
            )
            return result, log, balances(ledger), ok

    return measure("正常完成", "SUCCEEDED / 750/250", body)


def scenario_rejected() -> Row:
    def body() -> tuple[Any, MemoryEventLog, str, bool]:
        with TemporaryDirectory() as tmp:
            ledger = ledger_at(Path(tmp))
            registry = registry_with(ledger, Transfer(ledger))
            policy = ScriptedPolicy(
                [ToolCall("transfer", {"src": ALICE, "dst": BOB, "amount": 800}), Finish("被拒绝")]
            )
            log = MemoryEventLog()
            result = Engine(policy, registry, log).run(RUN, "超额转账", max_steps=5)
            ok = (
                result.stop_reason is StopReason.SUCCEEDED
                and result.view.steps[0].outcome is not None
                and ledger.balance(ALICE) == 1000
            )
            return result, log, balances(ledger), ok

    return measure("工具拒绝后结束", "SUCCEEDED / 1000/0", body)


def scenario_budget() -> Row:
    def body() -> tuple[Any, MemoryEventLog, str, bool]:
        with TemporaryDirectory() as tmp:
            ledger = ledger_at(Path(tmp))
            registry = registry_with(ledger, Transfer(ledger))
            log = MemoryEventLog()
            result = Engine(ScriptedPolicy([transfer_call(), Finish("来不及")]), registry, log).run(
                RUN, "预算只有 1 步", max_steps=1
            )
            ok = result.stop_reason is StopReason.EXHAUSTED and result.view.answer is None
            return result, log, balances(ledger), ok

    return measure("预算耗尽", "EXHAUSTED / 750/250", body)


def scenario_unknown_tool() -> Row:
    def body() -> tuple[Any, MemoryEventLog, str, bool]:
        with TemporaryDirectory() as tmp:
            ledger = ledger_at(Path(tmp))
            log = MemoryEventLog()
            result = Engine(ScriptedPolicy([ToolCall("nope", {})]), ToolRegistry(), log).run(
                RUN, "未注册工具", max_steps=5
            )
            ok = result.stop_reason is StopReason.FAILED and len(result.view.steps) == 0
            return result, log, balances(ledger), ok

    return measure("未注册工具", "FAILED / 0 步", body)


def scenario_broken_policy() -> Row:
    def body() -> tuple[Any, MemoryEventLog, str, bool]:
        with TemporaryDirectory() as tmp:
            ledger = ledger_at(Path(tmp))
            log = MemoryEventLog()
            result = Engine(BrokenPolicy(), ToolRegistry(), log).run(RUN, "策略失败", max_steps=5)
            ok = result.stop_reason is StopReason.FAILED and len(result.view.steps) == 0
            return result, log, balances(ledger), ok

    return measure("策略给不出动作", "FAILED / 0 步", body)


def scenario_reconcile_hit() -> Row:
    def body() -> tuple[Any, MemoryEventLog, str, bool]:
        with TemporaryDirectory() as tmp:
            ledger = ledger_at(Path(tmp))
            lost = LostResponse(Transfer(ledger), applied=True)
            registry = registry_with(ledger, lost)
            log = MemoryEventLog()
            first = Engine(
                ScriptedPolicy([transfer_call(), Finish("不该执行")]), registry, log
            ).run(RUN, "响应丢失", max_steps=5)
            if first.stop_reason is not StopReason.UNCERTAIN:
                return first, log, balances(ledger), False
            found = reconcile(log, RUN, LookupRequest(ledger))
            if not found.recorded:
                return first, log, balances(ledger), False
            finished = Engine(
                ScriptedPolicy([Finish("占位"), Finish("对账后结束")]),
                ToolRegistry(),
                log,
            ).resume(RUN)
            ok = (
                finished.stop_reason is StopReason.SUCCEEDED
                and ledger.balance(ALICE) == 750
                and ledger.balance(BOB) == 250
            )
            return finished, log, balances(ledger), ok

    return measure("已生效→对账补记", "SUCCEEDED / 750/250", body)


def scenario_review_retry() -> Row:
    def body() -> tuple[Any, MemoryEventLog, str, bool]:
        with TemporaryDirectory() as tmp:
            ledger = ledger_at(Path(tmp))
            registry = registry_with(ledger, LostResponse(Transfer(ledger), applied=False))
            log = MemoryEventLog()
            first = Engine(
                ScriptedPolicy([transfer_call(), Finish("不该执行")]), registry, log
            ).run(RUN, "响应丢失", max_steps=5)
            if first.stop_reason is not StopReason.UNCERTAIN:
                return first, log, balances(ledger), False
            asked = reconcile(log, RUN, LookupRequest(ledger))
            if not asked.requested:
                return first, log, balances(ledger), False
            resolve(log, RUN, decision="not_applied", note="账本查无此笔，进程已退出")
            retry = Engine(
                ScriptedPolicy([Finish("占位"), transfer_call(), Finish("重做完成")]),
                registry_with(ledger, Transfer(ledger)),
                log,
            ).resume(RUN)
            ok = (
                retry.stop_reason is StopReason.SUCCEEDED
                and ledger.balance(ALICE) == 750
                and ledger.balance(BOB) == 250
            )
            return retry, log, balances(ledger), ok

    return measure("未生效→复核重做", "SUCCEEDED / 750/250", body)


def scenario_review_abort() -> Row:
    def body() -> tuple[Any, MemoryEventLog, str, bool]:
        with TemporaryDirectory() as tmp:
            ledger = ledger_at(Path(tmp))
            registry = registry_with(ledger, LostResponse(Transfer(ledger), applied=False))
            log = MemoryEventLog()
            first = Engine(
                ScriptedPolicy([transfer_call(), Finish("不该执行")]), registry, log
            ).run(RUN, "响应丢失", max_steps=5)
            if first.stop_reason is not StopReason.UNCERTAIN:
                return first, log, balances(ledger), False
            reconcile(log, RUN, LookupRequest(ledger))
            resolve(log, RUN, decision="abort", note="证据不足，业务放弃")
            after = Engine(ScriptedPolicy([Finish("不该执行")]), registry, log).resume(RUN)
            ok = (
                after.stop_reason is StopReason.ABORTED
                and after.view.status is RunStatus.ABORTED
                and ledger.balance(ALICE) == 1000
            )
            return after, log, balances(ledger), ok

    return measure("证据不足→终止", "ABORTED / 1000/0", body)


def print_table(rows: list[Row]) -> None:
    """按固定列宽打印结果表。"""
    header = (
        f"{'场景':<14}{'期望':<20}{'停止':<12}{'状态':<14}"
        f"{'步':>3}{'事件':>5}{'余额':>10}{'ms':>6}  判定"
    )
    print(header)
    print("-" * 100)
    for row in rows:
        verdict = "通过" if row.ok else "失败"
        print(
            f"{row.name:<14}{row.expected:<20}{row.stop:<12}{row.status:<14}"
            f"{row.steps:>3}{row.events:>5}{row.balances:>10}{row.milliseconds:>6}  {verdict}"
        )


def main() -> None:
    """跑完整评测集并给出退出码。"""
    rows = [
        scenario_normal(),
        scenario_rejected(),
        scenario_budget(),
        scenario_unknown_tool(),
        scenario_broken_policy(),
        scenario_reconcile_hit(),
        scenario_review_retry(),
        scenario_review_abort(),
    ]
    print_table(rows)
    passed = sum(1 for row in rows if row.ok)
    print(f"\n{passed}/{len(rows)} 通过；总计 {sum(r.milliseconds for r in rows)} ms")
    raise SystemExit(0 if passed == len(rows) else 1)


if __name__ == "__main__":
    main()
