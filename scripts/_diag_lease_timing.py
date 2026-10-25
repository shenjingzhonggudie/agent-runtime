"""临时诊断：一次日志写事务在本机要花多久。

用途：判断 `tests/test_lease.py` 里 ``lease_ttl=0.05`` 的接管用例为何偶发失败。
假设是「单次事务耗时已经接近 50ms，worker 会把自己判死」，这里用数据验证。
不属于交付代码，可随时删除。
"""

from __future__ import annotations

import statistics
import tempfile
import time
from pathlib import Path

from agentrun.events import EventType
from agentrun.journal import LeaseLost, SqliteEventLog

ROUNDS = 200


def percentile(values: list[float], ratio: float) -> float:
    """取分位数。

    Args:
        values: 已排序或未排序的样本。
        ratio: 0~1 之间的分位，例如 0.99。

    Returns:
        float: 对应分位的耗时（毫秒）。
    """
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1))))
    return ordered[index] * 1000


def main() -> int:
    """测量 acquire 与续租+追加的耗时分布。

    Returns:
        int: 超时（>50ms）的次数之和，用于判断是否命中根因。
    """
    acquire_cost: list[float] = []
    append_cost: list[float] = []
    slow = 0

    with tempfile.TemporaryDirectory() as raw:
        log = SqliteEventLog(Path(raw) / "events.db")
        for _ in range(ROUNDS):
            start = time.perf_counter()
            lease = log.acquire("r1", "w1", 30)
            acquire_cost.append(time.perf_counter() - start)
            assert lease is not None

            start = time.perf_counter()
            lease = log.renew(lease, 30)
            log.append("r1", EventType.STEP_DECIDED, {"step": 1}, lease=lease)
            append_cost.append(time.perf_counter() - start)

    for name, values in (("acquire", acquire_cost), ("renew+append", append_cost)):
        print(
            f"{name}: min={min(values) * 1000:.1f}ms "
            f"p50={statistics.median(values) * 1000:.1f}ms "
            f"p99={percentile(values, 0.99):.1f}ms max={max(values) * 1000:.1f}ms"
        )
        slow += sum(1 for value in values if value > 0.05)

    print(f"over 50ms: {slow}/{2 * ROUNDS}")
    try:
        log.renew(lease, 30)
    except LeaseLost:
        print("renew 正常路径可用")
    return slow


if __name__ == "__main__":
    raise SystemExit(0 if main() >= 0 else 1)
