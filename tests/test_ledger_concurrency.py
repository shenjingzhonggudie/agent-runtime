"""账本并发：两个进程同时读改写时，记录不能比成功次数少。"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentrun.demo import Ledger, LedgerBusy
from agentrun.ledger_tools import Transfer
from agentrun.tools import ToolContext, ToolRejected, ToolStatus, run_tool

ROOT = Path(__file__).resolve().parents[1]
HAMMER = ROOT / "scripts" / "ledger_hammer.py"
CTX = ToolContext(run_id="run-1", step=2, attempt=1, step_key="run-1:2")


def start_hammer(root: Path, rounds: int) -> subprocess.Popen[str]:
    """启动一个连做 ``rounds`` 次转账的进程。"""
    return subprocess.Popen(
        [
            sys.executable,
            str(HAMMER),
            "hammer",
            "--root",
            str(root),
            "--rounds",
            str(rounds),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def start_hold(root: Path, ready: Path, seconds: float) -> subprocess.Popen[str]:
    """启动一个占住账本锁的进程，拿到锁后写就绪标记。"""
    return subprocess.Popen(
        [
            sys.executable,
            str(HAMMER),
            "hold",
            "--root",
            str(root),
            "--seconds",
            str(seconds),
            "--ready",
            str(ready),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_ready(ready: Path, proc: subprocess.Popen[str], timeout: float = 10.0) -> None:
    """等到持锁进程写出就绪标记；超时则杀掉进程并失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.exists():
            return
        if proc.poll() is not None:
            break
        time.sleep(0.02)
    proc.kill()
    raise AssertionError(f"持锁进程没有就绪：{proc.communicate()}")


def test_two_processes_do_not_lose_updates(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    Ledger(root, initial={"A": 1000, "B": 0})

    procs = [start_hammer(root, 20) for _ in range(2)]
    outputs = [proc.communicate(timeout=120)[0] for proc in procs]

    assert [proc.returncode for proc in procs] == [0, 0]
    succeeded = sum(output.count("OK") for output in outputs)
    assert succeeded == 40, outputs

    ledger = Ledger(root)
    # 40 笔 × 10：1000 → 600 / 0 → 400。丢更新的表现就是 A 比 600 多。
    assert ledger.balance("A") == 600
    assert ledger.balance("B") == 400


def test_busy_raises_and_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    Ledger(root, initial={"A": 1000, "B": 0})
    ready = tmp_path / "ready"
    holder = start_hold(root, ready, seconds=2.0)
    try:
        wait_ready(ready, holder)
        ledger = Ledger(root, lock_timeout=0.1)
        with pytest.raises(LedgerBusy):
            ledger.transfer("A", "B", 10, request_id="busy-1")
    finally:
        holder.kill()
        holder.communicate()

    # 拿不到锁时一笔都没动：余额、记录都不变
    later = Ledger(root)
    assert later.balance("A") == 1000
    assert later.balance("B") == 0
    assert later.lookup("busy-1") is None


def test_lock_is_released_when_holder_is_killed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    Ledger(root, initial={"A": 1000, "B": 0})
    ready = tmp_path / "ready"
    holder = start_hold(root, ready, seconds=30.0)
    wait_ready(ready, holder)
    holder.kill()
    holder.communicate()

    # 内核在进程死亡时释放锁：不需要等超时，也不需要清理锁文件
    Ledger(root, lock_timeout=2.0).transfer("A", "B", 50, request_id="after-kill")
    assert Ledger(root).balance("A") == 950


def test_transfer_tool_maps_busy_to_rejected(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    Ledger(root, initial={"A": 1000, "B": 0})
    ready = tmp_path / "ready"
    holder = start_hold(root, ready, seconds=2.0)
    try:
        wait_ready(ready, holder)
        tool = Transfer(Ledger(root, lock_timeout=0.1))
        outcome = run_tool(tool, {"src": "A", "dst": "B", "amount": 10}, CTX)
    finally:
        holder.kill()
        holder.communicate()

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error["kind"] == "rejected"
    assert "账本忙" in str(outcome.error["message"])


def test_rejected_tool_exposes_tool_rejected(tmp_path: Path) -> None:
    """工具层只做映射：``run`` 抛 ``ToolRejected``，不自己吞掉。"""
    ledger = Ledger(tmp_path / "ledger", initial={"A": 400, "B": 0})
    with pytest.raises(ToolRejected):
        Transfer(ledger).run({"src": "A", "dst": "B", "amount": 200}, CTX)
