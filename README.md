# Agent 任务执行引擎（agentrun）

以持久化事件为事实来源、根据工具反馈决定下一步动作、并能在明确安全边界内恢复的轻量 Agent 执行引擎。

默认场景为隔离目录中的**模拟账本**（余额查询、非幂等转移、幂等回执、按调用标识对账），不连接任何真实支付、银行或邮件系统。

## 当前状态

第 11 轮完成：失败原因进了视图，提示有了确定性的长度上限。

- 事件落盘到 SQLite，进程重启后 `replay` 出的视图与停止前完全一致。
- 单次工具调用分三类结果：`SUCCEEDED` / `FAILED`（确定没生效）/ `UNCERTAIN`（不知道）。
- 遇到 `UNCERTAIN` 引擎立即停止；对账查到已生效则补记结果，查不到则保持未知，绝不自动重做非幂等调用。
- `trace` 把事件渲染成时间线，也能导出 JSONL；观察者拿到的都是副本，改不动日志。
- 模型策略把视图渲染成提示、把模型输出严格解析成 `ToolCall` / `Finish`；不联网，客户端由调用方注入。
- 解析失败、模型超时、脚本写短一律 `PolicyError` → `RUN_FAILED`：一个工具都没调用，外部世界未变。
- 提示只由视图与工具清单决定，重启后同一个视图得到同一份提示，续跑不再依赖重新提供脚本。
- 自动对账查不到记录时不再停在「不知道」，而是写 `REVIEW_REQUESTED` 进入 `WAITING_REVIEW`，等人工给结论。
- 复核结论三种：`applied` 补记结果、`not_applied` 允许把调用当作新步骤重做、`abort` 终止任务（`ABORTED`）。
- 等待复核期间引擎一步也不走：`resume` 只把状态带回来，不会替人猜。
- 进程级故障矩阵：`after_decided`（决策已落盘）、`before_tool`（意图已落盘）、`after_tool`（副作用已发生）三个窗口都用 `os._exit` 真退进程，再用新进程恢复。
- 只有决策、没有意图时复用已落盘的决策，不重新问策略——参数不会被换掉。
- `STEP_UNCERTAIN` 不是终局：对账命中补记成功，查不到转人工，收敛之前 `resume` 一步不走。
- 离线评测集覆盖 8 种结局，打印每次的停止原因、步数、事件数、余额与耗时。
- `RUN_FAILED` 的原因进入视图，`status` 直接打印「为什么失败」，不必再翻事件。
- 提示里超长的参数与结果按 `field_limit`（默认 200 字）截断并标注截掉多少；截断是确定性的，同一视图仍渲染出同一份提示。

里程碑与契约见 `PROJECT_LAYOUT.md`，每轮的理解与复盘见 `LEARNING_LOG.md`。

## 环境准备

已验证环境：Windows + Python 3.14.6。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -e ".[dev]"
```

本机 pip 全局配置指向清华镜像，2026-10-18 实测该镜像 TLS 握手失败（`SSLEOFError`），官方 PyPI 可用。如再次遇到，追加 `--index-url https://pypi.org/simple`，不要为此修改全局 pip 配置。

**必须用 `.venv` 里的解释器**：`agentrun` 只装在虚拟环境中，用系统 Python 会得到 `ModuleNotFoundError`。

## 命令行

任务由 JSON 脚本描述，每项要么调用工具，要么结束：

```json
[
  {"tool": "get_balance", "args": {"owner": "alice"}},
  {"tool": "transfer", "args": {"src": "alice", "dst": "bob", "amount": 250}},
  {"finish": "转账完成"}
]
```

```powershell
# 跑一个新任务
.\.venv\Scripts\python.exe -m agentrun.cli run --run-id r1 --script task.json

# 查看事件与每一步状态
.\.venv\Scripts\python.exe -m agentrun.cli status --run-id r1

# 接着上次的进度继续（策略不持久化，需要重新给脚本）
.\.venv\Scripts\python.exe -m agentrun.cli resume --run-id r1 --script task.json

# 为「已开始、没有结果」的那一步补记结论；查不到则转入人工复核
.\.venv\Scripts\python.exe -m agentrun.cli reconcile --run-id r1

# 写下人工复核结论：applied / not_applied / abort
.\.venv\Scripts\python.exe -m agentrun.cli resolve --run-id r1 --decision applied `
  --note "账本记录与 step_key 一致" --result '{"found": true, "amount": 250}'

# 打印时间线，或导出成一行一条的 JSONL
.\.venv\Scripts\python.exe -m agentrun.cli trace --run-id r1
.\.venv\Scripts\python.exe -m agentrun.cli trace --run-id r1 --export trace.jsonl
```

默认 `--db agentrun.db`、`--ledger ledger`，账本与事件日志必须分开存储（D6）。

退出码：`0` 表示任务成功（或 `resolve` / `reconcile` 成功写入）；`1` 表示任务失败、预算耗尽或仍在等待复核；`2` 表示参数、脚本、日志或复核时机有问题。

安装为命令后可直接用 `agentrun <子命令>`。

脚本是策略的一种：写短了不会抛栈，下一次决策会记 `RUN_FAILED`，退出码 1。

## 模型策略（离线）

`llm.py` 只定义适配器，不绑定任何模型服务：客户端只需实现 `complete(prompt) -> str`。

```python
from agentrun.engine import Engine
from agentrun.llm import ModelPolicy, ToolSpec

policy = ModelPolicy(client, [ToolSpec("get_balance", "读取账户余额")])
result = Engine(policy, registry, log).run("r1", "查一下 alice 的余额", max_steps=5)
```

完整演示（假模型，不需要网络与密钥）：

```powershell
.\.venv\Scripts\python.exe scripts\model_policy_demo.py
```

## 本地检查

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe scripts\eval_suite.py
```

`eval_suite.py` 是离线评测集：8 个场景各跑一遍，逐行核对停止原因、状态、步数、事件数与余额。余额同时充当「重复副作用」的探针。

四者必须同时通过；CI 使用完全相同的入口。

## 目录

```text
src/agentrun/        包本体：账本、事件、折叠、编解码、工具、策略、模型适配器、日志、引擎、对账、CLI
tests/               契约测试
scripts/             教学脚本：崩溃窗口、账本工具、恢复重放、对账续跑、模型策略、人工复核、进程级故障注入、评测集
.github/workflows/   CI 配置
PROJECT_LAYOUT.md    当前契约：目录、接口、决策、验证记录
LEARNING_LOG.md      学习记录与复盘
```
