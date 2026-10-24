"""内存事件日志契约测试：序号连续、按 run 隔离、只追加。"""

from agentrun.events import EventType
from agentrun.journal import MemoryEventLog


def test_seq_starts_at_one_and_increments() -> None:
    log = MemoryEventLog()

    first = log.append("run-1", EventType.RUN_STARTED, {"task": "t", "max_steps": 1})
    second = log.append("run-1", EventType.RUN_EXHAUSTED, {"max_steps": 1})

    assert (first.seq, second.seq) == (1, 2)


def test_runs_are_isolated() -> None:
    log = MemoryEventLog()
    log.append("run-1", EventType.RUN_STARTED, {"task": "a", "max_steps": 1})
    log.append("run-2", EventType.RUN_STARTED, {"task": "b", "max_steps": 1})

    assert [event.seq for event in log.read("run-1")] == [1]
    assert [event.seq for event in log.read("run-2")] == [1]
    assert [event.payload["task"] for event in log.read("run-1")] == ["a"]


def test_payload_is_copied_on_write() -> None:
    log = MemoryEventLog()
    payload = {"task": "原始", "max_steps": 1}
    log.append("run-1", EventType.RUN_STARTED, payload)

    payload["task"] = "被调用方改掉"

    assert log.read("run-1")[0].payload["task"] == "原始"


def test_unknown_run_reads_empty() -> None:
    assert MemoryEventLog().read("never-started") == ()
