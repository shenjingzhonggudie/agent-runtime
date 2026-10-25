"""事件编码：把 ``Event`` 与可直接落盘的 JSON 文本互相转换。

本模块只回答两个问题：这条事件能不能原样写出去？写出去之后能不能原样读回来？

它不判断事件顺序（那是 ``model.apply`` 的职责），不分配 ``seq``（那是日志层的职责），
不认识任何业务字段。唯一硬要求是往返无损：``decode_event(encode_event(e)) == e``。

依赖方向：
    codec → events
"""

import json
import math
from collections.abc import Mapping
from typing import Any

from .events import Event, EventType


class CodecError(Exception):
    """事件编码或解码失败。

    编码与解码共用同一个基类，是为了让日志层能用一条 ``except`` 兜住两侧的失败；
    需要区分阶段时再分到子类，不要靠 ``isinstance(exc, ValueError)`` 猜。
    """


class EncodeError(CodecError):
    """事件含有无法原样落盘的内容，编码失败。

    抛出它表示「这条事件不能写进库」，而不是「库写失败了」。
    """


class DecodeError(CodecError):
    """文本不是一条合法的事件记录，解码失败。

    抛出它表示「这条记录读不懂」，调用方不能把它当成「没有这条事件」。
    """


def _check_value(value: Any, path: str) -> None:
    """递归检查一个值能否无损地写成 JSON 再读回来。

    Args:
        value: 待检查的值，可以是标量，也可以是任意层级的容器。
        path: 出错信息里用于定位的路径，如 ``payload.args.amount``。

    Raises:
        EncodeError: 出现下列任一情况时抛出：
            值不在 JSON 原生类型内（``tuple`` / ``set`` / ``bytes`` /
            ``Decimal`` / ``datetime`` / 自定义对象）；映射的键不是 ``str``；
            浮点数是 ``nan`` / ``inf`` / ``-inf``。

    Notes:
        判断依据只有一条：这个值编码后再解码，还等于它自己吗？``tuple`` 会被
        ``json`` 静默转成 ``list``、``int`` 键会被静默转成 ``str``、``nan``
        永远不等于自己——三者都属于「写得出去但读回来已不是原来那个值」，
        必须在编码前拒绝，不能留给 ``json.dumps`` 决定。
    """
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EncodeError(f"{path} 不是有限浮点数")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_value(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EncodeError(f"{path} 的键不是 str：{key!r}")
            _check_value(item, f"{path}.{key}")
        return
    raise EncodeError(f"{path} 的类型 {type(value).__name__} 无法无损落盘")


def _require(document: Mapping[str, Any], name: str, kind: type) -> Any:
    """取出必需字段并校验类型。

    Args:
        document: 解码得到的顶层映射。
        name: 字段名。
        kind: 期望的类型。

    Returns:
        Any: 字段值。

    Raises:
        DecodeError: 字段缺失，或类型不符；``kind`` 为 ``int`` 时 ``bool``
            同样不接受，因为 ``True`` 是 ``int`` 的子类。

    Notes:
        缺字段与类型不符对外是同一种失败：调用方关心的是「这条记录读不懂」，
        不是「缺了哪个键」。具体原因只写进异常消息，不进入类型。
    """
    if name not in document:
        raise DecodeError(f"缺少字段 {name!r}")
    value = document[name]
    if kind is int and isinstance(value, bool):
        raise DecodeError(f"字段 {name!r} 必须是 int")
    if not isinstance(value, kind):
        raise DecodeError(f"字段 {name!r} 必须是 {kind.__name__}")
    return value


def encode_event(event: Event) -> str:
    """把一条事件编码成 JSON 文本。

    Args:
        event: 待编码的事件。

    Returns:
        str: JSON 文本，顶层对象的字段固定为 ``run_id`` / ``seq`` / ``type`` /
        ``payload`` / ``at``，其中 ``type`` 使用枚举的字符串值。

    Raises:
        EncodeError: 出现下列任一情况时抛出：
            ``payload`` 含 JSON 原生类型以外的值——包括 ``tuple``、``set``、
            ``bytes``、``Decimal``、``datetime`` 与任意自定义对象；``payload``
            或其中任意层级的映射出现非 ``str`` 的键；``payload`` 不是映射。

    Notes:
        只允许「编码后解码回来一模一样」的类型。``tuple`` 必须拒绝，而不是交给
        ``json`` 静默转成 ``list``——后者能写进库，但读回来已不是同一个值，这类
        损失在重放时无法与「本来就是这样」区分。
    """
    if not isinstance(event.payload, Mapping):
        raise EncodeError("payload 必须是映射")
    _check_value(event.payload, "payload")
    document = {
        "run_id": event.run_id,
        "seq": event.seq,
        "type": event.type.value,
        "payload": dict(event.payload),
        "at": event.at,
    }
    try:
        return json.dumps(document, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EncodeError(str(exc)) from exc


def decode_event(text: str) -> Event:
    """把 JSON 文本解码成事件。

    Args:
        text: ``encode_event`` 产出的 JSON 文本。

    Returns:
        Event: 与编码前相等的事件；``type`` 还原为 ``EventType``，``seq`` 还原为
        ``int``。

    Raises:
        DecodeError: 出现下列任一情况时抛出：
            ``text`` 不是 ``str``；不是合法 JSON；顶层不是对象；缺少
            ``run_id`` / ``seq`` / ``type`` / ``payload`` / ``at`` 任一字段；
            ``type`` 不是已知的 ``EventType``；``seq`` 不是 ``int``（``bool``
            同样不接受）；``payload`` 不是对象；``run_id`` / ``at`` 不是 ``str``。

    Notes:
        失败时抛异常，不返回「尽量拼出来的」事件。一条读不懂的记录如果被当成
        合法事件喂进 ``replay``，比直接报错危险得多——它会以合法身份污染视图。
    """
    if not isinstance(text, str):
        raise DecodeError("text 必须是 str")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise DecodeError(f"不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise DecodeError("顶层必须是对象")

    run_id = _require(raw, "run_id", str)
    seq = _require(raw, "seq", int)
    type_name = _require(raw, "type", str)
    payload = _require(raw, "payload", dict)
    at = _require(raw, "at", str)

    try:
        kind = EventType(type_name)
    except ValueError as exc:
        raise DecodeError(f"未知的事件类型：{type_name!r}") from exc

    return Event(run_id=run_id, seq=seq, type=kind, payload=payload, at=at)
