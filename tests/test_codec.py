"""事件编码契约测试：往返无损，以及编码侧与解码侧各自的拒绝规则。"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from agentrun.codec import CodecError, DecodeError, EncodeError, decode_event, encode_event
from agentrun.events import Event, EventType

AT = "2026-10-25T00:00:00+00:00"


def make_event(**overrides: Any) -> Event:
    """构造一条默认合法的事件，用 ``overrides`` 覆盖个别字段。"""
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "seq": 1,
        "type": EventType.RUN_STARTED,
        "payload": {"task": "转账 250", "max_steps": 10},
        "at": AT,
    }
    fields.update(overrides)
    return Event(**fields)


def raw_json(**overrides: Any) -> str:
    """构造一份解码用的原始 JSON 文本，用 ``overrides`` 覆盖个别字段。"""
    data: dict[str, Any] = {
        "run_id": "run-1",
        "seq": 1,
        "type": "RUN_STARTED",
        "payload": {},
        "at": AT,
    }
    data.update(overrides)
    return json.dumps(data)


class Opaque:
    """无法用 JSON 表示的自定义对象。"""


def test_encode_output_is_json_object() -> None:
    raw = json.loads(encode_event(make_event()))
    assert set(raw) == {"run_id", "seq", "type", "payload", "at"}
    assert raw["type"] == "RUN_STARTED"
    assert raw["run_id"] == "run-1"
    assert raw["at"] == AT


def test_round_trip_basic() -> None:
    event = make_event()
    assert decode_event(encode_event(event)) == event


def test_round_trip_decided_step() -> None:
    payload = {
        "step": 2,
        "tool": "transfer",
        "args": {"src": "alice", "dst": "bob", "amount": 250},
        "step_key": "run-1:2",
    }
    event = make_event(type=EventType.STEP_DECIDED, seq=5, payload=payload)
    assert decode_event(encode_event(event)) == event


def test_round_trip_preserves_scalar_types() -> None:
    payload = {
        "i": 7,
        "f": 1.5,
        "b": True,
        "none": None,
        "text": "中文也要能读回",
        "nested": {"items": [1, 2, 3]},
    }
    event = make_event(payload=payload)
    restored = decode_event(encode_event(event))
    assert restored == event
    assert restored.payload["b"] is True
    assert restored.payload["i"] == 7


def test_round_trip_keeps_seq_as_int() -> None:
    restored = decode_event(encode_event(make_event(seq=42)))
    assert restored.seq == 42
    assert isinstance(restored.seq, int)
    assert not isinstance(restored.seq, bool)


def test_round_trip_all_event_types() -> None:
    for kind in EventType:
        event = make_event(type=kind, seq=3)
        assert decode_event(encode_event(event)) == event


@pytest.mark.parametrize(
    "value",
    [
        pytest.param((1, 2), id="tuple"),
        pytest.param({1, 2}, id="set"),
        pytest.param(b"bytes", id="bytes"),
        pytest.param(Decimal("1.10"), id="decimal"),
        pytest.param(datetime(2026, 10, 25, tzinfo=UTC), id="datetime"),
        pytest.param(Opaque(), id="custom-object"),
    ],
)
def test_encode_rejects_non_json_value(value: object) -> None:
    event = make_event(payload={"task": "转账", "max_steps": 10, "extra": value})
    with pytest.raises(EncodeError):
        encode_event(event)


def test_encode_rejects_non_json_value_in_nested_list() -> None:
    event = make_event(payload={"task": "转账", "trace": [1, (2, 3)]})
    with pytest.raises(EncodeError):
        encode_event(event)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="neg-inf"),
    ],
)
def test_encode_rejects_non_finite_float(value: float) -> None:
    event = make_event(payload={"task": "转账", "ratio": value})
    with pytest.raises(EncodeError):
        encode_event(event)


def test_encode_rejects_non_str_key() -> None:
    event = make_event(payload={"task": "转账", 1: "x"})
    with pytest.raises(EncodeError):
        encode_event(event)


def test_encode_rejects_non_mapping_payload() -> None:
    event = make_event(payload=["task", "转账"])
    with pytest.raises(EncodeError):
        encode_event(event)


def test_decode_rejects_non_str() -> None:
    with pytest.raises(DecodeError):
        decode_event(None)


def test_decode_rejects_invalid_json() -> None:
    with pytest.raises(DecodeError):
        decode_event("{not json")


def test_decode_rejects_non_object_top_level() -> None:
    with pytest.raises(DecodeError):
        decode_event("[1, 2]")


@pytest.mark.parametrize("missing", ["run_id", "seq", "type", "payload", "at"])
def test_decode_rejects_missing_field(missing: str) -> None:
    data: dict[str, Any] = {
        "run_id": "run-1",
        "seq": 1,
        "type": "RUN_STARTED",
        "payload": {},
        "at": AT,
    }
    del data[missing]
    with pytest.raises(DecodeError):
        decode_event(json.dumps(data))


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("NOT_A_TYPE", id="unknown-name"),
        pytest.param("", id="empty-string"),
        pytest.param(1, id="int"),
        pytest.param(None, id="none"),
    ],
)
def test_decode_rejects_bad_type(bad: object) -> None:
    with pytest.raises(DecodeError):
        decode_event(raw_json(type=bad))


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("1", id="numeric-string"),
        pytest.param(1.5, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(None, id="none"),
        pytest.param([1], id="list"),
    ],
)
def test_decode_rejects_non_int_seq(bad: object) -> None:
    with pytest.raises(DecodeError):
        decode_event(raw_json(seq=bad))


@pytest.mark.parametrize("field", ["run_id", "at"])
def test_decode_rejects_non_str_field(field: str) -> None:
    with pytest.raises(DecodeError):
        decode_event(raw_json(**{field: 42}))


def test_decode_rejects_non_object_payload() -> None:
    with pytest.raises(DecodeError):
        decode_event(raw_json(payload=["task"]))


def test_codec_error_hierarchy() -> None:
    assert issubclass(EncodeError, CodecError)
    assert issubclass(DecodeError, CodecError)
