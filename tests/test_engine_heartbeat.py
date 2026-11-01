"""心跳：长工具调用期间租约不该过期（19C）。

没有心跳时，租约只靠「写前续租」维持。一次耗时超过 ``ttl`` 的工具调用会让另一个
worker 在它**还在执行**的时候抢到租约——接管者看到 ``STEP_STARTED`` 却没有结果，
只能把这一步判成「不知道有没有生效」，而实际上它正跑到一半。心跳把这段空白填上。

心跳失败也不等于工具失败：调用已经发出去了，半途掐断只会留下一个说不清的副作用。
引擎只置 ``lost``，等工具返回后由栅栏拒绝下一次写入。
"""

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentrun.engine import Engine, StopReason
from agentrun.journal import Lease, SqliteEventLog
from agentrun.model import RunStatus
from agentrun.policy import Finish, ScriptedPolicy, ToolCall
from agentrun.tools import SideEffect, ToolContext, ToolRegistry

RUN = "run-1"
TTL = 0.5
CALL_SECONDS = 0.8  # 明显长于 TTL：没有心跳就一定会过期


class SlowTool:
    """跑 ``seconds`` 秒，期间探一次「别人能不能抢到租约」。

    Attributes:
        probe: 每次执行追加一次 ``acquire`` 的结果；``None`` 表示租约仍被占着。
    """

    name = "slow"
    side_effect = SideEffect.READ_ONLY

    def __init__(self, log: SqliteEventLog, seconds: float) -> None:
        self._log = log
        self._seconds = seconds
        self.probe: list[Lease | None] = []

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        """不要求参数。"""

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        """睡够时间之后，让别人试一次抢租约。"""
        time.sleep(self._seconds)
        self.probe.append(self._log.acquire(RUN, "other-worker", TTL))
        return {"ok": True}


def build(tmp_path: Path) -> tuple[SqliteEventLog, SlowTool, Engine]:
    """装配一个带慢工具、启用了租约的引擎。"""
    log = SqliteEventLog(tmp_path / "events.db")
    tool = SlowTool(log, CALL_SECONDS)
    registry = ToolRegistry()
    registry.register(tool)
    engine = Engine(
        ScriptedPolicy([ToolCall("slow", {}), Finish("完成")]),
        registry,
        log,
        owner="worker-a",
        lease_ttl=TTL,
    )
    return log, tool, engine


def test_heartbeat_keeps_the_lease_during_a_long_call(tmp_path: Path) -> None:
    """调用时长超过 TTL 时，别人仍然抢不到。"""
    _, tool, engine = build(tmp_path)

    result = engine.run(RUN, "测试任务", 5)

    assert result.stop_reason is StopReason.SUCCEEDED
    assert tool.probe == [None]


def test_without_heartbeat_the_lease_is_taken(tmp_path: Path, no_heartbeat: None) -> None:
    """心跳缺席时：同一个慢调用就会让别人接管。"""
    _, tool, engine = build(tmp_path)

    engine.run(RUN, "测试任务", 5)

    assert isinstance(tool.probe[0], Lease)


def test_call_that_outlived_the_lease_ends_as_lost(tmp_path: Path, no_heartbeat: None) -> None:
    """租约被抢走之后，本次运行以 ``LOST`` 收场，任务状态仍是 ``RUNNING``。

    结局不能写成 ``UNCERTAIN``：那不是「不知道有没有生效」，而是「这个 worker 出局
    了」。接手的人会看到悬挂的 ``STEP_STARTED``，由对账或复核收敛。
    """
    _, tool, engine = build(tmp_path)

    result = engine.run(RUN, "测试任务", 5)

    assert isinstance(tool.probe[0], Lease)
    assert result.stop_reason is StopReason.LOST
    assert result.view.status is RunStatus.RUNNING
