# PROJECT_LAYOUT — 当前契约

> 本文件只描述**已经存在**的东西。未来模块单列在末尾，不混入现状。
> 最后更新：2026-10-25（第 4 轮完成）

## 1. 里程碑与当前状态

| 项 | 状态 |
|---|---|
| 当前里程碑 | 第 4 轮完成：事件可无损编码为 JSON 文本并读回，落盘前置条件已具备 |
| 已实现能力 | 包骨架、模拟账本、事件契约、事件折叠、工具协议与单次执行、策略协议与脚本策略、内存事件日志、引擎主循环与预算、账本工具适配器、基于对账的续跑演示、事件编解码 |
| 未实现能力 | 持久化日志、恢复、对账工具化、追踪、CLI、真实模型 |
| 运行前提 | Windows + Python 3.14.6；全部离线，不依赖任何模型服务 |

v0 / v1 / v2 的定义见 `LEARNING_LOG.md` 第 0 轮。

## 2. 当前真实目录

```text
项目2/
├─ pyproject.toml            依赖与工具配置唯一来源（pytest / ruff / mypy）
├─ README.md                 环境准备与检查入口
├─ .gitignore
├─ .github/workflows/ci.yml  CI：安装 → lint → format → mypy → pytest
├─ scripts/
│  ├─ crash_window_demo.py   崩溃窗口对比实验（教学脚本，非引擎代码）
│  └─ ledger_demo.py         账本工具演示：一次完整运行 + 一次对账式续跑（教学脚本）
├─ src/agentrun/
│  ├─ __init__.py            包入口，__version__
│  ├─ demo.py                模拟账本（外部世界）：Ledger / Record / LedgerRejected / LedgerCorrupted
│  ├─ events.py              事件契约：EventType / Event / make_step_key
│  ├─ model.py               RunStatus / Outcome / InvalidEvent / StepView / RunView / apply / replay
│  ├─ codec.py               CodecError / EncodeError / DecodeError / encode_event / decode_event
│  ├─ tools.py               SideEffect / Tool 协议 / 异常 / ToolOutcome / ToolContext / ToolRegistry / run_tool
│  ├─ journal.py             EventLog 协议 + MemoryEventLog（分配 seq）
│  ├─ policy.py              ToolCall / Finish / Action / Policy 协议 / ScriptedPolicy
│  ├─ engine.py              StopReason / EngineResult / Engine（循环、预算、终止）
│  └─ ledger_tools.py        账本工具适配器：GetBalance / Transfer / SaveReceipt / LookupRequest
└─ tests/
   ├─ test_package_smoke.py     包冒烟断言
   ├─ test_demo_ledger.py       账本：余额、三条拒绝规则、幂等回执、对账查询
   ├─ test_model_contract.py    事件折叠契约：顺序、seq、终态、结果唯一性
   ├─ test_codec.py             事件编解码：往返无损、非 JSON 类型拒绝、坏文本拒绝
   ├─ test_tools_contract.py    工具执行：校验零副作用、异常分类、注册表
   ├─ test_journal_contract.py  内存日志：seq 按 run 独立分配
   ├─ test_policy_contract.py   脚本策略：按 len(steps) 取动作、脚本耗尽
   ├─ test_engine_contract.py   引擎：事件顺序、预算边界、UNCERTAIN 立即停止
   └─ test_ledger_tools.py      账本工具：参数校验、异常映射、幂等键取自 ctx
```

**故意不存在**：`trace.py` / `llm.py` / `cli.py`。

## 3. 已确认的公共接口

### 3.1 事件（`events.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `EventType` | — | 9 个枚举值 | 字符串值即日志稳定标识，改名等于破坏兼容 |
| `Event` | `run_id, seq, type, payload, at` | 冻结 dataclass | `seq` 从 1 连续递增，唯一排序依据；`at` 仅展示 |
| `make_step_key(run_id, step)` | 任务 id、步骤号 | `"<run_id>:<step>"` | **不含 attempt**，重试间稳定，工具端去重靠它 |

