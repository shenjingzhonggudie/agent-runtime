"""真实模型评测：八个有界场景，决策均来自配置的模型服务。

余额与转账是隔离临时目录中的模拟业务；响应丢失、重复投递、人工复核结论由本脚本
明确注入，不代表真实支付或真实审批。每个场景最多六个工具步骤，没有模型重试或
脚本策略兜底。自动化契约测试仍在 tests 中离线运行。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\eval_suite.py
"""

import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# 其余演示脚本在 scripts/demo/ 下。评测集是入口，不该要求调用方先设 PYTHONPATH，
# 所以按自己的位置把它加进搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parent / "demo"))

from _live_demo import build_live_policy  # noqa: E402

from agentrun.demo import Ledger
from agentrun.engine import Engine, StopReason
from agentrun.journal import MemoryEventLog
from agentrun.ledger_tools import GetBalance, LookupRequest, SaveReceipt, Transfer
from agentrun.model import Outcome, RunStatus
from agentrun.model_client import ModelClientError
from agentrun.reconcile import reconcile, resolve
from agentrun.tools import SideEffect, ToolContext, ToolRegistry

RUN = "eval-run"
TRANSFER_TASK = (
    "在隔离模拟账本中从 alice 向 bob 转账250。直接调用 transfer，参数 src=alice、"
    "dst=bob、amount=250，不要先查询，也不要保存回执。只要历史中已有一次成功的"
    "该转账，立即结束并说明结果，绝不重复转账。如果历史显示调用失败且已被人工"
    "确认未生效，可以作为新步骤再执行一次；结果未知时不得自行重试。"
)


@dataclass(frozen=True, slots=True)
class Row:
    """真实运行的观测结果；调用次数包括未拿到回答的网络请求。"""

    name: str
    expected: str
    stop: str
    status: str
    steps: int
    events: int
    balances: str
    calls: int
    milliseconds: int
    ok: bool


class LostResponse:
    """故障注入：转账生效之后、或执行之前，丢失工具响应。"""

    name = "transfer"
    side_effect = SideEffect.NON_IDEMPOTENT

    def __init__(self, inner: Transfer, *, applied: bool) -> None:
        self._inner = inner
        self._applied = applied

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        self._inner.validate(args, ctx)

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        if self._applied:
            self._inner.run(args, ctx)
        raise RuntimeError("演示注入：工具响应丢失")


class DuplicateReceipt:
    """故障注入：向工具投递两次相同参数和同一个引擎 step_key。"""

    name = "save_receipt"
    side_effect = SideEffect.IDEMPOTENT

    def __init__(self, ledger: Ledger) -> None:
        self._inner = SaveReceipt(ledger)
        self.deliveries = 0
        self.same_result = False

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        self._inner.validate(args, ctx)

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        first = self._inner.run(args, ctx)
        self.deliveries += 1
        second = self._inner.run(args, ctx)
        self.deliveries += 1
        self.same_result = first == second
        return second


def registry_with(ledger: Ledger, *, transfer: Any = None, receipt: Any = None) -> ToolRegistry:
    """仅替换故障注入工具，其余工具与 CLI 保持一致。"""
    registry = ToolRegistry()
    for tool in (
        GetBalance(ledger),
        transfer if transfer is not None else Transfer(ledger),
        receipt if receipt is not None else SaveReceipt(ledger),
        LookupRequest(ledger),
    ):
        registry.register(tool)
    return registry


