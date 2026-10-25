"""SQLite 事件日志契约测试：seq 连续性、持久化读回、原子回滚、多实例并发。"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pytest

from agentrun.codec import DecodeError, EncodeError
from agentrun.events import EventType
from agentrun.journal import SqliteEventLog

RUN = "run-1"
STARTED = {"task": "转账 250", "max_steps": 10}


def make_log(tmp_path: Path) -> SqliteEventLog:
    """在临时目录下建一个日志实例。"""
    return SqliteEventLog(tmp_path / "events.db")


def test_database_file_is_created(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append(RUN, EventType.RUN_STARTED, STARTED)
    assert (tmp_path / "events.db").exists()


def test_parent_directory_is_created(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "nested" / "deeper" / "events.db")
    log.append(RUN, EventType.RUN_STARTED, STARTED)
    assert (tmp_path / "nested" / "deeper" / "events.db").exists()


def test_first_append_starts_at_seq_one(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    event = log.append(RUN, EventType.RUN_STARTED, STARTED)
    assert event.seq == 1


def test_sequential_appends_increment_seq(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    seqs = [
        log.append(RUN, EventType.STEP_STARTED, {"step": 1, "attempt": 1}).seq for _ in range(3)
    ]
    assert seqs == [1, 2, 3]


def test_append_returns_event_equal_to_stored(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    written = log.append(RUN, EventType.RUN_STARTED, STARTED)
    (restored,) = log.read(RUN)
    assert restored == written


def test_read_returns_empty_for_unknown_run(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    assert log.read("nope") == ()


def test_runs_are_isolated_by_run_id(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("a", EventType.RUN_STARTED, {"task": "a", "max_steps": 1})
    log.append("a", EventType.RUN_FAILED, {"reason": "x"})
    log.append("b", EventType.RUN_STARTED, {"task": "b", "max_steps": 1})

    assert [e.seq for e in log.read("a")] == [1, 2]
    assert [e.seq for e in log.read("b")] == [1]


def test_reopen_reads_previous_events(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    first = SqliteEventLog(path)
    written = first.append(RUN, EventType.RUN_STARTED, STARTED)

    reopened = SqliteEventLog(path)
    assert reopened.read(RUN) == (written,)
    assert reopened.append(RUN, EventType.RUN_FAILED, {"reason": "x"}).seq == 2


def test_read_preserves_nested_payload(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    payload = {"step": 2, "tool": "transfer", "args": {"src": "alice", "amount": 250}}
    log.append(RUN, EventType.STEP_DECIDED, payload)
    assert log.read(RUN)[0].payload == payload


def test_at_is_iso_utc(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    event = log.append(RUN, EventType.RUN_STARTED, STARTED)
    assert isinstance(event.at, str)
    assert datetime.fromisoformat(event.at).tzinfo is not None


def test_encode_failure_leaves_no_trace(tmp_path: Path) -> None:
    # 坏写入不能消耗 seq，否则下一次正常写入会出现缺口
    log = make_log(tmp_path)
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    with pytest.raises(EncodeError):
        log.append(RUN, EventType.STEP_STARTED, {"step": 1, "trace": (1, 2)})

    third = log.append(RUN, EventType.RUN_FAILED, {"reason": "x"})
    assert third.seq == 2
    assert [e.seq for e in log.read(RUN)] == [1, 2]


def test_bad_body_raises_decode_error(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    log = SqliteEventLog(path)
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    conn = sqlite3.connect(path)
    conn.execute("UPDATE events SET body = ? WHERE run_id = ?", ("{not json", RUN))
    conn.commit()
    conn.close()

    with pytest.raises(DecodeError):
        SqliteEventLog(path).read(RUN)


def test_concurrent_appends_do_not_duplicate_seq(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(5):
                log.append(RUN, EventType.STEP_STARTED, {"step": 1, "attempt": 1})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    seqs = [event.seq for event in log.read(RUN)]
    assert seqs == list(range(1, 21))
    assert len(set(seqs)) == 20
