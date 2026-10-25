"""两个真实进程的接管：旧 worker 醒来后不能再写。

这不是线程模拟：``scripts/lease_worker.py`` 是独立进程，租约到期后由另一个进程
接管；旧进程随后尝试写入，被日志的 epoch 栅栏拒绝。
"""

import subprocess
import sys
import time
from pathlib import Path

from agentrun.events import EventType
from agentrun.journal import SqliteEventLog
from agentrun.model import RunStatus, replay

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "lease_worker.py"
RUN = "takeover"


def start_hold(db: Path, owner: str, ttl: float, hold: float) -> subprocess.Popen[str]:
    """启动一个持有租约、卡在工具里的进程。"""
    return subprocess.Popen(
        [
            sys.executable,
            str(WORKER),
            "--db",
            str(db),
            "--run-id",
            RUN,
            "--owner",
            owner,
            "--ttl",
            str(ttl),
            "--hold",
            str(hold),
            "--mode",
            "hold",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def takeover(db: Path, owner: str) -> subprocess.CompletedProcess[str]:
    """启动一个只取租约、打印 epoch 的进程。"""
    return subprocess.run(
        [
            sys.executable,
            str(WORKER),
            "--db",
            str(db),
            "--run-id",
            RUN,
            "--owner",
            owner,
            "--ttl",
            "30",
            "--mode",
            "takeover",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_takeover_process_blocks_stale_writes(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    log = SqliteEventLog(db)
    old = start_hold(db, "A", ttl=0.5, hold=1.5)

    deadline = time.time() + 5
    while len(log.read(RUN)) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert len(log.read(RUN)) == 3

    time.sleep(0.6)  # 等 A 的租约过期；它仍睡在工具里
    new = takeover(db, "B")
    assert new.returncode == 0, new.stderr
    assert "EPOCH 2" in new.stdout

    out, err = old.communicate(timeout=10)
    assert old.returncode == 7, f"{old.returncode}: {out} {err}"
    assert "LOST" in out

    # A 的结果写入被拒绝，日志停在「已开始、没有结果」
    view = replay(log.read(RUN))
    assert view.status is RunStatus.RUNNING
    assert view.steps[0].outcome is None
    assert [event.type for event in log.read(RUN)] == [
        EventType.RUN_STARTED,
        EventType.STEP_DECIDED,
        EventType.STEP_STARTED,
    ]

    # 接管者仍能写；同一 owner 重新取租约不涨 epoch
    lease = log.acquire(RUN, "B", 30)
    assert lease is not None
    assert lease.epoch == 2
    log.append(RUN, EventType.RUN_FAILED, {"reason": "接管后由新 worker 终止"}, lease=lease)
    assert replay(log.read(RUN)).status is RunStatus.FAILED
