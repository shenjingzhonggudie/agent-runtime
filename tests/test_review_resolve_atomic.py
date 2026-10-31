"""复核提交的原子性与幂等（19A）。

背景：两个「确认成功」同时到达时，第二条 ``REVIEW_RESOLVED`` 会被写进日志，此后
``replay`` 永远抛 ``InvalidEvent``，任务再也读不出来。这不是多一条记录的问题，
是整个任务作废。

三道防线，缺一不可：

1. 追加时带 ``expect_seq``：后到的写入撞 ``Conflict``，不落地。
2. 撞上之后重读一次，把「已经由谁处理」如实报出来，而不是装作无事发生。
3. 同一个 ``resolution_id`` 重复提交返回幂等结果，日志里始终只有一条结论。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentrun import cli
from agentrun.events import EventType
from agentrun.journal import SqliteEventLog
from agentrun.model import RunStatus, replay
from agentrun.reconcile import NothingToResolve, ResolveResult, resolve, resolve_review

RUN = "run-1"


def build_waiting_review(log: SqliteEventLog, run_id: str = RUN) -> None:
    """造一个停在 ``WAITING_REVIEW`` 的任务：第 1 步已开始、没有结果。"""
    log.append(run_id, EventType.RUN_STARTED, {"task": "向 bob 转 250", "max_steps": 5})
    log.append(
        run_id,
        EventType.STEP_DECIDED,
        {"step": 1, "tool": "transfer", "args": {"amount": 250}, "step_key": f"{run_id}:1"},
    )
    log.append(run_id, EventType.STEP_STARTED, {"step": 1, "attempt": 1})
    log.append(run_id, EventType.REVIEW_REQUESTED, {"step": 1, "reason": "调用结果未知"})


def count_resolved(log: SqliteEventLog, run_id: str = RUN) -> int:
    """日志里有几条复核结论。"""
    return sum(1 for item in log.read(run_id) if item.type is EventType.REVIEW_RESOLVED)


# --- 幂等键 ---------------------------------------------------------------


def test_same_resolution_id_writes_one_event(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    build_waiting_review(log)

    first = resolve_review(log, RUN, decision="abort", note="甲：业务放弃", resolution_id="r-1")
    second = resolve_review(log, RUN, decision="abort", note="甲：又点了一次", resolution_id="r-1")

    assert first.recorded is True and first.duplicate is False
    assert second.recorded is False and second.duplicate is True
    assert count_resolved(log) == 1


def test_duplicate_reports_the_first_decision(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    build_waiting_review(log)
    resolve_review(log, RUN, decision="not_applied", note="甲：账本无此笔", resolution_id="r-1")

    second = resolve_review(log, RUN, decision="abort", note="乙：也要终止", resolution_id="r-1")

    assert "not_applied" in second.detail


def test_different_id_after_resolution_reports_who_handled(tmp_path: Path) -> None:
    """不是重复，是晚了：必须告诉后来者「已经发生了什么」。"""
    log = SqliteEventLog(tmp_path / "events.db")
    build_waiting_review(log)
    resolve_review(log, RUN, decision="abort", note="甲：业务放弃", resolution_id="r-1")

    late = resolve_review(log, RUN, decision="abort", note="乙：也放弃", resolution_id="r-2")

    assert late.recorded is False
    assert late.duplicate is False
    assert "已被处理" in late.detail
    assert "abort" in late.detail
    assert count_resolved(log) == 1


def test_resolution_id_survives_replay(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    build_waiting_review(log)
    resolve_review(log, RUN, decision="abort", note="甲：业务放弃", resolution_id="r-1")

    view = replay(log.read(RUN))

    assert view.review is not None
    assert view.review.resolution_id == "r-1"
    assert view.review.resolved_at is not None


def test_blank_resolution_id_is_rejected(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    build_waiting_review(log)

    with pytest.raises(ValueError, match="resolution_id"):
        resolve_review(log, RUN, decision="abort", note="甲", resolution_id="   ")


def test_events_without_resolution_id_still_replay(tmp_path: Path) -> None:
    """旧日志（这一轮之前写的）没有这个字段，必须照常读回。"""
    log = SqliteEventLog(tmp_path / "events.db")
    build_waiting_review(log)
    log.append(
        RUN,
        EventType.REVIEW_RESOLVED,
        {"step": 1, "decision": "abort", "note": "旧版本写的结论"},
    )

    view = replay(log.read(RUN))

    assert view.review is not None
    assert view.review.decision == "abort"
    assert view.review.resolution_id is None


# --- 并发：日志不许被写坏 ------------------------------------------------


def test_two_resolvers_write_one_event(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    build_waiting_review(SqliteEventLog(path))

    barrier = threading.Barrier(2)
    outcomes: list[ResolveResult] = []

    def attempt(note: str) -> None:
        log = SqliteEventLog(path)
        barrier.wait()
        outcomes.append(resolve_review(log, RUN, decision="abort", note=note))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, "甲：放弃"), pool.submit(attempt, "乙：放弃")]
        for future in futures:
            future.result()

    assert count_resolved(SqliteEventLog(path)) == 1
    assert sorted(item.recorded for item in outcomes) == [False, True]
    assert replay(SqliteEventLog(path).read(RUN)).status is RunStatus.ABORTED


def test_concurrent_same_id_yields_a_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    build_waiting_review(SqliteEventLog(path))

    barrier = threading.Barrier(2)
    outcomes: list[ResolveResult] = []

    def attempt() -> None:
        log = SqliteEventLog(path)
        barrier.wait()
        outcomes.append(
            resolve_review(log, RUN, decision="abort", note="甲：放弃", resolution_id="r-1")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt) for _ in range(2)]:
            future.result()

    assert count_resolved(SqliteEventLog(path)) == 1
    assert sum(1 for item in outcomes if item.duplicate) == 1


def test_probe_scenario_no_longer_corrupts_the_log(tmp_path: Path) -> None:
    """把 ``scripts/_probe_double_resolve.py`` 的并发情形变成回归用例。"""
    log = SqliteEventLog(tmp_path / "events.db")
    build_waiting_review(log)
    resolve(log, RUN, decision="applied", note="甲：账本里有这笔", result={"ok": True})

    with pytest.raises(NothingToResolve):
        resolve(log, RUN, decision="applied", note="甲：又点了一次", result={"ok": True})

    view = replay(log.read(RUN))
    assert view.status is RunStatus.RUNNING
    assert count_resolved(log) == 1


# --- CLI：两种「没写进去」的退出码不同 ------------------------------------


def resolve_args(db: Path, *, resolution_id: str | None = None) -> list[str]:
    """构造一条 ``resolve`` 命令。"""
    argv = [
        "resolve",
        "--run-id",
        RUN,
        "--decision",
        "abort",
        "--note",
        "甲：业务放弃",
        "--db",
        str(db),
    ]
    if resolution_id is not None:
        argv.extend(["--resolution-id", resolution_id])
    return argv


def test_cli_duplicate_resolution_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "events.db"
    build_waiting_review(SqliteEventLog(db))
    argv = resolve_args(db, resolution_id="r-1")

    assert cli.main(argv) == 0
    capsys.readouterr()

    assert cli.main(argv) == 0
    assert "重复提交" in capsys.readouterr().out


def test_cli_late_resolution_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "events.db"
    build_waiting_review(SqliteEventLog(db))

    assert cli.main(resolve_args(db, resolution_id="r-1")) == 0
    capsys.readouterr()

    assert cli.main(resolve_args(db, resolution_id="r-2")) == 2
    printed = capsys.readouterr().out
    assert "已被处理" in printed
