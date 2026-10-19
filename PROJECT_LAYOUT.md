# PROJECT_LAYOUT — 当前契约

> 本文件只描述**已经存在**的东西。未来模块单列在末尾，不混入现状。
> 最后更新：2026-10-18（第 0 轮）

## 1. 里程碑与当前状态

| 项 | 状态 |
|---|---|
| 当前里程碑 | 第 0 轮 / 骨架与工程检查 |
| 已实现能力 | 可安装包、骨架冒烟测试、lint/格式检查、CI 配置（未远端运行） |
| 进行中 | 模拟账本 `demo.Ledger`：方法签名与 12 个黑盒测试已就位，**实现为空（NotImplementedError）** |
| 未实现能力 | 事件契约、reduce、工具注册与执行、策略、主循环、日志、恢复、对账、CLI |
| 运行前提 | Windows + Python 3.14.6；离线，不依赖任何模型服务 |

v0 / v1 / v2 的定义与差异见 `LEARNING_LOG.md` 第 0 轮，不在本文件重复。

## 2. 当前真实目录

```text
项目2/
├─ pyproject.toml            唯一的依赖与工具配置来源
├─ README.md                 环境准备与检查入口
├─ .gitignore
├─ .github/workflows/ci.yml  CI：安装 → ruff check → ruff format --check → pytest
├─ src/agentrun/
│  ├─ __init__.py            包入口，仅暴露 __version__ = "0.1.0.dev0"
│  └─ demo.py                模拟账本（外部世界）占位：Ledger / Record / LedgerRejected
└─ tests/
   ├─ test_package_smoke.py  2 个冒烟断言：可导入、版本非空
   └─ test_demo_ledger.py    12 个黑盒测试，当前全红（NotImplementedError）
```

**故意不存在**：`events.py` / `model.py` / `tools.py` / `policy.py` / `engine.py` / `journal.py` / `trace.py` / `llm.py` / `demo.py` / `cli.py`。空目录或空文件不提供任何契约信息，等对应轮次真正需要时再创建。

## 3. 已确认的公共接口

本轮只有包级契约：

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `import agentrun` | 无 | module | 包名即 import 路径，已由 `pip install -e .` 实测 |
| `agentrun.__version__` | 无 | `str` | 与 `pyproject.toml` 的 `version` 一致（当前 `0.1.0.dev0`） |

错误语义、不变量、序列化格式：**尚无**，待第 1 轮契约确认。

## 4. 依赖方向

本轮无模块间调用。已定的方向约束（第 1 轮起生效）：

```text
cli → engine → {policy, tools, journal, model, events}
trace 只读 journal / 事件快照，不得反向修改运行状态
model / events 不依赖数据库、模型服务与 I/O
```

## 5. 学习者已确认的决策

| # | 决策 | 结论 | 日期 | 代价 / 可逆性 |
|---|---|---|---|---|
| D1 | 项目根目录 | `C:\Users\王佳浩\Desktop\项目2`（中文路径） | 2026-10-18 | 已实测 venv + editable install + pytest + ruff 全部可用；若将来某工具链不兼容再迁移 |
| D2 | 包名 | `agentrun`，与参考项目同名 | 2026-10-18 | 两仓库路径隔离，不共享代码；风险是搜索时混淆，用仓库路径区分 |
| D3 | 场景 | 模拟账本，见 §6 | 2026-10-18 | 参数集中在工具层，改动成本低 |
| D4 | 运行时与依赖 | Python 3.14.6 + venv + pip；业务代码只用标准库；开发依赖仅 pytest + ruff | 2026-10-18 | 不引 pydantic；运行时校验需手写，类型注解不等于校验 |
| D5 | 版本控制 | 本地 `git init`，第 0 轮提交一次骨架，不推送；最终完成时再推 GitHub | 2026-10-18 | 无远端，CI 仅"配置已准备" |
| D6 | 存储边界 | 模拟账本与引擎事件日志**分开存储**，不共享事务 | 2026-10-18（第 1 轮正式落定字段） | 保留跨系统崩溃窗口，这是本项目主要教学素材 |

