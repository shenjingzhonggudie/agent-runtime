"""账本并发夹具：并发转账与「占着锁不放」。

两个模式：

- ``hammer``：对同一个账本连做 N 次转账，每成功一次输出一行 ``OK``；被业务拒绝
  输出一行 ``REJ``。
- ``hold``：拿到账本锁后写一个就绪标记文件，然后占住锁若干秒；用来验证
  ``LedgerBusy`` 与「持锁进程被杀后锁自动释放」。

这是测试夹具，不是教学脚本：它只做一件事，输出给测试断言用。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from agentrun.demo import Ledger, LedgerBusy, LedgerRejected

SRC = "A"
DST = "B"


def main() -> int:
    """按模式执行一次夹具。

    Returns:
        int: ``hold`` 模式下没拿到锁返回 1，其余返回 0。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("hammer", "hold"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--amount", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--ready", type=Path, default=None)
    args = parser.parse_args()

    if args.mode == "hammer":
        ledger = Ledger(args.root, initial={SRC: 1000, DST: 0})
        for _ in range(args.rounds):
            try:
                ledger.transfer(
                    SRC, DST, args.amount, request_id=f"{args.root.name}-{time.time_ns()}"
                )
            except LedgerRejected:
                print("REJ", flush=True)
                continue
            print("OK", flush=True)
        return 0

    try:
        with Ledger(args.root).lock():
            if args.ready is not None:
                args.ready.write_text("ready", encoding="utf-8")
            print("HELD", flush=True)
            time.sleep(args.seconds)
    except LedgerBusy as exc:
        print(f"BUSY {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
