# Agent 任务执行引擎（agentrun）

以持久化事件为事实来源、根据工具反馈决定下一步动作、并能在明确安全边界内恢复的轻量 Agent 执行引擎。

默认场景为隔离目录中的**模拟账本**（余额查询、非幂等转移、幂等回执、按调用标识对账），不连接任何真实支付、银行或邮件系统。

## 当前状态

第 8 轮完成：决策可以来自模型，且「策略给不出动作」有了确定的失败语义。

- 事件落盘到 SQLite，进程重启后 `replay` 出的视图与停止前完全一致。
- 单次工具调用分三类结果：`SUCCEEDED` / `FAILED`（确定没生效）/ `UNCERTAIN`（不知道）。
- 遇到 `UNCERTAIN` 引擎立即停止；对账查到已生效则补记结果，查不到则保持未知，绝不自动重做非幂等调用。
- `trace` 把事件渲染成时间线，也能导出 JSONL；观察者拿到的都是副本，改不动日志。
- 模型策略把视图渲染成提示、把模型输出严格解析成 `ToolCall` / `Finish`；不联网，客户端由调用方注入。
- 解析失败、模型超时、脚本写短一律 `PolicyError` → `RUN_FAILED`：一个工具都没调用，外部世界未变。
- 提示只由视图与工具清单决定，重启后同一个视图得到同一份提示，续跑不再依赖重新提供脚本。

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

# 为「已开始、没有结果」的那一步补记结论
.\.venv\Scripts\python.exe -m agentrun.cli reconcile --run-id r1

# 打印时间线，或导出成一行一条的 JSONL
.\.venv\Scripts\python.exe -m agentrun.cli trace --run-id r1
.\.venv\Scripts\python.exe -m agentrun.cli trace --run-id r1 --export trace.jsonl
```

默认 `--db agentrun.db`、`--ledger ledger`，账本与事件日志必须分开存储（D6）。

退出码：`0` 仅表示任务成功；`1` 表示任务失败或预算耗尽；`2` 表示参数、脚本、日志或对账前置条件有问题。

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
```

四者必须同时通过；CI 使用完全相同的入口。

## 目录

```text
src/agentrun/        包本体：账本、事件、折叠、编解码、工具、策略、模型适配器、日志、引擎、对账、CLI
tests/               契约测试
scripts/             教学脚本：崩溃窗口、账本工具、恢复重放、对账续跑、模型策略
.github/workflows/   CI 配置
PROJECT_LAYOUT.md    当前契约：目录、接口、决策、验证记录
LEARNING_LOG.md      学习记录与复盘
```
