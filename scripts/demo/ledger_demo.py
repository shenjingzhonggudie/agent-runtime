"""真实模型选择账本工具，并在重开同一 SQLite 日志后避免重复转账。

运行：.\\.venv\\Scripts\\python.exe scripts\\ledger_demo.py
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from _live_demo import build_live_policy, build_registry, run_demo, show_result, transfer_task

from agentrun.demo import Ledger
from agentrun.engine import Engine, StopReason
from agentrun.journal import SqliteEventLog
from agentrun.model import Outcome

RUN_ID = "ledger-live"


def main() -> None:
    """运行并重开同一日志，核对余额、事件和成功转账次数。"""
    policy, client = build_live_policy()
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "events.db"
        ledger_path = root / "ledger"
        ledger = Ledger(ledger_path, initial={"alice": 1000, "bob": 0})
        log = SqliteEventLog(db)
        print("\n=== 真实模型决策：查询余额、转账 250、保存回执、结束 ===", flush=True)
        first = Engine(policy, build_registry(ledger), log).run(
            RUN_ID, transfer_task(receipt=True), max_steps=6
        )
        show_result(first, client)
        for event in log.read(RUN_ID):
            print(f"  {event.seq:>2} {event.type.value}")
        before = log.read(RUN_ID)

        print("\n=== 重开账本与同一日志，resume 已完成任务 ===", flush=True)
        resumed_policy, resumed_client = build_live_policy()
        reopened = SqliteEventLog(db)
        second = Engine(resumed_policy, build_registry(Ledger(ledger_path)), reopened).resume(
            RUN_ID
        )
        show_result(second, resumed_client)
        print("已完成的任务直接返回已有视图，因此本次 resume 的模型调用应为 0。")
        print(f"余额：alice={ledger.balance('alice')}  bob={ledger.balance('bob')}")
        print(f"resume 未追加事件：{reopened.read(RUN_ID) == before}")
        transfers = [
            step
            for step in second.view.steps
            if step.tool == "transfer" and step.outcome is Outcome.SUCCEEDED
        ]
        receipt = any(
            step.tool == "save_receipt" and step.outcome is Outcome.SUCCEEDED
            for step in second.view.steps
        )
        ok = (
            first.stop_reason is StopReason.SUCCEEDED
            and second.stop_reason is StopReason.SUCCEEDED
            and len(transfers) == 1
            and receipt
            and (ledger.balance("alice"), ledger.balance("bob")) == (750, 250)
            and reopened.read(RUN_ID) == before
            and resumed_client.calls == 0
        )
        print(f"演示核对：{'通过' if ok else '未通过，请查看停止原因与工具结果'}")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    run_demo(main)
