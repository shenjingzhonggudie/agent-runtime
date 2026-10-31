"""同一个任务只能启动一次：日志写入口的契约。

三条要求：

1. 第二次 ``RUN_STARTED`` 被 ``RunAlreadyStarted`` 拒绝，日志里仍然只有一条。
2. 拒绝之后 ``last_seq`` 不变：下一条事件仍拿到正确的 ``seq``，不留缺口。
3. ``acquire`` 先建的空记录（``last_seq = 0``）不算启动，第一次启动照常允许。

第 3 条是这条规则唯一容易写错的地方：判据是「有没有事件」，不是「``runs`` 里有没有
这一行」。按行判断会把每一次合法启动都挡在门外，因为取租约总是先插一行。

检查与追加必须原子：先查后写分成两步时，两个进程会同时读到 0，然后各写一条。
"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentrun import cli
from agentrun.engine import Engine
from agentrun.events import EventType
from agentrun.journal import (
    EventLog,
    LeaseLost,
    MemoryEventLog,
    RunAlreadyStarted,
    SqliteEventLog,
)
from agentrun.model import RunView
from agentrun.policy import Action, Finish
from agentrun.tools import ToolRegistry

RUN = "run-1"
STARTED = {"task": "向 bob 转 250", "max_steps": 3}


@pytest.fixture(params=["memory", "sqlite"])
def log(request: pytest.FixtureRequest, tmp_path: Path) -> EventLog:
    """两个实现跑同一组契约：差异只能在存储上，不能在规则上。"""
    if request.param == "memory":
        return MemoryEventLog()
    return SqliteEventLog(tmp_path / "events.db")


class ImmediatePolicy:
    """立刻结束任务，把焦点留在写 ``RUN_STARTED`` 那一步。"""

    def decide(self, view: RunView) -> Action:
        """不读视图，直接给结论。"""
        return Finish("完成")


# --- 两个实现的共同契约 ---------------------------------------------------


def test_first_start_is_allowed(log: EventLog) -> None:
    assert log.append(RUN, EventType.RUN_STARTED, STARTED).seq == 1


def test_second_start_is_rejected(log: EventLog) -> None:
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    with pytest.raises(RunAlreadyStarted):
        log.append(RUN, EventType.RUN_STARTED, STARTED)

    assert [item.type for item in log.read(RUN)] == [EventType.RUN_STARTED]


def test_rejected_start_does_not_consume_seq(log: EventLog) -> None:
    """拒绝要干净：下一条事件仍拿到 2，不能因为失败过一次就出现缺口。"""
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    with pytest.raises(RunAlreadyStarted):
        log.append(RUN, EventType.RUN_STARTED, STARTED)

    assert log.append(RUN, EventType.RUN_FAILED, {"reason": "业务拒绝"}).seq == 2


def test_other_events_are_not_affected(log: EventLog) -> None:
    """限制只针对开始事件：别的类型该写就写，不看启动次数。"""
    log.append(RUN, EventType.RUN_STARTED, STARTED)
    log.append(RUN, EventType.RUN_FAILED, {"reason": "业务拒绝"})
    log.append(RUN, EventType.RUN_FAILED, {"reason": "又来一次"})

    assert len(log.read(RUN)) == 3


# --- SQLite：事务与租约交互 ------------------------------------------------


def test_rejected_start_leaves_last_seq_untouched(tmp_path: Path) -> None:
    """直接查库：回滚之后 ``last_seq`` 与事件条数都还是 1。"""
    path = tmp_path / "events.db"
    log = SqliteEventLog(path)
    log.append(RUN, EventType.RUN_STARTED, STARTED)

    with pytest.raises(RunAlreadyStarted):
        log.append(RUN, EventType.RUN_STARTED, STARTED)

    with sqlite3.connect(path) as conn:
        meta = conn.execute("SELECT last_seq FROM runs WHERE run_id = ?", (RUN,)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM events WHERE run_id = ?", (RUN,)).fetchone()
    assert meta == (1,)
    assert total == (1,)


def test_empty_run_from_acquire_does_not_block_first_start(tmp_path: Path) -> None:
    """取租约先插入 ``last_seq = 0`` 的空记录，此时任务还没有开始。"""
    log = SqliteEventLog(tmp_path / "events.db")
    lease = log.acquire(RUN, "worker-a", 30.0)
    assert lease is not None

    event = log.append(RUN, EventType.RUN_STARTED, STARTED, lease=lease)

    assert event.seq == 1
    assert log.read(RUN)[0].type is EventType.RUN_STARTED


def test_second_connection_sees_the_first_start(tmp_path: Path) -> None:
    """判断来自库，不是进程内的缓存：换个连接照样拒绝。"""
    path = tmp_path / "events.db"
    SqliteEventLog(path).append(RUN, EventType.RUN_STARTED, STARTED)

    with pytest.raises(RunAlreadyStarted):
        SqliteEventLog(path).append(RUN, EventType.RUN_STARTED, STARTED)


def test_concurrent_starts_admit_exactly_one(tmp_path: Path) -> None:
    """两个线程同时启动：``BEGIN IMMEDIATE`` 让它们串行，只能有一个写成。"""
    path = tmp_path / "events.db"
    SqliteEventLog(path).read(RUN)  # 先建表，避免两个线程争 DDL

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt() -> None:
        log = SqliteEventLog(path)
        barrier.wait()
        try:
            log.append(RUN, EventType.RUN_STARTED, STARTED)
        except RunAlreadyStarted:
            outcomes.append("rejected")
        else:
            outcomes.append("written")

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt) for _ in range(2)]:
            future.result()

    assert sorted(outcomes) == ["rejected", "written"]
    assert len(SqliteEventLog(path).read(RUN)) == 1


# --- 错误优先级 -----------------------------------------------------------


@pytest.fixture
def started_and_leased(log: EventLog) -> EventLog:
    """任务已启动，且租约在别人手上：两条拒绝理由同时成立。"""
    lease = log.acquire(RUN, "worker-a", 30.0)
    assert lease is not None
    log.append(RUN, EventType.RUN_STARTED, STARTED, lease=lease)
    return log


def test_lease_check_comes_before_start_check(started_and_leased: EventLog) -> None:
    """先报租约，再报重复启动：身份的判断先于意图的判断。

    顺序一旦调换，调用方收到的异常就会变成 ``RunAlreadyStarted``——它以为问题是
    「任务已存在」，其实是「你根本不该写这个库」。
    """
    with pytest.raises(LeaseLost):
        started_and_leased.append(RUN, EventType.RUN_STARTED, STARTED)


# --- 调用方：引擎与 CLI ---------------------------------------------------


def test_engine_run_twice_is_rejected(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    engine = Engine(ImmediatePolicy(), ToolRegistry(), log)

    engine.run(RUN, "测试任务", 5)

    with pytest.raises(RunAlreadyStarted):
        engine.run(RUN, "测试任务", 5)

    assert [item.type for item in log.read(RUN)] == [
        EventType.RUN_STARTED,
        EventType.RUN_SUCCEEDED,
    ]


def test_engine_resume_still_works_after_a_refused_restart(tmp_path: Path) -> None:
    """被拒绝的是「重新启动」，不是「接着跑」：resume 照常可用。"""
    log = SqliteEventLog(tmp_path / "events.db")
    engine = Engine(ImmediatePolicy(), ToolRegistry(), log)
    engine.run(RUN, "测试任务", 5)

    with pytest.raises(RunAlreadyStarted):
        engine.run(RUN, "测试任务", 5)

    assert engine.resume(RUN).stop_reason.value == "SUCCEEDED"


def test_cli_run_twice_reports_existing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = tmp_path / "s.json"
    script.write_text('[{"finish": "完成"}]', encoding="utf-8")
    argv = [
        "run",
        "--run-id",
        RUN,
        "--script",
        str(script),
        "--db",
        str(tmp_path / "events.db"),
        "--ledger",
        str(tmp_path / "ledger"),
    ]

    assert cli.main(argv) == 0
    capsys.readouterr()

    assert cli.main(argv) == 2
    printed = capsys.readouterr().out
    assert "任务已存在" in printed
    assert "resume" in printed
    # 只提示，不替调用方读状态：run 的输出语义不能因为撞上已存在的任务就变成 status
    assert "状态：" not in printed