payload 字段：`RUN_STARTED{task,max_steps}`、`STEP_DECIDED{step,tool,args,step_key}`、
`STEP_STARTED{step,attempt}`、`STEP_COMPLETED{step,result}`、`STEP_FAILED{step,error}`、
`STEP_UNCERTAIN{step,reason}`、`RUN_SUCCEEDED{answer}`、`RUN_FAILED{reason}`、
`RUN_EXHAUSTED{max_steps}`。

### 3.2 视图（`model.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `apply(view, event)` | 当前视图或 `None`、事件 | 新 `RunView` | 纯函数，不修改入参；非法抛 `InvalidEvent` |
| `replay(events)` | 事件可迭代对象 | `RunView` | 等价于依次 `apply`；空序列抛 `InvalidEvent` |
| `RunView` | — | 冻结 dataclass | `run_id, task, max_steps, status, last_seq, steps, answer` |
| `StepView` | — | 冻结 dataclass | `step, tool, args, step_key, attempts, outcome, result, error, reason` |
| `RunStatus` | — | 4 值枚举 | `RUNNING / SUCCEEDED / FAILED / EXHAUSTED`，后三者不可逆 |
| `Outcome` | — | 3 值枚举 | `SUCCEEDED / FAILED / UNCERTAIN` |

**关键不变量**：

1. 首事件必须是 `RUN_STARTED` 且 `seq == 1`；之后 `seq == last_seq + 1`。
2. 终态之后不得追加任何事件。
3. 只有 `STEP_DECIDED` 会新增步骤；其余步骤事件更新已有步骤（`step` 必须已存在）。
4. `STEP_STARTED` 的 `attempt` 必须等于 `attempts + 1`；有结果后不得再开始。
5. 结果事件必须先有 `STEP_STARTED`（`attempts >= 1`），且该步还没有结果。
6. `RUN_SUCCEEDED` 的 `answer` 去空白后不得为空；`RUN_EXHAUSTED` 不得携带答案。

### 3.3 模拟账本（`demo.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `Ledger(root, initial)` | 独占目录、初始余额 | 实例 | 已存在时忽略 `initial` |
| `balance(owner)` | 账户名 | `int` | 未知账户为 0 |
| `transfer(src,dst,amount,request_id)` | — | `None` | 非幂等；三条规则违反则 `LedgerRejected` 且零副作用 |
| `save_receipt(request_id,text)` | — | `None` | 幂等，首写生效 |
| `lookup(request_id)` | — | `Record \| None` | `None` 只表示“此刻查不到” |
| `LedgerRejected` | — | 异常 | **确定未生效** |
| `LedgerCorrupted` | — | 异常 | 存储不可信；**不能**据此断言生效或未生效 |

存储：`root/ledger.json`，写入用临时文件 + `fsync` + `os.replace`，保证余额与记录整体可见。
读取时校验顶层结构与余额类型，不合法抛 `LedgerCorrupted`。

### 3.4 工具（`tools.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `SideEffect` | — | 3 值枚举 | `READ_ONLY / IDEMPOTENT / NON_IDEMPOTENT` |
| `Tool` 协议 | — | — | `name`、`side_effect`、`validate(args, ctx)`、`run(args, ctx)` |
| `ToolContext` | `run_id, step, attempt, step_key` | 冻结 dataclass | 由引擎构造；幂等键只能取自 `step_key`，不能取自 `args` |
| `InvalidArgs` | — | 异常 | 参数不合法，**工具未被调用** |
| `ToolRejected` | — | 异常 | 已调用，业务拒绝，**确定未生效** |
| `ToolCrashed` | — | 异常 | 已调用，崩溃，**效果未知** |
| `ToolRegistry` | — | — | `register / get / names`；重名抛 `DuplicateTool`，未知抛 `UnknownTool` |
| `run_tool(tool, args, ctx)` | 工具、参数、上下文 | `ToolOutcome` | 只执行一次，不重试；把结果归为 `SUCCEEDED / FAILED / UNCERTAIN` |

**分类规则（不可调换顺序）**：

