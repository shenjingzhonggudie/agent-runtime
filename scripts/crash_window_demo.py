"""同一个崩溃点，在三种设计下的不同后果。

只用于观察，不参与引擎实现。所谓“崩溃”是指：线程在写完日志之前直接返回，
不再执行后续代码。这不是真进程退出，也没有断电，只演示“两个存储不同步”这件事。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\crash_window_demo.py
"""

import json
import os
import tempfile
from pathlib import Path

from agentrun.demo import Ledger

ALICE = "alice"
BOB = "bob"
INITIAL = {ALICE: 1000, BOB: 0}


def line(title: str) -> None:
    print(f"\n=== {title} ===")


def balances(ledger: Ledger) -> str:
    return f"alice={ledger.balance(ALICE)}  bob={ledger.balance(BOB)}"


def scenario_no_log(root: Path) -> None:
    """v0：事件只在内存，进程一死什么都不剩。"""
    line("场景 A：没有持久化日志（v0）")
    ledger = Ledger(root / "a", initial=INITIAL)

    print("[进程 1] 决定转账 250 → 账本执行成功")
    ledger.transfer(ALICE, BOB, 250, "req-1")
    print("[进程 1] 崩溃（内存里的事件随进程消失，磁盘上没有任何日志）")

    print(f"[重启]   读取账本：{balances(ledger)}")
    print("[重启]   引擎看不到任何历史，只能认为“任务还没做”，于是再来一次")
    ledger.transfer(ALICE, BOB, 250, "req-1")
    print(f"[重启]   第二次执行后：{balances(ledger)}")
    print("结论：业务被扣了两次，而引擎认为自己只执行了一次。")


def scenario_intent_log(root: Path) -> None:
    """v1：先写意图，再执行；结果没写下来时状态是“未知”。"""
    line("场景 B：意图已落盘、结果未落盘（v1）")
    world = root / "b"
    ledger = Ledger(world / "ledger", initial=INITIAL)
    log_path = world / "engine_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print("[进程 1] 先把意图写进日志：STEP_STARTED req-1 amount=250")
    log_path.write_text(json.dumps({"event": "STEP_STARTED", "req": "req-1"}) + "\n")

    print("[进程 1] 调用 transfer → 成功")
    ledger.transfer(ALICE, BOB, 250, "req-1")

    print("[进程 1] 崩溃（结果事件没来得及写）")
    events = [json.loads(x) for x in log_path.read_text().splitlines()]
    print(f"[重启]   日志里有 {len(events)} 条事件：{[e['event'] for e in events]}")
    print("[重启]   日志说：有意图，没结果 → 这笔调用处于“未知”")
    print(f"[重启]   账本现状：{balances(ledger)}")

    found = ledger.lookup("req-1")
    print(f"[重启]   对账查询 lookup('req-1') → {found.kind if found else None}")
    print("[重启]   查到已生效 → 补记结果，不再执行第二次")
    print(f"[重启]   最终：{balances(ledger)}")


def scenario_same_storage(root: Path) -> None:
    """反事实：如果账本和日志在同一个文件、同一次原子替换里。"""
    line("场景 C：账本与日志放同一个文件，一次原子替换（反事实）")
    path = root / "c" / "world.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    def save(state: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)

    before = {ALICE: 1000, BOB: 0, "log": []}
    after = {ALICE: 750, BOB: 250, "log": ["transfer req-1 done"]}
    save(before)

    print("[进程 1] 在内存里算出“扣款后”的新状态：alice=750 bob=250 log=[done]")
    print("[进程 1] 崩溃在 save 之前")
    print(f"[重启]   磁盘：{json.loads(path.read_text())}")
    print("         → 钱没扣，日志也没有，两者一致：这笔就是没发生")

    save(after)
    print("[进程 1] 崩溃在 save 之后（此时钱和日志已经一起落盘）")
    print(f"[重启]   磁盘：{json.loads(path.read_text())}")
    print("         → 钱扣了，日志也有，两者一致：这笔就是发生了")
    print("结论：不存在“钱扣了但日志没有”的中间态，因此也就不需要对账。")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        scenario_no_log(root)
        scenario_intent_log(root)
        scenario_same_storage(root)


if __name__ == "__main__":
    main()
