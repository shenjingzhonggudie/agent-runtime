"""真实模型任务落盘后重放；另外显式构造缺失结果的故障现场。

运行：.\\.venv\\Scripts\\python.exe scripts\\recovery_demo.py
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from _live_demo import build_live_policy, build_registry, run_demo, show_result

from agentrun.demo import Ledger
from agentrun.engine import Engine, StopReason
from agentrun.events import EventType, make_step_key
from agentrun.journal import SqliteEventLog
from agentrun.model import RunStatus, replay

NORMAL_RUN = "normal-run"
CRASH_RUN = "crash-run"


def record_crash(log: SqliteEventLog, run_id: str) -> None:
    """显式注入三条故障事件，不把构造的记录称为模型决策。"""
    log.append(run_id, EventType.RUN_STARTED, {"task": "alice 向 bob 转 250", "max_steps": 6})
    log.append(
        run_id,
        EventType.STEP_DECIDED,
        {
            "step": 1,
            "tool": "transfer",
            "args": {"src": "alice", "dst": "bob", "amount": 250},
            "step_key": make_step_key(run_id, 1),
        },
    )
    log.append(run_id, EventType.STEP_STARTED, {"step": 1, "attempt": 1})


def main() -> None:
    """真实查询、SQLite 重开重放，以及明确标注的故障记录。"""
    policy, client = build_live_policy()
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "events.db"
        ledger = Ledger(root / "ledger", initial={"alice": 1000, "bob": 0})
        log = SqliteEventLog(db)
        print("\n=== 场景 1：真实模型查询一次 alice 余额，然后 finish ===", flush=True)
        result = Engine(policy, build_registry(ledger), log).run(
            NORMAL_RUN,
            "调用一次 get_balance 查询 alice 的余额，拿到结果后直接 finish 总结，不做写操作。",
            max_steps=4,
        )
        show_result(result, client)
        events = log.read(NORMAL_RUN)

        print("\n=== 场景 2：重开 SQLite，只重放事件 ===")
        before_calls = client.calls
        reopened = SqliteEventLog(db)
        restored = reopened.read(NORMAL_RUN)
        view = replay(restored)
        print(f"重开后读到 {len(restored)} 条事件，与原事件相同：{restored == events}")
        print(f"重放视图与停止时相等：{view == result.view}")
        print(f"重放新增模型调用：{client.calls - before_calls} 次（重放只读日志）")

        print("\n=== 场景 3：[故障注入] 构造 STEP_STARTED 后缺失结果的历史 ===")
        print("以下三条记录由演示代码构造，用于观察恢复状态；没有发起工具或模型请求。")
        record_crash(reopened, CRASH_RUN)
        crash_events = reopened.read(CRASH_RUN)
        crash_view = replay(crash_events)
        first = crash_view.steps[0]
        print(f"事件：{[event.type.value for event in crash_events]}")
        print(f"状态：{crash_view.status}；attempts={first.attempts}；outcome={first.outcome}")
        print("日志不能证明转账是否生效，需要对账或人工复核。")
        ok = (
            result.stop_reason is StopReason.SUCCEEDED
            and any(step.tool == "get_balance" for step in result.view.steps)
            and restored == events
            and view == result.view
            and crash_view.status is RunStatus.RUNNING
            and first.attempts == 1
            and first.outcome is None
            and client.calls == before_calls
        )
        print(f"演示核对：{'通过' if ok else '未通过'}")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    run_demo(main)