| 情况 | 结果 | 约束 |
|---|---|---|
| `args` 不是映射 | FAILED，`kind=invalid_args` | 不校验、不执行 |
| `validate` 抛任何 `Exception` | FAILED | 不执行；分类依据是“有没有执行过” |
| `run` 返回映射 | SUCCEEDED | 带 `value` |
| `run` 抛 `ToolRejected` | FAILED，`kind=rejected` | 确定未生效 |
| `run` 抛 `ToolCrashed` 或其他 `Exception` | UNCERTAIN | **绝不能归为 FAILED** |

### 3.5 账本工具（`ledger_tools.py`）

| 接口 | 参数 | 副作用类别 | 说明 |
|---|---|---|---|
| `GetBalance` | `owner` | `READ_ONLY` | 未知账户返回 0；账本不可读抛 `ToolRejected` |
| `Transfer` | `src`、`dst`、`amount` | `NON_IDEMPOTENT` | 幂等键取 `ctx.step_key`；业务拒绝与非幂等都映射为 `ToolRejected` |
| `SaveReceipt` | `text` | `IDEMPOTENT` | 幂等键取 `ctx.step_key`；同一键只保留首次内容 |
| `LookupRequest` | `request_id` | `READ_ONLY` | 查到时返回 `{found, kind, amount}`，查不到返回 `{found: False}` |

**统一约束**：

1. `validate` 只读 `args`，不碰账本，因此任何校验失败都对应零副作用。
2. 幂等键一律取自 `ctx.step_key`（`run_id:step`），**不取自 `args`**：`args` 由策略
   给出，若允许从其中取键，调用方就能绕过步骤级去重。
3. `amount` 拒绝 `bool`：`True` 是 `int` 的子类，不单独判断就会被当成 1。

### 3.6 事件编码（`codec.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `encode_event(event)` | `Event` | `str` | 产出 JSON 文本，字段固定为 `run_id / seq / type / payload / at`，`type` 用枚举字符串值 |
| `decode_event(text)` | `str` | `Event` | 还原 `EventType` 与 `int`；任何读不懂的输入抛 `DecodeError` |
| `EncodeError` | — | 异常 | 事件无法无损落盘 |
| `DecodeError` | — | 异常 | 文本不是合法事件记录 |

**不变量**：

1. `decode_event(encode_event(e)) == e`，对任意可编码的事件成立。
2. 只接受「编码后解码回来一模一样」的值。`tuple` 必须拒绝，不能交给 `json` 静默转成
   `list`；映射的非 `str` 键同样拒绝；`nan` / `inf` / `-inf` 拒绝，因为 `nan != nan`
   会让往返相等性直接失效。
3. 解码时缺字段、类型不符、`type` 未知一律抛 `DecodeError`，绝不返回半成品事件。
   `seq` 显式排除 `bool`——`True` 是 `int` 的子类。
4. 本模块不判断事件顺序（`model.apply` 负责），不分配 `seq`（日志层负责），
   不校验业务字段是否齐全（各事件的 payload 契约负责）。

## 4. 依赖方向

```text
events      → 无
model       → events                      （无 I/O、无数据库、无模型服务）
codec       → events                      （只做 JSON 转换，不判断顺序、不分配 seq）
tools       → 无内部依赖                   （不认识 model / events / journal，不写事件）
journal     → events
policy      → model
engine      → policy / tools / journal / model / events
ledger_tools→ demo / tools                （只做校验、一次账本调用、异常映射）
tests       → 全部
scripts     → engine / policy / journal / ledger_tools / demo
```

`model` 不得反向依赖 `demo`、`engine`、`journal`；`tools` 不得依赖 `model`；
`ledger_tools` 不得写事件、不得分配 `seq`、不得重试、不得判断预算；
`codec` 不得判断事件合法性，也不得依赖 `journal`。

## 5. 学习者已确认的决策

