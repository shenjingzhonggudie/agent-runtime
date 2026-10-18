# Agent 任务执行引擎（agentrun）

以持久化事件为事实来源、根据工具反馈决定下一步动作、并能在明确安全边界内恢复的轻量 Agent 执行引擎。本项目是学习项目：**决策与核心实现由学习者完成，测试、脚手架与证据由指导 Agent 负责。**

默认场景为隔离目录中的**模拟账本**（余额查询、非幂等转移、幂等回执、按调用标识对账），不连接任何真实支付、银行或邮件系统。

## 当前状态

第 0 轮：骨架与工程检查。除包本身外尚无任何业务模块，事件、工具、策略、引擎、日志均未实现。

里程碑定义见 `PROJECT_LAYOUT.md`，每轮的理解与复盘见 `LEARNING_LOG.md`。

## 环境准备

已验证环境：Windows + Python 3.14.6。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

本机 pip 全局配置指向清华镜像，2026-10-18 实测该镜像 TLS 握手失败（`SSLEOFError`），官方 PyPI 可用。如再次遇到，追加 `--index-url https://pypi.org/simple`，不要为此修改全局 pip 配置。

## 本地检查

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
```

三者必须同时通过；CI 使用完全相同的入口。

## 目录

```text
src/agentrun/        包本体（当前仅有 __init__.py）
tests/               测试（当前仅骨架冒烟测试）
.github/workflows/   CI 配置
PROJECT_LAYOUT.md    当前契约：目录、接口、决策、验证记录
LEARNING_LOG.md      学习记录与复盘
```
