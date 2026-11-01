"""租约 worker：供 ``tests/test_process_takeover.py`` 启动的两个真实进程。

两个模式：

- ``hold``：取得租约，写下「已开始」三步，然后模拟一次很慢的工具调用；醒来后尝试
  写结果。若期间被接管，写入被日志拒绝，进程打印 ``LOST`` 并以 7 退出——它没有
  重试，也没有假装自己还活着。
- ``takeover``：取得租约并打印 epoch；用于在旧租约过期后完成接管。

运行（一般由测试调用）：
    .\\.venv\\Scripts\\python.exe scripts\\lease_worker.py `
        --db tmp/events.db --run-id r1 --owner A --ttl 0.2 --hold 1.0 --mode hold
"""

import argparse
import time
from pathlib import Path

from agentrun.events import EventType, make_step_key
from agentrun.journal import LeaseLost, SqliteEventLog

LOST_CODE = 7


def main() -> int:
    """取得租约、按模式行动；被接管时以 7 退出。"""
    parser = argparse.ArgumentParser(description="租约 worker")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--ttl", required=True, type=float)
    parser.add_argument("--hold", type=float, default=0.0)
    parser.add_argument("--mode", choices=("hold", "takeover"), default="hold")
    args = parser.parse_args()

    log = SqliteEventLog(args.db)
    lease = log.acquire(args.run_id, args.owner, args.ttl)
    if lease is None:
        print("BUSY")
        return 8

    print(f"EPOCH {lease.epoch}")
    if args.mode == "takeover":
        return 0

    try:
        log.append(
            args.run_id,
            EventType.RUN_STARTED,
            {"task": "租约演示", "max_steps": 3},
            lease=lease,
        )
        log.append(
            args.run_id,
            EventType.STEP_DECIDED,
            {
                "step": 1,
                "tool": "transfer",
                "args": {"src": "alice", "dst": "bob", "amount": 250},
                "step_key": make_step_key(args.run_id, 1),
            },
            lease=lease,
        )
        log.append(args.run_id, EventType.STEP_STARTED, {"step": 1, "attempt": 1}, lease=lease)
        print("STARTED")
        time.sleep(args.hold)
        log.append(
            args.run_id,
            EventType.STEP_COMPLETED,
            {"step": 1, "result": {"ok": True}},
            lease=lease,
        )
        print("WROTE")
        return 0
    except LeaseLost:
        print("LOST")
        return LOST_CODE


if __name__ == "__main__":
    raise SystemExit(main())
