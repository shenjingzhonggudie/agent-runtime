"""事件哈希链：让日志自证「没被改过」（19B）。

``read()`` 已经能发现缺号、末尾被删与归属错乱，但它有一个盲区：把中间一条删掉、
把后面的 ``seq`` 依次前移、再把 ``runs.last_seq`` 一起改小，序列就重新自洽了——
``read()`` 会认为一切正常。哈希链补的正是这个洞：每条的摘要覆盖自身内容与上一条的
摘要，删掉任何一条都会让后面的 ``prev_hash`` 对不上。

迁移前的记录没有摘要，无法自证。校验会跳过它们并如实报告跳过了多少条：那不算通过，
也不算篡改，是第三件事。
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentrun import cli
from agentrun.codec import encode_event
from agentrun.events import Event, EventType
from agentrun.journal import SqliteEventLog

RUN = "run-1"
AT = "2026-10-31T00:00:00Z"


def run_sql(path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    """直接改库，模拟一个有写权限的人动了手脚。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def seed(log: SqliteEventLog) -> None:
    """写三条语义合法、可以正常重放的事件。"""
    log.append(RUN, EventType.RUN_STARTED, {"task": "向 bob 转 250", "max_steps": 3})
    log.append(
        RUN,
        EventType.STEP_DECIDED,
        {"step": 1, "tool": "transfer", "args": {"amount": 250}, "step_key": f"{RUN}:1"},
    )
    log.append(RUN, EventType.STEP_STARTED, {"step": 1, "attempt": 1})


@pytest.fixture()
def seeded(tmp_path: Path) -> tuple[Path, SqliteEventLog]:
    """一个写完三条事件、链完好的库。"""
    path = tmp_path / "events.db"
    log = SqliteEventLog(path)
    seed(log)
    return path, log


# --- 完好 -----------------------------------------------------------------


def test_fresh_log_passes(seeded: tuple[Path, SqliteEventLog]) -> None:
    _, log = seeded

    report = log.verify_chain(RUN)

    assert report.ok is True
    assert report.total == 3
    assert report.hashed == 3


def test_first_event_has_no_prev_hash(seeded: tuple[Path, SqliteEventLog]) -> None:
    path, _ = seeded

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT prev_hash, hash FROM events WHERE run_id = ? AND seq = 1", (RUN,)
        ).fetchone()
    finally:
        conn.close()

    assert row[0] is None
    assert isinstance(row[1], str) and len(row[1]) == 64


# --- 篡改 -----------------------------------------------------------------


def test_tampered_body_is_caught(seeded: tuple[Path, SqliteEventLog]) -> None:
    path, log = seeded
    run_sql(path, "UPDATE events SET body = ? WHERE run_id = ? AND seq = 2", ('{"evil": 1}', RUN))

    report = log.verify_chain(RUN)

    assert report.first_break == 2
    assert report.reason is not None and "摘要" in report.reason


def test_deleted_middle_event_with_fixed_last_seq_is_caught(
    seeded: tuple[Path, SqliteEventLog],
) -> None:
    """删掉中间一条，把后面那条重写成合法的 seq，再改小 ``last_seq``。

    这是 ``read()`` 抓不到的破坏：序列连续、内容和 seq 列一致、能正常重放，只是
    「事实」少了一段。哈希链补的就是这个洞。
    """
    path, log = seeded
    survivor = log.read(RUN)[2]
    rewritten = Event(survivor.run_id, 2, survivor.type, dict(survivor.payload), survivor.at)
    run_sql(path, "DELETE FROM events WHERE run_id = ? AND seq = 2", (RUN,))
    run_sql(
        path,
        "UPDATE events SET seq = 2, body = ? WHERE run_id = ? AND seq = 3",
        (encode_event(rewritten), RUN),
    )
    run_sql(path, "UPDATE runs SET last_seq = 2 WHERE run_id = ?", (RUN,))

    assert [item.seq for item in log.read(RUN)] == [1, 2]  # 读侧认为一切正常

    report = log.verify_chain(RUN)

    assert report.first_break == 2
    assert report.reason is not None and "摘要" in report.reason


