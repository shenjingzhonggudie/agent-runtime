"""真实模型发起工具调用；故障与人工复核结论由演示代码明确注入。

展示 not_applied 后真实模型重新决策，以及 abort 后不再问模型。
运行：.\\.venv\\Scripts\\python.exe scripts\\review_demo.py
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from _live_demo import (
    LostResponseTransfer,
    build_live_policy,
    build_registry,
    run_demo,
    show_result,
    transfer_task,
)

from agentrun.demo import Ledger
from agentrun.engine import Engine, StopReason
from agentrun.journal import SqliteEventLog
from agentrun.ledger_tools import LookupRequest
from agentrun.model import RunStatus
from agentrun.reconcile import reconcile, resolve


def scenario(root: Path, *, abort: bool) -> bool:
    """模型先真实发起调用，随后注入明确说明的人工复核结论。"""
    decision = "abort" if abort else "not_applied"
    run_id = f"review-{decision}"
    print(f"\n=== 真实模型调用 → 故障注入 → 人工复核示例：{decision} ===", flush=True)
    ledger = Ledger(root / "ledger", initial={"alice": 1000, "bob": 0})
    log = SqliteEventLog(root / "events.db")
    policy, client = build_live_policy()
    fault = LostResponseTransfer(ledger, applied=False)
    first = Engine(policy, build_registry(ledger, transfer=fault), log).run(
        run_id, transfer_task(), max_steps=7
    )
    show_result(first, client)
    if first.stop_reason is not StopReason.UNCERTAIN:
        return False
    checked = reconcile(log, run_id, LookupRequest(ledger))
    print(f"自动对账：{checked.detail}")
    if not checked.requested:
        return False

    next_policy, next_client = build_live_policy()
    engine = Engine(next_policy, build_registry(ledger), log)
    waiting = engine.resume(run_id)
    waiting_calls = next_client.calls
    print(f"等待复核时 resume：{waiting.stop_reason}；模型调用：{waiting_calls} 次")
    print(f"[人工复核示例] 操作者写入 {decision}；该结论不由模型生成。")
    note = (
        "演示操作者决定放弃本次任务"
        if abort
        else "演示控制器确认故障发生在执行前，未产生副作用；调用方已停止，不会延迟执行"
    )
    resolved = resolve(log, run_id, decision=decision, note=note)
    before = log.read(run_id)
    result = engine.resume(run_id)
    show_result(result, next_client)
    print(f"复核后状态：{resolved.status}")
    print(f"余额：alice={ledger.balance('alice')}  bob={ledger.balance('bob')}")
    if abort:
        print("ABORTED 是终态，恢复时不会调用模型，也不会追加事件。")
        return (
            waiting.stop_reason is StopReason.WAITING_REVIEW
            and waiting_calls == 0
            and result.stop_reason is StopReason.ABORTED
            and result.view.status is RunStatus.ABORTED
            and next_client.calls == 0
            and log.read(run_id) == before
            and (ledger.balance("alice"), ledger.balance("bob")) == (1000, 0)
        )
    return (
        waiting.stop_reason is StopReason.WAITING_REVIEW
        and waiting_calls == 0
        and resolved.status is RunStatus.RUNNING
        and result.stop_reason is StopReason.SUCCEEDED
        and next_client.calls > 0
        and (ledger.balance("alice"), ledger.balance("bob")) == (750, 250)
    )


def main() -> None:
    """运行两个有界场景，既不让模型冒充审批人，也不伪造模型回答。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        results = [scenario(root / "retry", abort=False), scenario(root / "abort", abort=True)]
        print(f"\n演示核对：{sum(results)}/{len(results)} 通过")
        raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    run_demo(main)
