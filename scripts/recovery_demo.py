"""事件日志的持久化与恢复演示。

三个场景：

1. 正常跑完：事件写进 SQLite，视图由日志折叠得到。
2. 重开文件：新实例读回同一批事件，``replay`` 出的视图与关闭前完全相等。
3. 崩溃窗口：只写到 ``STEP_STARTED``，进程就没了。重启后 ``replay`` 得到
   ``attempts=1``、``outcome=None``——「已经开始、结果未知」。

场景 3 是本项目所有后续工作的起点：日志里没有结果，就没有任何证据能证明
那笔转账是否生效；这时自动重做非幂等调用会直接造成重复扣款。

只用于观察，不参与引擎实现。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\recovery_demo.py
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from agentrun.demo import Ledger
from agentrun.engine import Engine
from agentrun.events import EventType, make_step_key
from agentrun.journal import SqliteEventLog
from agentrun.ledger_tools import GetBalance, LookupRequest, SaveReceipt, Transfer
from agentrun.model import RunStatus, replay
from agentrun.policy import Finish, ScriptedPolicy, ToolCall
from agentrun.tools import ToolRegistry

ALICE = "alice"
BOB = "bob"
NORMAL_RUN = "normal-run"
CRASH_RUN = "crash-run"


def section(title: str) -> None:
    """打印一节的分隔标题。"""
    print(f"\n=== {title} ===")


def build_registry(ledger: Ledger) -> ToolRegistry:
    """注册四个账本工具，共用同一个账本实例。"""
    registry = ToolRegistry()
    for tool in (GetBalance(ledger), Transfer(ledger), SaveReceipt(ledger), LookupRequest(ledger)):
        registry.register(tool)
    return registry


def record_crash(log: SqliteEventLog, run_id: str) -> None:
    """手工写下「已决策、已开始、但没有结果」这三条事件。

    这里不调用工具，因为要演示的正是一个**没能返回结果**的进程：它把意图写进了
    日志，然后就不再有任何后续事件。
    """
    log.append(run_id, EventType.RUN_STARTED, {"task": "alice 向 bob 转 250", "max_steps": 10})
    log.append(
        run_id,
        EventType.STEP_DECIDED,
        {
            "step": 1,
            "tool": "transfer",
            "args": {"src": ALICE, "dst": BOB, "amount": 250},
            "step_key": make_step_key(run_id, 1),
        },
    )
    log.append(run_id, EventType.STEP_STARTED, {"step": 1, "attempt": 1})


def main() -> None:
    """依次演示正常落盘、重开读回与崩溃现场。"""
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "events.db"
        ledger = Ledger(Path(tmp) / "ledger", initial={ALICE: 1000, BOB: 0})
        registry = build_registry(ledger)

        section("场景 1：正常跑完并落盘")
        log = SqliteEventLog(db)
        policy = ScriptedPolicy(
            [
                ToolCall("get_balance", {"owner": ALICE}),
                Finish(f"{ALICE} 余额充足"),
            ]
        )
        result = Engine(policy, registry, log).run(NORMAL_RUN, "查余额", max_steps=5)
        events = log.read(NORMAL_RUN)
        for event in events:
            print(f"  {event.seq:>2} {event.type}")
        print(f"任务状态：{result.view.status}  答案：{result.view.answer}")

        section("场景 2：换一个实例重开同一个文件")
        reopened = SqliteEventLog(db)
        restored = reopened.read(NORMAL_RUN)
        view = replay(restored)
        print(f"重开后读到 {len(restored)} 条事件")
        print(f"与关闭前读到的完全一致：{restored == events}")
        print(f"重放视图与停止时相等：{view == result.view}")
        print(f"重放状态：{view.status}（{RunStatus.SUCCEEDED}）")

        section("场景 3：崩溃在 STEP_STARTED 之后")
        record_crash(reopened, CRASH_RUN)
        crash_events = reopened.read(CRASH_RUN)
        crash_view = replay(crash_events)
        first = crash_view.steps[0]
        print(f"日志里只有 {len(crash_events)} 条事件：{[e.type.value for e in crash_events]}")
        print(f"任务状态：{crash_view.status}（{RunStatus.RUNNING}）")
        print(
            f"step {first.step}：tool={first.tool} "
            f"attempts={first.attempts} outcome={first.outcome}"
        )
        print(f"调用键：{first.step_key}")
        print("没有结果事件，就没有证据能说明这笔转账生效了——不能自动重做。")

        section("结论")
        ok = (
            restored == events
            and view == result.view
            and crash_view.status is RunStatus.RUNNING
            and first.attempts == 1
            and first.outcome is None
        )
        print("事件是唯一事实来源：进程没了，视图仍能从日志重建。" if ok else "不符合预期。")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