def evaluate(name: str, expected: str, task: str, mode: str) -> Row:
    """运行一个独立场景，检查业务状态，保留真实失败结果。"""
    print(f"\n[开始] {name}，期望 {expected}", flush=True)
    started = time.perf_counter()
    policy, client = build_live_policy()
    with TemporaryDirectory(prefix="agentrun-live-eval-") as tmp:
        ledger = Ledger(Path(tmp), initial={"alice": 1000, "bob": 0})
        duplicate = DuplicateReceipt(ledger)
        transfer: Any = Transfer(ledger)
        if mode in {"reconcile", "retry", "abort"}:
            applied = mode == "reconcile"
            print(f"  故障注入：转账{'已生效' if applied else '执行前'}丢失响应。", flush=True)
            transfer = LostResponse(transfer, applied=applied)
        if mode == "receipt":
            print("  故障注入：同一步骤的回执重复投递两次。", flush=True)
        registry = registry_with(
            ledger,
            transfer=transfer,
            receipt=duplicate if mode == "receipt" else None,
        )
        log = MemoryEventLog()
        result = Engine(policy, registry, log).run(
            RUN, task, max_steps=1 if mode == "budget" else 6
        )
        intermediate_ok = True
        if mode in {"reconcile", "retry", "abort"}:
            intermediate_ok = result.stop_reason is StopReason.UNCERTAIN
            if intermediate_ok:
                checked = reconcile(log, RUN, LookupRequest(ledger))
                if mode == "reconcile":
                    intermediate_ok = checked.recorded
                else:
                    intermediate_ok = checked.requested
                    if intermediate_ok:
                        waiting_calls = client.calls
                        waiting = Engine(policy, registry, log).resume(RUN)
                        intermediate_ok = (
                            waiting.view.status is RunStatus.WAITING_REVIEW
                            and client.calls == waiting_calls
                        )
                        decision = "not_applied" if mode == "retry" else "abort"
                        print(f"  演示人工结论：{decision}，由脚本明确写入。", flush=True)
                        resolve(
                            log,
                            RUN,
                            decision=decision,
                            note="演示操作员已确认注入的旧调用停止且未执行"
                            if mode == "retry"
                            else "演示操作员决定终止任务",
                        )
                if intermediate_ok:
                    before_resume = client.calls
                    result = Engine(policy, registry_with(ledger), log).resume(RUN)
                    if mode == "abort":
                        intermediate_ok = client.calls == before_resume

        actual_balances = (ledger.balance("alice"), ledger.balance("bob"))
        unchanged = actual_balances == (1000, 0)
        transferred = actual_balances == (750, 250)
        success = result.stop_reason is StopReason.SUCCEEDED
        if mode == "budget":
            ok = (
                result.stop_reason is StopReason.EXHAUSTED
                and result.view.answer is None
                and transferred
                and len(result.view.steps) == 1
            )
        elif mode == "rejected":
            ok = (
                success
                and unchanged
                and len(result.view.steps) == 1
                and result.view.steps[0].tool == "transfer"
                and result.view.steps[0].outcome is Outcome.FAILED
            )
        elif mode == "query":
            ok = (
                success
                and unchanged
                and len(result.view.steps) == 1
                and result.view.steps[0].tool == "get_balance"
                and result.view.steps[0].result == {"owner": "alice", "balance": 1000}
            )
        elif mode == "receipt":
            receipt_steps = [s for s in result.view.steps if s.tool == "save_receipt"]
            saved = ledger.lookup(receipt_steps[0].step_key) if receipt_steps else None
            ok = (
                success
                and unchanged
                and len(result.view.steps) == 1
                and len(receipt_steps) == 1
                and duplicate.deliveries == 2
                and duplicate.same_result
                and saved is not None
                and saved.kind == "receipt"
                and saved.text == receipt_steps[0].args.get("text")
            )
        elif mode == "abort":
            ok = result.stop_reason is StopReason.ABORTED and unchanged
        else:
            ok = success and transferred
        ok = ok and intermediate_ok and client.calls > 0
        row = Row(
            name,
            expected,
            result.stop_reason.value,
            result.view.status.value,
            len(result.view.steps),
            len(log.read(RUN)),
            f"{actual_balances[0]}/{actual_balances[1]}",
            client.calls,
            int((time.perf_counter() - started) * 1000),
            ok,
        )
        print(
            f"[{'通过' if ok else '失败'}] {name}：{row.stop}，余额 {row.balances}，"
            f"状态 {row.status}，步骤 {row.steps}，事件 {row.events}，"
            f"真实模型调用 {row.calls} 次，{row.milliseconds} ms",
            flush=True,
        )
        if result.view.failure_reason:
            print(f"  失败原因：{result.view.failure_reason}", flush=True)
        return row


def main() -> None:
    """串行调用真实模型；输出观测值，不把模型偏离指令改写成通过。"""
    scenarios = (
        ("正常转账", "SUCCEEDED / 750/250", TRANSFER_TASK, "normal"),
        (
            "业务明确拒绝",
            "SUCCEEDED / 1000/0",
            "直接调用 transfer 尝试从 alice 向 bob 转账800，参数 src=alice、dst=bob、"
            "amount=800。不要预先查询或改金额。收到拒绝后立即结束说明失败，不得重试。",
            "rejected",
        ),
        ("预算耗尽", "EXHAUSTED / 750/250", TRANSFER_TASK, "budget"),
        (
            "只读余额查询",
            "SUCCEEDED / 1000/0",
            "仅调用一次 get_balance，参数 owner=alice，收到结果后立即结束并报告余额。"
            "不要调用其他工具。",
            "query",
        ),
        (
            "同键回执重放",
            "SUCCEEDED / 1000/0",
            "仅调用一次 save_receipt，text=真实模型幂等回执演示。成功后立即结束。"
            "工具端会注入同一step_key的重复投递；不要另开步骤重复保存，不要转账。",
            "receipt",
        ),
        ("已生效→对账", "SUCCEEDED / 750/250", TRANSFER_TASK, "reconcile"),
        ("人工确认→重做", "SUCCEEDED / 750/250", TRANSFER_TASK, "retry"),
        ("人工决定→终止", "ABORTED / 1000/0", TRANSFER_TASK, "abort"),
    )
    print("真实模型评测；账本、故障和人工结论均为明确标注的演示环境。", flush=True)
    try:
        rows = [evaluate(*scenario) for scenario in scenarios]
    except ModelClientError as exc:
        print(f"模型配置不可用：{exc}")
        raise SystemExit(2) from None
    passed = sum(row.ok for row in rows)
    print(
        f"\n{passed}/{len(rows)} 通过；真实模型调用 {sum(row.calls for row in rows)} 次；"
        f"总计 {sum(row.milliseconds for row in rows)} ms"
    )
    raise SystemExit(0 if passed == len(rows) else 1)


if __name__ == "__main__":
    main()
