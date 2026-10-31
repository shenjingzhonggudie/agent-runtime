"""真实模型选择转账；注入响应丢失后，对账补记并让真实模型续跑。

运行：.\\.venv\\Scripts\\python.exe scripts\\reconcile_demo.py
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
from agentrun.model import Outcome
from agentrun.reconcile import reconcile

RUN_ID = "reconcile-live"


def main() -> None:
    """模型真实决策，故障由包装工具注入，对账本身只查询账本。"""
    policy, client = build_live_policy()
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        ledger = Ledger(root / "ledger", initial={"alice": 1000, "bob": 0})
        log = SqliteEventLog(root / "events.db")
        fault = LostResponseTransfer(ledger, applied=True)
        print("\n=== 真实模型选择工具；转账生效后注入响应丢失 ===", flush=True)
        first = Engine(policy, build_registry(ledger, transfer=fault), log).run(
            RUN_ID, transfer_task(receipt=True), max_steps=7
        )
        show_result(first, client)
        if first.stop_reason is not StopReason.UNCERTAIN:
            print("未抵达预期故障现场，演示核对未通过。")
            raise SystemExit(1)

        print("\n=== 对账前 resume：不问模型，不重做未知调用 ===")
        next_policy, next_client = build_live_policy()
        engine = Engine(next_policy, build_registry(ledger), log)
        stopped = engine.resume(RUN_ID)
        print(f"停止原因：{stopped.stop_reason}；新增模型调用：{next_client.calls} 次")
        stopped_calls = next_client.calls

        print("\n=== 按实际 step_key 对账并补记，再让真实模型续跑 ===", flush=True)
        checked = reconcile(log, RUN_ID, LookupRequest(ledger))
        print(checked.detail)
        second = engine.resume(RUN_ID)
        show_result(second, next_client)
        print(f"余额：alice={ledger.balance('alice')}  bob={ledger.balance('bob')}")
        succeeded_transfers = [
            step
            for step in second.view.steps
            if step.tool == "transfer" and step.outcome is Outcome.SUCCEEDED
        ]
        ok = (
            stopped.stop_reason is StopReason.UNCERTAIN
            and stopped_calls == 0
            and checked.recorded
            and second.stop_reason is StopReason.SUCCEEDED
            and len(succeeded_transfers) == 1
            and fault.calls == 1
            and (ledger.balance("alice"), ledger.balance("bob")) == (750, 250)
        )
        print(f"演示核对：{'通过，转账只生效一次' if ok else '未通过'}")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    run_demo(main)
