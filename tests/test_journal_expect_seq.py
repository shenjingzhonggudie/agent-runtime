"""比较并追加：``expect_seq`` 的契约。

它补的是「读视图 → 写完」之间的那个窗口：两个请求都读到 ``last_seq = 1``，都认为
自己该写第 2 条。``expect_seq`` 把这个判断压进写入事务，晚到的那次得到 ``Conflict``，
而不是让日志长出两个「下一步」。

它不是悲观锁：不阻止别人写，只保证「基于我看见的那一版」这句承诺成立。所以拿到
``Conflict`` 之后该重读再判断，而不是把同一个 payload 原样重投。
"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentrun.events import EventType
from agentrun.journal import Conflict, EventLog, MemoryEventLog, SqliteEventLog

RUN = "run-1"
STARTED = {"task": "向 bob 转 250", "max_steps": 3}
FOLLOW = {"reason": "业务拒绝"}


@pytest.fixture(params=["memory", "sqlite"])
def log(request: pytest.FixtureRequest, tmp_path: Path) -> EventLog:
    """两个实现跑同一组契约：差异只能在存储上，不能在语义上。"""
    if request.param == "memory":
        return MemoryEventLog()
    return SqliteEventLog(tmp_path / "events.db")


def test_expect_seq_zero_starts_a_new_run(log: EventLog) -> None:
    assert log.append(RUN, EventType.RUN_STARTED, STARTED, expect_seq=0).seq == 1


def test_expect_seq_zero_rejected_when_run_exists(log: EventLog) -> None:
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    with pytest.raises(Conflict):
        log.append(RUN, EventType.RUN_FAILED, FOLLOW, expect_seq=0)

    assert len(log.read(RUN)) == 1


def test_expect_seq_matching_last_seq_is_accepted(log: EventLog) -> None:
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    assert log.append(RUN, EventType.RUN_FAILED, FOLLOW, expect_seq=1).seq == 2


def test_expect_seq_ahead_of_reality_is_rejected(log: EventLog) -> None:
    """期望值比现实大也一样冲突：说明写入者看到的是另一条时间线。"""
    with pytest.raises(Conflict):
        log.append(RUN, EventType.RUN_STARTED, STARTED, expect_seq=3)


def test_conflict_does_not_consume_seq(log: EventLog) -> None:
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    with pytest.raises(Conflict):
        log.append(RUN, EventType.RUN_FAILED, FOLLOW, expect_seq=9)

    assert log.append(RUN, EventType.RUN_FAILED, FOLLOW).seq == 2


def test_omitting_expect_seq_skips_the_check(log: EventLog) -> None:
    """省略即不检查：既有调用点一个都不用改。"""
    log.append(RUN, EventType.RUN_STARTED, STARTED)
    log.append(RUN, EventType.RUN_FAILED, FOLLOW)

    assert len(log.read(RUN)) == 2


def test_expect_seq_check_comes_before_start_check(log: EventLog) -> None:
    """两条理由同时成立时先报冲突：版本不对，先别谈意图。"""
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    with pytest.raises(Conflict):
        log.append(RUN, EventType.RUN_STARTED, STARTED, expect_seq=0)


# --- SQLite：回滚与并发 ---------------------------------------------------


def test_conflict_leaves_last_seq_untouched(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    log = SqliteEventLog(path)
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    with pytest.raises(Conflict):
        log.append(RUN, EventType.RUN_FAILED, FOLLOW, expect_seq=0)

    with sqlite3.connect(path) as conn:
        meta = conn.execute("SELECT last_seq FROM runs WHERE run_id = ?", (RUN,)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM events WHERE run_id = ?", (RUN,)).fetchone()
    assert meta == (1,)
    assert total == (1,)


def test_concurrent_writers_with_same_expect_seq_admit_one(tmp_path: Path) -> None:
    """两个线程都基于 ``last_seq = 1`` 追加：只能有一个落地。"""
    path = tmp_path / "events.db"
    SqliteEventLog(path).append(RUN, EventType.RUN_STARTED, STARTED)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt() -> None:
        log = SqliteEventLog(path)
        barrier.wait()
        try:
            log.append(RUN, EventType.RUN_FAILED, FOLLOW, expect_seq=1)
        except Conflict:
            outcomes.append("conflict")
        else:
            outcomes.append("written")

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt) for _ in range(2)]:
            future.result()

    assert sorted(outcomes) == ["conflict", "written"]
    assert len(SqliteEventLog(path).read(RUN)) == 2