## 6. 场景参数（第 0 轮确认）

> 任务："检查 alice 的模拟余额，满足条件时向 bob 转移 250 个测试单位，随后生成回执；遇到执行结果无法确认时，保留现场并等待核对。"

| 项 | 值 | 备注 |
|---|---|---|
| 账户初值 | alice = 1000，bob = 0 | 隔离测试目录 |
| 策略前置条件 | 仅当 alice 余额 ≥ 500 时发起转移 | 属于 Policy 的判断，不是工具职责 |
| 转移金额 | 250 测试单位 | 非幂等：重复调用会重复生效 |
| 工具级不变量（Agent 提议，学习者授权补充） | `amount > 0`；`amount ≤ alice 余额`；**转移后 alice 余额 ≥ 300**（最低储备） | 违反属"参数合法但业务拒绝"，是**确定未生效**，可与"结果未知"对照 |
| 回执 | `save_receipt` 以 `request_id` 幂等 | 去重与写入由工具端原子完成 |
| 对账 | `lookup_request(request_id)` → 已生效 / 确定未生效 / 无法确认 | 三态必须分开 |

存储形式（JSONL 追加文件 / SQLite 表）与字段命名：第 2 轮随工具契约确认。

## 7. 工程检查

| 工具 | 范围 | 命令 | 当前状态 |
|---|---|---|---|
| pytest | `tests/` | `.\.venv\Scripts\python.exe -m pytest` | 实测通过：2 passed |
| ruff lint | 全仓库 | `.\.venv\Scripts\ruff.exe check .` | 实测通过：All checks passed |
| ruff format | 全仓库 | `.\.venv\Scripts\ruff.exe format --check .` | 实测通过：3 files already formatted |
| CI | GitHub Actions，Python 3.14 | 同上三条 | **配置已准备，未推送、未远端运行** |
| mypy | 未引入 | — | 触发信号：第 1 轮事件/视图/公共接口成型 |
| Docker | 未引入 | — | 触发信号：CLI 启动方式稳定且出现跨机/容器交付需求 |

## 8. 最近一次实际验证

- 时间：2026-10-18
- 解释器：`C:\Users\王佳浩\Desktop\项目2\.venv\Scripts\python.exe`（Python 3.14.6）
- 安装：`pip install --index-url https://pypi.org/simple -e ".[dev]"` → agentrun 0.1.0.dev0、pytest 9.1.1、ruff 0.17.1
- 结果：`2 passed in 0.04s`；`All checks passed!`；`3 files already formatted`
- 未验证：CI 远端运行、真实模型调用、任何业务行为

### 环境事件记录

本机 pip 全局 `index-url` 指向清华镜像，2026-10-18 实测该镜像 TLS 握手失败（`SSLEOFError: UNEXPECTED_EOF_WHILE_READING`），官方 PyPI 正常。**未修改全局 pip 配置**，仅在命令中显式指定索引。属环境故障，非代码缺陷。

## 9. 未来模块（未创建）

| 文件 | 计划职责 | 引入轮次 |
|---|---|---|
| `events.py` | 事件契约、序列化 | 第 1～2 轮 |
| `model.py` | RunView / StepView、apply / reduce | 第 1～3 轮 |
| `tools.py` | Tool、副作用分类、对账协议、注册表 | 第 2 轮 |
| `policy.py` | Policy 协议、ScriptedPolicy | 第 3 轮 |
| `engine.py` | 驱动、执行、恢复、人工复核 | 第 3、6 轮 |
| `journal.py` | SQLite 事件日志，原子追加 | 第 5 轮 |
| `trace.py` | 时间线、JSONL、只读监听 | 第 7 轮 |
| `llm.py` | 模型适配器 | 第 8 轮 |
| `demo.py` | 隔离的模拟账本 | 第 2 轮 |
| `cli.py` | 入口与依赖组装 | 第 6 轮 |
