# 场景矩阵：8 个真实模型评测

`scripts/eval_suite.py` 每次跑完会打印一张表。这里说明每一行在验什么、期望值从哪来、
以及它对应的离线契约测试在哪。

**这些场景用真实模型。** 脚本把任务描述交给模型，模型的回答决定调什么工具、传什么参数。
脚本只做三件事：注入故障、观察结局、如实打印。模型偏离指令时，脚本不会把它改写成通过
——它会显示 `未通过`，然后由人决定这算模型的问题还是场景的问题。

余额与转账发生在隔离的临时目录里，是模拟业务。故障与人工结论由脚本明确注入，不代表
真实支付或真实审批。

## 矩阵

| 场景 | 注入的故障 | 期望结局 | 期望余额（alice/bob） | 它证明了什么 |
|---|---|---|---|---|
| 正常转账 | 无 | `SUCCEEDED` | 750 / 250 | 端到端跑通：决策 → 落盘 → 执行 → 落盘 |
| 业务明确拒绝 | 转账被业务拒绝 | `SUCCEEDED` | 1000 / 0 | 确定失败是终局，不重试、不改金额 |
| 预算耗尽 | `max_steps=1` | `EXHAUSTED` | 750 / 250 | 预算是独立的终态，不是成功，也不写 answer |
| 只读余额查询 | 无 | `SUCCEEDED` | 1000 / 0 | 读操作不产生副作用，预算不该被写操作独占 |
| 同键回执重放 | 同 `step_key` 重复投递两次 | `SUCCEEDED` | 1000 / 0 | 幂等键在工具端生效：两次投递只有一份记录 |
| 已生效 → 对账 | 转账**已生效**，但响应丢失 | `SUCCEEDED` | 750 / 250 | `UNCERTAIN` 不是终局：外部证据能把它收敛成成功 |
| 人工确认 → 重做 | 转账**执行前**，响应丢失 | `SUCCEEDED` | 750 / 250 | 复核给 `not_applied` 之后，重做是**新步骤**、新 `step_key` |
| 人工决定 → 终止 | 同上，复核给 `abort` | `ABORTED` | 1000 / 0 | 证据不足时可以停下，不必猜 |

## 三个带中间态的场景

第 6、7、8 行的中间态都是 `UNCERTAIN`，这是刻意的：

```
执行 → STEP_STARTED 已落盘 → 调用发出 → 响应丢失
      ↑ 此刻日志知道「可能改了」，但不知道改没改
```

脚本先断言 `stop_reason is UNCERTAIN`，然后才去对账。对账查账本：

- 查到 → 补记 `STEP_COMPLETED`，任务回到 `RUNNING`，继续跑（第 6 行）
- 查不到 → 写 `REVIEW_REQUESTED`，任务停在 `WAITING_REVIEW`（第 7、8 行）
- 复核给 `not_applied` → 该步记失败，任务回到 `RUNNING`，模型可以重做（第 7 行）
- 复核给 `abort` → `ABORTED`，此后一次模型调用都不再发生（第 8 行）

第 8 行还额外断言「终止之后 `client.calls` 没变」——终止不是一个状态标签，是「真的不再
打扰外部世界」。

## 对应的离线契约测试

上面每个场景都有一组不依赖模型的测试，跑 `pytest` 即可，不需要网络与凭据：

| 场景 | 契约测试 |
|---|---|
| 正常转账 / 预算耗尽 | `tests/test_engine_contract.py` |
| 业务明确拒绝 | `tests/test_ledger_tools.py` |
| 同键回执重放 | `tests/test_ledger_tools.py`、`tests/test_tools_contract.py` |
| 已生效 → 对账 | `tests/test_reconcile.py` |
| 人工确认 → 重做 / 终止 | `tests/test_review.py` |
| 崩溃窗口（未列在矩阵里，但同属恢复语义） | `tests/test_process_crash.py` |
| 决策失败可重试 | `tests/test_decision_failure.py` |
| 复核提交的并发与幂等 | `tests/test_review_resolve_atomic.py` |

矩阵是**给人看的证据链**，契约测试是**给 CI 看的红线**。前者会被模型的不确定性影响，
后者不会——两者不能互相替代。

## 怎么跑

```powershell
# 真实模型：会联网、按服务实际用量计费
.\.venv\Scripts\python.exe scripts\eval_suite.py

# 离线：不需要网络与凭据
.\.venv\Scripts\python.exe -m pytest
```

退出码：`0` 全部通过，`1` 至少一行未通过，`2` 模型配置不可用（不会拿预设回答顶替）。
