"""临时探针：两个「确认成功」请求同时进来，日志会怎样。

模拟顺序（两个请求都通过了自己的前置检查）：
    A 读视图 → 判定「待复核」→ 追加 REVIEW_RESOLVED
    B 读视图（同上）→ 判定「待复核」→ 追加 REVIEW_RESOLVED
然后看日志是否还能被 replay。不属于交付代码。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agentrun.events import EventType
from agentrun.journal import SqliteEventLog
from agentrun.model import replay
from agentrun.reconcile import NothingToResolve, resolve

RUN = "r1"


def build_waiting_review(log: SqliteEventLog) -> None:
    """造一个停在 WAITING_REVIEW 的任务。"""
    log.append(RUN, EventType.RUN_STARTED, {"task": "转账 250", "max_steps": 5})
    log.append(
        RUN,
        EventType.STEP_DECIDED,
        {"step": 1, "tool": "transfer", "args": {"amount": 250}, "step_key": "r1:1"},
    )
    log.append(RUN, EventType.STEP_STARTED, {"step": 1, "attempt": 1})
    log.append(RUN, EventType.REVIEW_REQUESTED, {"step": 1, "reason": "调用结果未知"})


def main() -> int:
    """跑一遍串行重复与并发重复。

    Returns:
        int: 0 表示探针跑完（不代表通过的用例都该通过）。
    """
    with tempfile.TemporaryDirectory() as raw:
        log = SqliteEventLog(Path(raw) / "events.db")
        build_waiting_review(log)

        # 情况一：串行重复点击——第二次走的是 resolve()，被状态守卫挡住
        resolve(log, RUN, decision="applied", note="甲：账本里有这笔", result={"ok": True})
        try:
            resolve(log, RUN, decision="applied", note="甲：又点了一次", result={"ok": True})
        except NothingToResolve as exc:
            print(f"[串行] 第二次被拒：{exc}")

        # 情况二：并发重复——两个请求都读到了旧视图，各自直接追加
        payload = {
            "step": 1,
            "decision": "applied",
            "note": "甲：账本里有这笔",
            "result": {"ok": True},
        }
        log.append(RUN, EventType.REVIEW_RESOLVED, payload)
        print(f"[并发] 第二条 REVIEW_RESOLVED 写入后共 {len(log.read(RUN))} 条事件")

        try:
            replay(log.read(RUN))
        except Exception as exc:  # noqa: BLE001 - 探针要看的就是异常类型
            print(f"[并发] replay 失败：{type(exc).__name__}: {exc}")
        else:
            print("[并发] replay 正常——说明日志没被写坏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
