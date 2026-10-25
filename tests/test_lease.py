"""租约与 epoch 栅栏：谁能写、谁必须停手。"""

import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agentrun import cli
from agentrun.engine import Engine, StopReason
from agentrun.events import EventType
from agentrun.journal import LeaseBusy, LeaseLost, MemoryEventLog, SqliteEventLog
from agentrun.model import RunStatus, replay
from agentrun.policy import Finish, ScriptedPolicy, ToolCall
from agentrun.tools import SideEffect, ToolContext, ToolRegistry

RUN = "r1"

# 接管用例的租约时长与工具体内的等待。
#
# 不能把 TTL 压到「够触发过期」就完事：本机一次「续租 + 追加」实测 p50 约 30ms、
# p99 约 70ms、最坏 100ms（见 ``scripts/_diag_lease_timing.py``）。TTL 若与这个量级
# 同阶（曾用 0.05），worker 会在工具真正跑起来之前就把自己判死，用例随机失败。
# 所以 TTL 取 0.5s，等待取 1.0s：留给接管 500ms 余量，约为最坏事务耗时的 5 倍。
TTL = 0.5
HOLD = 1.0


def test_same_owner_keeps_epoch_and_other_owner_is_blocked() -> None:
    log = MemoryEventLog()
    first = log.acquire(RUN, "w1", 30)
    assert first is not None

    again = log.acquire(RUN, "w1", 30)
    assert again is not None
    assert again.epoch == first.epoch
    assert log.acquire(RUN, "w2", 30) is None

    log.release(first)
    taken = log.acquire(RUN, "w2", 30)
    assert taken is not None
    assert taken.epoch == 2


def test_unfenced_write_is_rejected_while_leased(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    lease = log.acquire(RUN, "w1", 30)
    assert lease is not None

    with pytest.raises(LeaseLost):
        log.append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 3})

    log.append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 3}, lease=lease)
    assert len(log.read(RUN)) == 1


def test_stale_epoch_is_rejected_after_takeover(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    first = log.acquire(RUN, "w1", 0.01)
    assert first is not None
    time.sleep(0.02)
    second = log.acquire(RUN, "w2", 30)
    assert second is not None
    assert second.epoch == first.epoch + 1

    with pytest.raises(LeaseLost):
        log.append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 3}, lease=first)
    log.append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 3}, lease=second)
    assert len(log.read(RUN)) == 1


def test_renew_after_expiry_loses_lease(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    lease = log.acquire(RUN, "w1", 0.01)
    assert lease is not None
    time.sleep(0.02)

    with pytest.raises(LeaseLost):
        log.renew(lease, 30)


def test_old_schema_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)")
    # last_seq 必须与实际事件条数一致，否则按新契约属于「日志不完整」：
    # 这里没有事件，所以 last_seq 只能是 0。
    conn.execute("INSERT INTO runs (run_id, last_seq) VALUES (?, 0)", (RUN,))
    conn.commit()
    conn.close()

    log = SqliteEventLog(path)

    assert log.read(RUN) == ()
    lease = log.acquire(RUN, "w1", 30)
    assert lease is not None
    assert lease.epoch == 1


