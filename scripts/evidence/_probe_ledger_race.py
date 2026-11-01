"""临时探针：账本的读改写是否真的会丢更新。

两种模式：

- ``hammer``：对同一个账本连做 N 次转账，每成功一次打印一行 ``OK``。
- ``count``：读账本，打印余额与转账记录条数。

两个进程同时 ``hammer``，就能把"成功次数"与"记录条数"对上账。不属于交付代码。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from agentrun.demo import Ledger, LedgerRejected

SRC = "A"
DST = "B"


def main() -> int:
    """按模式执行一次探针。

    Returns:
        int: 进程退出码；``LedgerRejected`` 记一行 ``REJ`` 后继续。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("hammer", "count"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--amount", type=int, default=10)
    args = parser.parse_args()

    if args.mode == "count":
        ledger = Ledger(args.root)
        state = ledger._load()  # noqa: SLF001 - 探针需要直接看记录总数
        transfers = [item for item in state["records"] if item["kind"] == "transfer"]
        print(f"balance={ledger.balance(SRC)}/{ledger.balance(DST)} transfers={len(transfers)}")
        return 0

    ledger = Ledger(args.root, initial={SRC: 1000, DST: 0})
    ok = 0
    for index in range(args.rounds):
        try:
            ledger.transfer(SRC, DST, args.amount, request_id=f"{os.getpid()}-{index}")
        except LedgerRejected as exc:
            print(f"REJ {exc}")
            continue
        ok += 1
    print(f"OK {ok}/{args.rounds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
