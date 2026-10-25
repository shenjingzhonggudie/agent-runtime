"""事件契约：引擎唯一的事实来源。

``Event`` 只承载“发生过什么”，不判断合法性；某条事件在当前状态下是否允许，
由 ``agentrun.model.apply`` 负责。两者分开，是为了让“事实”可以原样保存、
原样重放，而“判断”可以随规则演化。

各事件的 payload 字段（第 1B 轮契约）：

- ``RUN_STARTED``：``task: str``、``max_steps: int >= 0``
- ``STEP_DECIDED``：``step: int``（从 1 开始）、``tool: str``、``args: dict``、
  ``step_key: str``
- ``STEP_STARTED``：``step: int``、``attempt: int``（从 1 开始）
- ``STEP_COMPLETED``：``step: int``、``result: dict``
- ``STEP_FAILED``：``step: int``、``error: dict``（约定含 ``kind`` 与 ``message``）
- ``STEP_UNCERTAIN``：``step: int``、``reason: str``
- ``RUN_SUCCEEDED``：``answer: str``（去空白后不得为空）
- ``RUN_FAILED``：``reason: str``
- ``RUN_EXHAUSTED``：``max_steps: int``
- ``REVIEW_REQUESTED``：``step: int``、``reason: str``（去空白后不得为空）
- ``REVIEW_RESOLVED``：``step: int``、``decision: str``（``applied`` / ``not_applied`` /
  ``abort``）、``note: str``（去空白后不得为空）；``decision == "applied"`` 时另带
  ``result: dict``，其余决策不得携带

顺序约定：``seq`` 从 1 开始连续递增，是唯一的排序依据；``at`` 只供人阅读，
不参与任何判定（时钟回拨、时区差异都不该改变重放结果）。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    """事件类型。字符串值即日志中保存的稳定标识，不要随意改名。"""

    RUN_STARTED = "RUN_STARTED"
    STEP_DECIDED = "STEP_DECIDED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    STEP_UNCERTAIN = "STEP_UNCERTAIN"
    RUN_SUCCEEDED = "RUN_SUCCEEDED"
    RUN_FAILED = "RUN_FAILED"
    RUN_EXHAUSTED = "RUN_EXHAUSTED"
    REVIEW_REQUESTED = "REVIEW_REQUESTED"
    REVIEW_RESOLVED = "REVIEW_RESOLVED"


@dataclass(frozen=True, slots=True)
class Event:
    """一条已经发生的事实，追加后不再修改。

    Attributes:
        run_id: 所属任务的标识；同一次任务内保持不变。
        seq: 从 1 开始的连续整数，唯一排序依据。由 Journal 分配；v0 阶段由引擎
            传入，但语义与后续持久化阶段一致。
        type: 事件类型。
        payload: 与类型相关的字段，见模块说明。payload 内部允许嵌套 dict，
            因此“不可变”只覆盖顶层引用，深层隔离由调用方负责。
        at: ISO-8601 UTC 时间字符串，仅供人阅读与展示，不参与任何判定。
    """

    run_id: str
    seq: int
    type: EventType
    payload: Mapping[str, Any]
    at: str


def make_step_key(run_id: str, step: int) -> str:
    """构造跨重试稳定的调用键。

    Args:
        run_id: 任务标识。
        step: 从 1 开始的步骤序号。

    Returns:
        str: ``"<run_id>:<step>"``。同一步骤的所有尝试共用这一个键，因此工具端
            按它去重时，重试不会被当成新业务。

    Notes:
        键里**不能**包含 attempt。一旦包含，每次重试都会换一个幂等键，去重失效，
        非幂等工具就会重复生效。attempt 只记录在 ``STEP_STARTED`` 事件里。
    """
    return f"{run_id}:{step}"