def test_engine_refuses_when_lease_is_busy(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    assert log.acquire(RUN, "w1", 30) is not None
    engine = Engine(ScriptedPolicy([Finish("不该执行")]), ToolRegistry(), log, owner="w2")

    with pytest.raises(LeaseBusy):
        engine.run(RUN, "t", 3)


class SilentTool:
    """返回非映射，让引擎停在 UNCERTAIN（既不成功也不确定失败）。"""

    name = "silent"
    side_effect = SideEffect.READ_ONLY

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """永远通过。"""

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """返回 None：run_tool 会把它归为 UNCERTAIN。"""


class ReleaseFails(SqliteEventLog):
    """归还租约时抛 ``LeaseLost``，用来验证「归还不改变结局」。"""

    def release(self, lease: object) -> None:
        """模拟「归还时发现租约已经不属于自己」。"""
        raise LeaseLost("模拟归还失败")


class TakeoverEcho:
    """在工具执行期间完成接管：确定性复现「旧 worker 醒来时已无权写入」。"""

    name = "echo"
    side_effect = SideEffect.READ_ONLY

    def __init__(self, log: SqliteEventLog) -> None:
        self._log = log
        self.new_lease = None

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """永远通过。"""

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        """等租约过期，由 w2 接管，再返回。"""
        time.sleep(HOLD)
        self.new_lease = self._log.acquire(RUN, "w2", 30)
        return {"ok": True}


def test_finished_run_gives_the_lease_back(tmp_path: Path) -> None:
    """任务干净收尾后，别的 worker 应当能立刻接手，而不是等 TTL 过期。"""
    log = SqliteEventLog(tmp_path / "events.db")
    engine = Engine(
        ScriptedPolicy([Finish("完成")]),
        ToolRegistry(),
        log,
        owner="w1",
        lease_ttl=30,
    )

    result = engine.run(RUN, "t", 3)
    assert result.stop_reason is StopReason.SUCCEEDED

    lease = log.acquire(RUN, "w2", 30)
    assert lease is not None
    assert lease.epoch == 2


def test_uncertain_stop_gives_the_lease_back(tmp_path: Path) -> None:
    """停在 UNCERTAIN 时归还租约：这一步本来就该由接手的人去对账。"""
    log = SqliteEventLog(tmp_path / "events.db")
    registry = ToolRegistry()
    registry.register(SilentTool())
    engine = Engine(
        ScriptedPolicy([ToolCall("silent", {})]),
        registry,
        log,
        owner="w1",
        lease_ttl=30,
    )

    result = engine.run(RUN, "t", 3)
    assert result.stop_reason is StopReason.UNCERTAIN

    lease = log.acquire(RUN, "w2", 30)
    assert lease is not None
    assert lease.epoch == 2


def test_lost_worker_does_not_touch_the_new_owners_lease(tmp_path: Path) -> None:
    """被接管的 worker 收尾时不能「归还」别人的租约。"""
    log = SqliteEventLog(tmp_path / "events.db")
    tool = TakeoverEcho(log)
    registry = ToolRegistry()
    registry.register(tool)
    old = Engine(
        ScriptedPolicy([ToolCall("echo", {}), Finish("旧 worker 完成")]),
        registry,
        log,
        owner="w1",
        lease_ttl=TTL,
    )

    assert old.run(RUN, "t", 3).stop_reason is StopReason.LOST

    # w2 的租约还在：同一 owner 再取，epoch 不涨，也不是 None
    again = log.acquire(RUN, "w2", 30)
    assert again is not None
    assert again.epoch == 2


def test_lease_of_reports_holder_and_clears_after_release(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    assert log.lease_of(RUN) is None

    lease = log.acquire(RUN, "w1", 30)
    assert lease is not None
    current = log.lease_of(RUN)
    assert current is not None
    assert (current.owner, current.epoch) == ("w1", 1)

    log.release(lease)
    assert log.lease_of(RUN) is None


def test_status_shows_who_holds_the_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "events.db"
    log = SqliteEventLog(db)
    log.append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 2})
    assert log.acquire(RUN, "w9", 30) is not None

    assert cli.main(["status", "--run-id", RUN, "--db", str(db)]) == 0
    assert "租约：w9（epoch 1）" in capsys.readouterr().out


def test_status_marks_expired_lease(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """过期只说明"可以接管"，不等于"对方已经停下"——输出要区分这两件事。"""
    db = tmp_path / "events.db"
    log = SqliteEventLog(db)
    log.append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 2})
    assert log.acquire(RUN, "w9", 0.001) is not None

    assert cli.main(["status", "--run-id", RUN, "--db", str(db)]) == 0
    assert "已过期（可被接管）" in capsys.readouterr().out


def test_write_after_release_is_refused(tmp_path: Path) -> None:
    """白盒：归还之后引擎不许再写——此时 `runs.owner` 已清空，日志不会再拦。"""
    log = SqliteEventLog(tmp_path / "events.db")
    engine = Engine(ScriptedPolicy([]), ToolRegistry(), log, owner="w1", lease_ttl=30)
    engine._acquire(RUN)  # noqa: SLF001 - 这条守的是引擎内部的不变量
    engine._release()  # noqa: SLF001

    with pytest.raises(LeaseLost):
        engine._append(RUN, EventType.RUN_STARTED, {"task": "t", "max_steps": 1})  # noqa: SLF001


def test_failing_release_does_not_change_the_result(tmp_path: Path) -> None:
    """归还失败只意味着别人要多等一会儿，不能把成功改写成异常。"""
    log = ReleaseFails(tmp_path / "events.db")
    engine = Engine(
        ScriptedPolicy([Finish("完成")]),
        ToolRegistry(),
        log,
        owner="w1",
        lease_ttl=30,
    )

    result = engine.run(RUN, "t", 3)
    assert result.stop_reason is StopReason.SUCCEEDED


def test_takeover_mid_call_stops_the_old_worker(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    tool = TakeoverEcho(log)
    registry = ToolRegistry()
    registry.register(tool)
    old = Engine(
        ScriptedPolicy([ToolCall("echo", {}), Finish("旧 worker 完成")]),
        registry,
        log,
        owner="w1",
        lease_ttl=TTL,
    )

    result = old.run(RUN, "t", 3)

    assert tool.new_lease is not None
    assert tool.new_lease.epoch == 2
    assert result.stop_reason is StopReason.LOST
    assert replay(log.read(RUN)).status is RunStatus.RUNNING
    # 旧 worker 的结果写入被拒绝：日志停在「已开始、没有结果」
    assert [event.type for event in log.read(RUN)] == [
        EventType.RUN_STARTED,
        EventType.STEP_DECIDED,
        EventType.STEP_STARTED,
    ]