| # | 决策 | 结论 | 日期 | 代价 / 可逆性 |
|---|---|---|---|---|
| D1 | 项目根目录 | `C:\Users\王佳浩\Desktop\项目2` | 2026-10-18 | 中文路径已实测可用 |
| D2 | 包名 | `agentrun` | 2026-10-18 | 与参考项目同名，仓库路径隔离 |
| D3 | 场景 | 模拟账本 alice→bob 250，阈值 500，储备线 300 | 2026-10-18 | 参数集中在 demo |
| D4 | 运行时 | Python 3.14.6，业务仅标准库，开发依赖 pytest/ruff/mypy | 2026-10-18 | 不引 pydantic |
| D5 | 版本控制 | 本地 git；最终完成时推 GitHub | 2026-10-18 | CI 仍无远端执行记录 |
| D6 | 存储边界 | 账本与引擎日志分开存储 | 2026-10-18 | 保留跨系统崩溃窗口 |
| D7 | 结果编码 | 三态 `SUCCEEDED/FAILED/UNCERTAIN`，失败详情放 `error` | 2026-10-19 | 加状态不改枚举 |
| D8 | 调用身份 | `step_key = run_id:step` 跨重试稳定，`attempt` 单独记录 | 2026-10-19 | 幂等键不随重试变化 |
| D9 | 预算 | 按决策次数计，重试不额外消耗 | 2026-10-19 | 待 engine 实现时验证边界 |
| D10 | 事件信封 | `run_id, seq, type, payload, at`，`at` 不参与判定 | 2026-10-19 | 重放确定 |
| D11 | 存储损坏处理 | 运行时校验结构，失败抛 `LedgerCorrupted`，不当作空账本 | 2026-10-20 | 多几行校验，换取“不误判为未发生” |
| D12 | 副作用分类 | 三值枚举 `READ_ONLY / IDEMPOTENT / NON_IDEMPOTENT` | 2026-10-20 | 能表达“只读”与“可安全重复”的区别 |
| D13 | 参数校验位置 | 工具自己实现 `validate(args)` | 2026-10-20 | 不造 schema 语言；重复校验的风险由契约约束 |
| D14 | 异常映射 | 三异常 + 未知异常 → UNCERTAIN | 2026-10-20 | 避免把已生效调用当失败而重做 |
| D15 | 结果类型 | tools 自己的 `ToolStatus/ToolOutcome`，engine 负责映射 | 2026-10-20 | tools 不依赖 model，代价是 3 行映射 |
| D16 | 工具签名 | `validate(args, ctx)` / `run(args, ctx)`，由 engine 构造 `ToolContext` | 2026-10-24 | 工具拿得到 `step_key`；代价是所有工具与 Fake 工具都要改签名 |
| D17 | 账本异常映射 | `LedgerRejected` / `LedgerCorrupted` 一律映射为 `ToolRejected` | 2026-10-24 | 账本的读盘先于写盘，失败时可证明零副作用；不映射成 `ToolCrashed` 是为了不把“确定没生效”说成“不知道” |
| D18 | 续跑演示的位置 | 对账策略写在 `scripts/ledger_demo.py`，不进 `policy.py` | 2026-10-24 | 它只服务这一个验收场景；进库会把演示逻辑变成公共 API |
| D19 | 编码载体 | `encode_event` 返回 `str`，不返回中间 `dict` | 2026-10-25 | 免掉“先编码成 dict、落盘时再 dumps”的第二层 |
| D20 | 非 JSON 类型 | 一律 `EncodeError`，不用 `default=str` 兜底 | 2026-10-25 | `default=str` 会把 `Decimal("1.10")` 静默变成 `"1.10"`，重放时类型已变且无法发现 |
| D21 | 编码模块位置 | 独立 `codec.py`，不并入 `events.py` | 2026-10-25 | `events.py` 保持零依赖纯契约；编解码需要 `json` 与递归校验 |

## 6. 工程检查

