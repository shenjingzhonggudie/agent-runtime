"""进程级故障注入：在指定窗口真正退出进程，供 ``tests/test_process_crash.py`` 调用。

与 ``crash_window_demo.py`` 的区别：这里不是「返回而不写后续代码」，而是 ``os._exit``
立即终止进程——不执行 finally、不 flush、不关数据库连接。注意它仍不等于 ``kill -9``
或断电：文件系统缓存与已提交事务的行为由操作系统和 SQLite 决定，本项目不声称验证了
后者。

三个窗口（``--crash``）：

- ``after_decided``：``STEP_DECIDED`` 已写、``STEP_STARTED`` 未写。决策可复用，工具
  一定没被调用。
- ``before_tool``：``STEP_STARTED`` 已写、工具未调用。外部世界未改变，但日志无法证明
  这一点——恢复必须按「可能已调用」处理。
- ``after_tool``：工具已执行、结果事件未写。副作用已经发生，响应丢失。

运行（一般由测试调用）：
    .\\.venv\\Scripts\\python.exe scripts\\crash_runner.py `
        --db tmp/events.db --ledger tmp/ledger --run-id r1 --crash after_tool
"""

import argparse
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentrun.demo import Ledger
from agentrun.engine import Engine
from agentrun.events import Event, EventType
from agentrun.journal import SqliteEventLog
from agentrun.ledger_tools import Transfer
from agentrun.policy import Finish, ScriptedPolicy, ToolCall
from agentrun.tools import SideEffect, ToolContext, ToolRegistry

ALICE = "alice"
BOB = "bob"
AMOUNT = 250
CRASH_CODE = 9


class CrashingLog:
    """在指定事件前后退出进程的日志包装器。"""

    def __init__(self, inner: SqliteEventLog, crash: str) -> None:
        self._inner = inner
        self._crash = crash

    def read(self, run_id: str) -> tuple[Event, ...]:
        """原样转发读取。"""
        return self._inner.read(run_id)

    def append(self, run_id: str, type_: EventType, payload: Mapping[str, Any]) -> Event:
        """按窗口选择退出时机：写意图之前，或写完之后。"""
        if self._crash == "after_decided" and type_ is EventType.STEP_STARTED:
            os._exit(CRASH_CODE)
        event = self._inner.append(run_id, type_, payload)
        if self._crash == "before_tool" and type_ is EventType.STEP_STARTED:
            os._exit(CRASH_CODE)
        return event


class CrashingTransfer:
    """执行成功之后立刻退出，模拟「副作用已发生、结果没写回」。"""

    name = "transfer"
    side_effect = SideEffect.NON_IDEMPOTENT

    def __init__(self, inner: Transfer) -> None:
        self._inner = inner

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """校验交给真实适配器，保证崩溃前没有额外语义。"""
        self._inner.validate(args, ctx)

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        """执行真实转账，然后退出进程。"""
        self._inner.run(args, ctx)
        os._exit(CRASH_CODE)


def main() -> int:
    """跑一个两步任务，在指定窗口退出；没有崩溃则返回 3。"""
    parser = argparse.ArgumentParser(description="进程级故障注入")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--crash",
        required=True,
        choices=("after_decided", "before_tool", "after_tool"),
    )
    args = parser.parse_args()

    ledger = Ledger(args.ledger, initial={ALICE: 1000, BOB: 0})
    registry = ToolRegistry()
    registry.register(CrashingTransfer(Transfer(ledger)))
    policy = ScriptedPolicy(
        [
            ToolCall("transfer", {"src": ALICE, "dst": BOB, "amount": AMOUNT}),
            Finish("转账完成"),
        ]
    )
    engine = Engine(policy, registry, CrashingLog(SqliteEventLog(args.db), args.crash))
    # 3 步预算：转账 1 步 + 恢复后可能的重做 1 步 + 结束 1 步
    result = engine.run(args.run_id, f"从 {ALICE} 转 {AMOUNT} 给 {BOB}", max_steps=3)

    print(f"没有触发崩溃：{result.stop_reason}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
