"""临时探针：一边写一边读，读侧会不会误报「日志不完整」。

- ``writer``：连写 N 条事件。
- ``reader``：连读 N 次，统计 ``JournalCorrupted`` 次数。

用来验证「两次 SELECT 之间被写入插队」是否真的会让完整性校验误判。不属于交付代码。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from agentrun.events import EventType
from agentrun.journal import JournalCorrupted, SqliteEventLog

RUN = "probe"


def main() -> int:
    """按模式执行一次探针。

    Returns:
        int: ``writer`` 返回 0；``reader`` 返回误报次数（0 表示没复现）。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("writer", "reader"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--wait", type=float, default=0.0)
    args = parser.parse_args()

    log = SqliteEventLog(args.db)
    if args.mode == "writer":
        for index in range(args.count):
            log.append(RUN, EventType.STEP_STARTED, {"step": 1, "attempt": index + 1})
        return 0

    time.sleep(args.wait)
    bad = 0
    reads = 0
    for _ in range(args.count):
        try:
            log.read(RUN)
        except JournalCorrupted:
            bad += 1
        reads += 1
    print(f"reads={reads} corrupted={bad}")
    return bad


if __name__ == "__main__":
    raise SystemExit(0 if main() == 0 else 1)