| 工具 | 范围 | 命令 | 当前状态 |
|---|---|---|---|
| pytest | `tests/` | `.\.venv\Scripts\python.exe -m pytest` | 实测 146 passed |
| ruff lint | 全仓库 | `.\.venv\Scripts\ruff.exe check .` | 通过 |
| ruff format | 全仓库 | `.\.venv\Scripts\ruff.exe format --check .` | 通过（24 files） |
| mypy | `src/agentrun` | `.\.venv\Scripts\python.exe -m mypy` | 通过（10 source files，0 error） |
| CI | GitHub Actions，Python 3.14 | 同上四条 | **配置已准备，未推送、未运行** |
| Docker | 未引入 | — | 触发信号：CLI 稳定且出现跨机/容器需求 |

mypy 配置：`disallow_untyped_defs`、`warn_return_any`、`warn_unused_ignores`、
`no_implicit_optional`；未开启全局 `strict`，未忽略具体错误码。

## 7. 最近一次实际验证

- 时间：2026-10-25（第 4 轮完成）
- 命令与结果：
  - `pytest` → `146 passed`
  - `ruff check .` → `All checks passed!`
  - `ruff format --check .` → `24 files already formatted`
  - `mypy` → `Success: no issues found in 10 source files`
  - `python scripts/ledger_demo.py` → 退出码 0；第一次 `1000/0 → 750/250`，
    第二次 `750/250 → 750/250`（`lookup_request` 命中后直接结束）
- 未验证：CI 远端运行、真实模型、持久化、恢复、并发

### 第 4 轮新增证据

- `tests/test_codec.py` 39 个用例全绿，其中 6 个覆盖“编码后被拒绝”的类型：
  `tuple` / `set` / `bytes` / `Decimal` / `datetime` / 自定义对象，另有嵌套 `list`
  中的 `tuple` 与 `nan` / `inf` / `-inf`。
- `run_tool` 新增运行时返回形状校验：`run` 返回非 `Mapping`（含 `None`）归为
  `UNCERTAIN`，此时工具已执行，不能声称“确定没生效”。两条测试分别锁定
  “工具被调用过一次”与“状态不是 SUCCEEDED”。

### 历史环境事件

2026-10-18：本机 pip 全局索引指向清华镜像，TLS 握手失败（`SSLEOFError`）；官方 PyPI 可用。
未修改全局 pip 配置，仅在安装命令中显式指定索引。

## 8. 当前债务

| 债务 | 影响 | 触发信号 |
|---|---|---|
| `StepView.args` 与事件 payload 共享同一 dict | 观察者修改会污染视图与日志 | 第 7 轮监听器隔离 |
| `RUN_FAILED` 的 reason 未进视图 | CLI 显示失败原因需回读事件 | 第 6 轮 CLI |
| `replay` 信任调用方给定顺序 | 乱序输入不会被发现 | 第 5 轮 Journal |
| `lookup` 不区分记录类型 | 同一 id 先后写过 transfer 与 receipt 时只能返回先写入者 | 第 6 轮对账工具 |
| `validate` 崩溃与参数不合法共用 `kind=invalid_args` | 日志无法区分“用户输入错”和“工具自身 bug” | 第 7 轮时间线 |
| `codec` 不校验 payload 的业务字段 | 形状合法但语义不全的事件能通过编码 | 第 5 轮写入前的最后校验 |
| 对账逻辑只存在于演示脚本 | 引擎自己不会在 UNCERTAIN 后主动对账 | 第 6 轮对账 |
| 账本状态文件无格式版本号 | 改 `Record` 字段后旧文件不可读 | 第一次修改账本 schema |
| `_load → 改 → _save` 无锁 | 两个实例并发写会丢更新 | v2 并发 / 多进程演示 |
| CI 未远端运行 | 流水线可用性未验证 | 建立 GitHub 远端 |
| Docker 未引入 | 无容器化交付 | CLI 稳定 + 跨机需求 |

## 9. 未来模块（未创建）

| 文件 | 计划职责 | 引入轮次 |
|---|---|---|
| `journal.py` 的 SQLite 实现 | 原子追加、`seq` 分配、跨进程持久化 | 第 5 轮 |
| `trace.py` | 时间线、JSONL、只读监听 | 第 7 轮 |
| `llm.py` | 模型适配器 | 第 8 轮 |
| `cli.py` | 入口与依赖组装 | 第 6 轮 |
