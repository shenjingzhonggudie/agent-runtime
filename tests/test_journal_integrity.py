"""日志完整性：读侧要验真，不能“读到多少算多少”。"""

import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentrun import cli
from agentrun.codec import encode_event
from agentrun.events import Event, EventType
from agentrun.journal import JournalCorrupted, MemoryEventLog, SqliteEventLog
from agentrun.model import InvalidEvent, replay

RUN = "run-a"
STARTED = {"task": "转账 250", "max_steps": 10}
ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "_probe_journal_read.py"


def build_log(tmp_path: Path) -> tuple[SqliteEventLog, Path]:
    """建一个写了三条事件的日志，返回实例与数据库路径。"""
    path = tmp_path / "events.db"
    log = SqliteEventLog(path)
    log.append(RUN, EventType.RUN_STARTED, STARTED)
    log.append(RUN, EventType.STEP_DECIDED, {"step": 1, "tool": "get_balance", "args": {}})
    log.append(RUN, EventType.RUN_FAILED, {"reason": "演示用"})
    return log, path


def test_intact_log_reads_three_events(tmp_path: Path) -> None:
    log, _ = build_log(tmp_path)
    assert [event.seq for event in log.read(RUN)] == [1, 2, 3]


def test_acquired_but_unwritten_run_reads_empty(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    assert log.acquire(RUN, "w1", 30) is not None
    assert log.read(RUN) == ()


def test_read_detects_missing_middle_event(tmp_path: Path) -> None:
    _, path = build_log(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM events WHERE run_id = ? AND seq = 2", (RUN,))
    conn.commit()
    conn.close()

    with pytest.raises(JournalCorrupted, match="序号不连续"):
        SqliteEventLog(path).read(RUN)


def test_read_detects_missing_tail_event(tmp_path: Path) -> None:
    """末尾少一条时序号仍然连续，只有 last_seq 能暴露。"""
    _, path = build_log(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM events WHERE run_id = ? AND seq = 3", (RUN,))
    conn.commit()
    conn.close()

    with pytest.raises(JournalCorrupted, match="last_seq"):
        SqliteEventLog(path).read(RUN)


def test_read_detects_event_from_another_run(tmp_path: Path) -> None:
    log, path = build_log(tmp_path)
    log.append("run-b", EventType.RUN_STARTED, {"task": "b", "max_steps": 1})
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE events SET body = (SELECT body FROM events WHERE run_id = 'run-b' AND seq = 1) "
        "WHERE run_id = ? AND seq = 1",
        (RUN,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(JournalCorrupted, match="run-b"):
        SqliteEventLog(path).read(RUN)


def test_replay_rejects_event_from_another_run() -> None:
    log = MemoryEventLog()
    first = log.append(RUN, EventType.RUN_STARTED, STARTED)
    # 把陌生事件接到后面：seq 连续，只有 run_id 不一致
    forged = Event(
        run_id="run-b",
        seq=2,
        type=EventType.RUN_FAILED,
        payload={"reason": "混进来的"},
        at=datetime.now(UTC).isoformat(),
    )

    with pytest.raises(InvalidEvent, match="run-b"):
        replay([first, forged])


def test_cli_status_reports_corrupted_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, path = build_log(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM events WHERE run_id = ? AND seq = 3", (RUN,))
    conn.commit()
    conn.close()

    code = cli.main(["status", "--run-id", RUN, "--db", str(path)])

    assert code == 2
    assert "事件日志不完整" in capsys.readouterr().out


def test_concurrent_read_does_not_report_corruption(tmp_path: Path) -> None:
    """一边写一边读：两条 SELECT 必须在同一个只读事务里，否则会误报缺尾。"""
    db = tmp_path / "events.db"
    writer = subprocess.Popen(
        [
            sys.executable,
            str(PROBE),
            "writer",
            "--db",
            str(db),
            "--count",
            "600",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.2)
        reader = subprocess.run(
            [
                sys.executable,
                str(PROBE),
                "reader",
                "--db",
                str(db),
                "--count",
                "600",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        writer.communicate(timeout=120)

    assert writer.returncode == 0
    assert reader.returncode == 0, reader.stdout + reader.stderr
    assert "corrupted=0" in reader.stdout


def test_encoded_body_can_be_written_by_hand(tmp_path: Path) -> None:
    """跨进程测试常要手工造记录：编码函数必须能把事件原样写进 body 列。"""
    path = tmp_path / "events.db"
    log = SqliteEventLog(path)
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    hand = Event(
        run_id=RUN,
        seq=2,
        type=EventType.RUN_FAILED,
        payload={"reason": "手工写入"},
        at=datetime.now(UTC).isoformat(),
    )
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO events (run_id, seq, body) VALUES (?, ?, ?)",
        (RUN, hand.seq, encode_event(hand)),
    )
    conn.execute("UPDATE runs SET last_seq = ? WHERE run_id = ?", (hand.seq, RUN))
    conn.commit()
    conn.close()

    assert [event.seq for event in SqliteEventLog(path).read(RUN)] == [1, 2]