def test_reordered_events_are_caught(seeded: tuple[Path, SqliteEventLog]) -> None:
    """把两条的正文换位置，``seq`` 列不动：两条的摘要都会对不上。"""
    path, log = seeded
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT body FROM events WHERE run_id = ? AND seq IN (2, 3) ORDER BY seq", (RUN,)
        ).fetchall()
        conn.execute("UPDATE events SET body = ? WHERE run_id = ? AND seq = 2", (rows[1][0], RUN))
        conn.execute("UPDATE events SET body = ? WHERE run_id = ? AND seq = 3", (rows[0][0], RUN))
        conn.commit()
    finally:
        conn.close()

    assert log.verify_chain(RUN).first_break == 2


# --- 迁移 -----------------------------------------------------------------


def build_legacy(path: Path) -> None:
    """造一个加列之前的库：``events`` 没有 ``prev_hash`` / ``hash``。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL, "
            "owner TEXT, epoch INTEGER NOT NULL DEFAULT 0, lease_expires_at REAL)"
        )
        conn.execute(
            "CREATE TABLE events (run_id TEXT NOT NULL, seq INTEGER NOT NULL, "
            "body TEXT NOT NULL, PRIMARY KEY (run_id, seq))"
        )
        conn.execute("INSERT INTO runs (run_id, last_seq) VALUES (?, 2)", (RUN,))
        for seq in (1, 2):
            kind = EventType.RUN_STARTED if seq == 1 else EventType.RUN_FAILED
            payload = {"task": "旧任务", "max_steps": 1} if seq == 1 else {"reason": "旧原因"}
            body = encode_event(Event(RUN, seq, kind, payload, AT))
            conn.execute(
                "INSERT INTO events (run_id, seq, body) VALUES (?, ?, ?)", (RUN, seq, body)
            )
        conn.commit()
    finally:
        conn.close()


def test_legacy_database_migrates_and_still_reads(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    build_legacy(path)

    log = SqliteEventLog(path)

    assert [item.seq for item in log.read(RUN)] == [1, 2]
    report = log.verify_chain(RUN)
    assert report.total == 2
    assert report.hashed == 0  # 迁移不给历史数据补封条
    assert report.ok is True


def test_writes_after_migration_start_a_new_segment(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    build_legacy(path)
    log = SqliteEventLog(path)

    log.append(RUN, EventType.STEP_DECIDED, {"step": 1, "tool": "x", "args": {}, "step_key": "k"})

    report = log.verify_chain(RUN)
    assert report.total == 3
    assert report.hashed == 1  # 只有迁移之后写的那条能自证
    assert report.ok is True


def test_legacy_record_inserted_mid_chain_is_caught(seeded: tuple[Path, SqliteEventLog]) -> None:
    """链中间插进一条无摘要的记录：说明有人绕过 append 写库。"""
    path, log = seeded
    run_sql(
        path, "UPDATE events SET prev_hash = NULL, hash = NULL WHERE run_id = ? AND seq = 2", (RUN,)
    )

    report = log.verify_chain(RUN)

    assert report.first_break == 2
    assert report.reason is not None and "无摘要" in report.reason


# --- CLI ------------------------------------------------------------------


def test_cli_verify_reports_clean_chain(
    seeded: tuple[Path, SqliteEventLog], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = seeded

    assert cli.main(["verify", "--db", str(path)]) == 0
    assert "链完好" in capsys.readouterr().out


def test_cli_verify_reports_first_break(
    seeded: tuple[Path, SqliteEventLog], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = seeded
    run_sql(path, "UPDATE events SET body = ? WHERE run_id = ? AND seq = 3", ('{"evil": 1}', RUN))

    assert cli.main(["verify", "--db", str(path)]) == 1
    printed = capsys.readouterr().out
    assert "断点在 seq 3" in printed


def test_cli_verify_without_runs_exits_two(tmp_path: Path) -> None:
    assert cli.main(["verify", "--db", str(tmp_path / "empty.db")]) == 2


def test_cli_verify_legacy_says_it_cannot_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "legacy.db"
    build_legacy(path)

    assert cli.main(["verify", "--db", str(path)]) == 0
    assert "没有摘要" in capsys.readouterr().out
