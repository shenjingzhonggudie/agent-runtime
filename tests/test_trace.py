"""时间线与 JSONL 导出契约测试：只读、可读、可回读。"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentrun.codec import DecodeError, decode_event
from agentrun.events import Event, EventType
from agentrun.trace import TraceLine, export_jsonl, summarize, timeline

RUN = "run-1"
AT = "2026-10-25T00:00:00+00:00"


def ev(seq: int, type_: EventType, **payload: Any) -> Event:
    """构造一条测试事件。"""
    return Event(run_id=RUN, seq=seq, type=type_, payload=payload, at=AT)


def sample_events() -> list[Event]:
    """十一种事件各一条，覆盖全部 EventType。"""
    return [
        ev(1, EventType.RUN_STARTED, task="转账", max_steps=5),
        ev(
            2,
            EventType.STEP_DECIDED,
            step=1,
            tool="transfer",
            args={"src": "alice"},
            step_key="run-1:1",
        ),
        ev(3, EventType.STEP_STARTED, step=1, attempt=1),
        ev(4, EventType.STEP_COMPLETED, step=1, result={"ok": True}),
        ev(5, EventType.STEP_FAILED, step=2, error={"kind": "rejected", "message": "余额不足"}),
        ev(6, EventType.STEP_UNCERTAIN, step=3, reason="连接中断"),
        ev(7, EventType.REVIEW_REQUESTED, step=3, reason="账本查不到"),
        ev(8, EventType.REVIEW_RESOLVED, step=3, decision="applied", note="人工核对到记录"),
        ev(9, EventType.RUN_SUCCEEDED, answer="完成"),
        ev(10, EventType.RUN_FAILED, reason="配置错误"),
        ev(11, EventType.RUN_EXHAUSTED, max_steps=5),
    ]


def test_timeline_keeps_order_and_length() -> None:
    events = sample_events()
    lines = timeline(events)

    assert len(lines) == len(events)
    assert [line.seq for line in lines] == [event.seq for event in events]


def test_timeline_renders_every_event_type() -> None:
    for event in sample_events():
        text = summarize(event)
        assert text
        assert "None" not in text


def test_timeline_carries_kind_and_at() -> None:
    (line,) = timeline([ev(1, EventType.RUN_STARTED, task="t", max_steps=3)])
    assert isinstance(line, TraceLine)
    assert line.kind == "RUN_STARTED"
    assert line.at == AT


def test_summarize_tolerates_missing_fields() -> None:
    # 时间线是观察工具，遇到残缺事件仍要打得出来
    for kind in EventType:
        assert summarize(ev(1, kind)) != ""


def test_timeline_on_empty_input_is_empty() -> None:
    assert timeline([]) == ()


def test_timeline_does_not_mutate_payload() -> None:
    event = ev(1, EventType.STEP_DECIDED, step=1, tool="t", args={"a": 1}, step_key="k")
    before = dict(event.payload)
    timeline([event])
    assert dict(event.payload) == before


def test_export_jsonl_writes_one_line_per_event(tmp_path: Path) -> None:
    events = sample_events()
    target = tmp_path / "trace.jsonl"

    count = export_jsonl(events, target)

    assert count == len(events)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(events)
    assert json.loads(lines[0])["type"] == "RUN_STARTED"


def test_export_jsonl_round_trips_through_decode(tmp_path: Path) -> None:
    events = sample_events()
    target = tmp_path / "trace.jsonl"
    export_jsonl(events, target)

    restored = [decode_event(line) for line in target.read_text(encoding="utf-8").splitlines()]

    assert restored == events


def test_export_jsonl_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "trace.jsonl"
    export_jsonl(sample_events(), target)
    assert target.exists()


def test_export_jsonl_rejects_unsafe_payload(tmp_path: Path) -> None:
    from agentrun.codec import EncodeError

    events = [ev(1, EventType.STEP_STARTED, step=1, attempt=(1, 2))]
    with pytest.raises(EncodeError):
        export_jsonl(events, tmp_path / "bad.jsonl")


def test_exported_file_is_not_valid_json_as_a_whole(tmp_path: Path) -> None:
    # JSONL 是「每行一个 JSON」，整份文件不是合法 JSON——这个断言防止有人把它改回数组
    target = tmp_path / "trace.jsonl"
    export_jsonl(sample_events(), target)
    with pytest.raises(json.JSONDecodeError):
        json.loads(target.read_text(encoding="utf-8"))


def test_decode_rejects_trace_line_without_body(tmp_path: Path) -> None:
    target = tmp_path / "trace.jsonl"
    target.write_text('{"seq": 1}\n', encoding="utf-8")
    with pytest.raises(DecodeError):
        decode_event(target.read_text(encoding="utf-8").strip())
