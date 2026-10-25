# PROJECT_LAYOUT — 当前契约

> 本文件只描述**已经存在**的东西。未来模块单列在末尾，不混入现状。
> 最后更新：2026-10-25（第 8 轮完成）

## 1. 里程碑与当前状态

| 项 | 状态 |
|---|---|
| 当前里程碑 | 第 8 轮完成：模型输出可解析成动作，决策失败与「可能已生效」彻底分开 |
| 已实现能力 | 包骨架、模拟账本、事件契约、事件折叠、工具协议与单次执行、策略协议与脚本策略、模型策略适配器（提示渲染 + 严格解析）、内存与 SQLite 两种事件日志、引擎主循环与预算、引擎续跑、对账补记、账本工具适配器、事件编解码、时间线与 JSONL 导出、CLI 五个子命令 |
| 未实现能力 | 真实模型服务调用（需联网与密钥）；提示长度预算 |
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
│  ├─ ledger_demo.py         账本工具演示：一次完整运行 + 一次对账式续跑（教学脚本）
│  ├─ recovery_demo.py       落盘与恢复演示：重开读回、崩溃现场重放（教学脚本）
│  ├─ reconcile_demo.py      崩溃 → 对账 → 续跑的完整链路（教学脚本）
│  └─ model_policy_demo.py   模型策略：合法输出、解析失败、重启续跑（教学脚本，离线）
├─ src/agentrun/
│  ├─ __init__.py            包入口，__version__
│  ├─ demo.py                模拟账本（外部世界）：Ledger / Record / LedgerRejected / LedgerCorrupted
│  ├─ events.py              事件契约：EventType / Event / make_step_key
│  ├─ model.py               RunStatus / Outcome / InvalidEvent / StepView / RunView / apply / replay
│  ├─ codec.py               CodecError / EncodeError / DecodeError / encode_event / decode_event
│  ├─ tools.py               SideEffect / Tool 协议 / 异常 / ToolOutcome / ToolContext / ToolRegistry / run_tool
│  ├─ journal.py             EventLog 协议 / MemoryEventLog / SqliteEventLog（原子追加、跨进程持久化）
│  ├─ policy.py              ToolCall / Finish / Action / PolicyError / Policy 协议 / ScriptedPolicy
│  ├─ llm.py                 ToolSpec / ModelClient 协议 / build_prompt / parse_action / ModelPolicy
│  ├─ engine.py              StopReason / EngineResult / NothingToResume / Engine（循环、预算、续跑）
│  ├─ ledger_tools.py        账本工具适配器：GetBalance / Transfer / SaveReceipt / LookupRequest
│  ├─ reconcile.py           ReconcileResult / NothingToReconcile / pending_step / reconcile
│  ├─ trace.py               TraceLine / summarize / timeline / export_jsonl（只读观察）
│  └─ cli.py                 argparse 入口：run / resume / status / reconcile / trace
└─ tests/
   ├─ test_package_smoke.py     包冒烟断言
   ├─ test_demo_ledger.py       账本：余额、三条拒绝规则、幂等回执、对账查询
   ├─ test_model_contract.py    事件折叠契约：顺序、seq、终态、结果唯一性
   ├─ test_codec.py             事件编解码：往返无损、非 JSON 类型拒绝、坏文本拒绝
   ├─ test_tools_contract.py    工具执行：校验零副作用、异常分类、注册表
   ├─ test_journal_contract.py  内存日志：seq 按 run 独立分配
   ├─ test_journal_sqlite.py    SQLite 日志：seq 连续、重开读回、原子回滚、并发不重号
   ├─ test_policy_contract.py   脚本策略：按 len(steps) 取动作、脚本耗尽
   ├─ test_llm.py               模型策略：提示只由视图决定、输出严格解析、客户端失败
   ├─ test_engine_contract.py   引擎：事件顺序、预算边界、UNCERTAIN 立即停止、决策失败
   ├─ test_engine_resume.py     续跑：终态不重跑、未完成步骤先对账、预算取自日志
   ├─ test_ledger_tools.py      账本工具：参数校验、异常映射、幂等键取自 ctx
   ├─ test_reconcile.py         对账：查到才补记、查不到保持未知、补记后可续跑
   ├─ test_trace.py             时间线：九种事件可渲染、JSONL 可回读、不修改原事件
   └─ test_cli.py               CLI：脚本解析、子命令退出码、状态与时间线输出
```

**故意不存在**：真实模型客户端（HTTP、密钥、超时重试）。`llm.py` 只定义协议与解析，
不 import 任何模型 SDK，因此全仓库仍然零运行时依赖。

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

### 3.7 事件日志（`journal.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `EventLog` 协议 | — | — | `append(run_id, type_, payload)` 与 `read(run_id)` |
| `MemoryEventLog` | — | 实例 | 内存实现，仅用于 v0 与测试 |
| `SqliteEventLog(path)` | 数据库文件路径 | 实例 | 跨进程持久化；构造时不连接、不建表 |

**表结构**：

```sql
runs   (run_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)
events (run_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY (run_id, seq))
```

`body` 存 `encode_event` 的完整输出，`codec` 是唯一格式来源。

**不变量**：

1. `seq` 按 `run_id` 独立计数，从 1 连续递增；跨实例、跨线程都不重号。
2. `append` 原子：先 `BEGIN IMMEDIATE` 拿写锁，再「读 `last_seq` → 构造 `Event` →
   `encode_event` → 写 `runs` → 写 `events` → `COMMIT`」。任一步失败整条回滚，
   **不消耗 seq**——否则下一次正常写入会出现缺口，恢复时会被误判为丢事件。
3. `read` 按 `seq` 升序返回；未知 `run_id` 返回空元组。`events.seq` 列与 `body`
   内编码的 `seq` 不一致时抛 `DecodeError`，不以列为准继续。
4. `sqlite3.connect(..., isolation_level=None)` 是硬要求：默认的隐式 BEGIN 是
   DEFERRED，两个连接会读到同一个 `last_seq`。

### 3.8 引擎（`engine.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `Engine.run(run_id, task, max_steps)` | 新任务 | `EngineResult` | 写 `RUN_STARTED` 后进入主循环 |
| `Engine.resume(run_id)` | 已存在的任务 | `EngineResult` | 不写 `RUN_STARTED`，预算取 `view.max_steps` |
| `NothingToResume` | — | 异常 | 日志里没有该任务的任何事件 |

**续跑的三种分支**（都不追加事件）：

1. 视图已是终态 → 原样返回，`stop_reason` 由状态推出。
2. 最后一步 `STEP_STARTED` 但没有结果 → 返回 `UNCERTAIN`。外部世界可能已经改变，
   既不能重做也不能跳过，必须先对账。
3. 其余 → 继续主循环。

第 2 条是这层的核心：宁可停着不动，也不拿「重试一次看看」去赌一个非幂等调用没生效。

**决策失败**：`policy.decide` 抛 `PolicyError`（`ScriptExhausted` 也是它）→ 追加
`RUN_FAILED`，停止，原因 `FAILED`。此时一条 `STEP_DECIDED` 都没有，因此可以断言零副作用；
不能记成 `UNCERTAIN`——那个状态只留给「可能已经改了」。

### 3.9 对账（`reconcile.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `pending_step(view)` | 视图 | `int \| None` | 第一个「已开始、无结果」的步骤号 |
| `reconcile(log, run_id, lookup)` | 日志、任务、只读查询工具 | `ReconcileResult` | 查到就补记，查不到不写 |
| `NothingToReconcile` | — | 异常 | 没有待对账的步骤 |

**三条走向**：

1. `found=True` → 追加 `STEP_COMPLETED`，该步变为成功；携带的是**账本记录**，不是原
   工具返回值（后者随进程一起丢了）。
2. `found=False` → **不写任何事件**。查不到只说明此刻查不到，不能据此断言未生效。
3. 查询失败 → 同样不写，原因写进 `detail`。

### 3.10 CLI（`cli.py`）

| 子命令 | 关键参数 | 退出码 |
|---|---|---|
| `run` | `--run-id --script [--task] [--max-steps]` | 0 成功 / 1 失败或耗尽 / 2 参数或脚本错 |
| `resume` | `--run-id --script` | 同上；任务不存在时 2 |
| `status` | `--run-id` | 0 / 任务不存在时 2 |
| `reconcile` | `--run-id` | 0 已补记 / 2 无法确认或无需对账 |

公共参数 `--db`（默认 `agentrun.db`）与 `--ledger`（默认 `ledger`）。

CLI 只装配 `ScriptedPolicy`：它只依赖 `len(view.steps)`，所以 `resume` 需要重新给一份
同样的脚本。策略属于调用方，不属于日志。脚本写短时下一次决策抛 `PolicyError`，任务记
`RUN_FAILED`（退出码 1），不再把异常栈抛给用户。

### 3.11 时间线（`trace.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `timeline(events)` | 事件可迭代对象 | `tuple[TraceLine, ...]` | 与输入等长、顺序不变 |
| `summarize(event)` | 一条事件 | `str` | 一句话说明；字段缺失用空串占位，不抛异常 |
| `export_jsonl(events, path)` | 事件、目标路径 | `int` | 一行一条，返回行数 |

**不变量**：

1. 只读：不写事件、不分配 `seq`、不改变任何状态。
2. `timeline` 不排序、不校验顺序——乱序本身是应该被看见的问题。
3. `TraceLine` 只含 `seq` / `at` / `kind` / `summary`，不携带 `payload` 引用，
   观察者无从污染日志。
4. JSONL 每行就是 `encode_event` 的输出，可被 `decode_event` 逐行读回；换行固定 `\n`。
5. 观察者与日志的隔离在 `model.apply` 侧保证：`StepView.args` 是 `payload` 的深拷贝。

### 3.12 模型策略（`llm.py`）

| 接口 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `ToolSpec` | `name`、`description` | 冻结 dataclass | 只用于提示；名字必须与注册表一致，拼错由引擎判为未知工具 |
| `ModelClient` 协议 | — | — | 唯一方法 `complete(prompt) -> str`；不解析、不碰日志 |
| `build_prompt(view, tools)` | 视图、工具清单 | `str` | 纯函数；工具按名字排序，渲染不抛异常 |
| `parse_action(text)` | 模型原始输出 | `Action` | 严格解析；任何不合约定抛 `PolicyError` |
| `ModelPolicy(client, tools)` | 客户端、工具清单 | 策略实例 | `decide` = 渲染 → 调用 → 解析；无缓存、无游标 |
| `PolicyError` | — | 异常 | 决策失败；引擎写 `RUN_FAILED`，不是 `UNCERTAIN` |

**提示结构**（固定三段，无第四段）：任务与预算、已执行步骤（`序号. 工具(参数) → 结果`）、
可用工具与输出约定。时间、进程号、随机数不得进入提示，否则同一视图渲染出不同文本。

**解析规则**（全部为硬性拒绝）：

| 情况 | 结果 |
|---|---|
| 非 `str`、空白、非 JSON、JSON 非对象 | `PolicyError` |
| `tool` 与 `finish` 同时出现或都不出现 | `PolicyError` |
| 调用工具时出现 `args` 之外的字段；结束任务时出现 `args` | `PolicyError` |
| `tool` 不是非空字符串、`args` 不是对象 | `PolicyError` |
| `finish` 不是非空字符串（含 `250` 这种数字） | `PolicyError` |
| Markdown 代码块、前后多余文字 | `PolicyError`（不剥壳、不截取） |
| 工具名不在注册表里 | **放行**，由引擎记「未注册的工具」→ `RUN_FAILED` |

**不变量**：

1. `build_prompt` 只依赖 `view` 与工具集合；同一视图两次渲染逐字节相同。
2. `ModelPolicy` 不保存提示、输出或步数；连续两次用同一视图 `decide`，两次提示相同。
3. 客户端抛任何 `Exception` → `PolicyError`；一个工具都没调用，外部世界未变。
4. 不可 JSON 化的 `args` 在提示里用 `repr` 兜底：提示不是日志，不承担可落盘承诺。

## 4. 依赖方向

```text
events      → 无
model       → events                      （无 I/O、无数据库、无模型服务）
codec       → events                      （只做 JSON 转换，不判断顺序、不分配 seq）
tools       → 无内部依赖                   （不认识 model / events / journal，不写事件）
journal     → events / codec              （分配 seq、落盘；不判断事件是否合法）
policy      → model
llm         → policy / model              （不认识工具实现、日志、引擎）
engine      → policy / tools / journal / model / events
ledger_tools→ demo / tools                （只做校验、一次账本调用、异常映射）
reconcile   → journal / model / tools / events
trace       → events / codec              （只读观察，不写日志、不判断合法性）
cli         → engine / journal / policy / tools / reconcile / trace / ledger_tools / demo
tests       → 全部
scripts     → engine / policy / llm / journal / ledger_tools / reconcile / demo
```

`model` 不得反向依赖 `demo`、`engine`、`journal`；`tools` 不得依赖 `model`；
`ledger_tools` 不得写事件、不得分配 `seq`、不得重试、不得判断预算；
`codec` 不得判断事件合法性，也不得依赖 `journal`；`llm` 不得 import 任何模型 SDK、
不得写日志、不得判断工具是否存在。

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
| D22 | 表结构 | `runs(run_id, last_seq)` + `events(run_id, seq, body)` 两张表 | 2026-10-25 | 计数器是独立事实；`SELECT last_seq` + `UPDATE` 比 `MAX(seq)+1` 子查询更好排查，也不依赖 `RETURNING`（SQLite 3.35+） |
| D23 | `body` 内容 | 存整条 `encode_event` 输出，同时保留 `seq` 列做主键与排序 | 2026-10-25 | `codec` 保持唯一格式来源；多一列换来 seq 一致性校验点 |
| D24 | 连接策略 | 每次 `append` / `read` 新开连接，不持有长连接 | 2026-10-25 | 无状态，“重启”等于换实例；代价是每次多一次连接开销 |
| D25 | 续跑预算 | 取日志里的 `view.max_steps`，不接收新参数 | 2026-10-25 | 预算属于任务；续跑时改预算等于事后改规则，日志里看不出来 |
| D26 | 续跑已终结任务 | 返回现有视图 + 对应 `StopReason`，不抛异常、不追加事件 | 2026-10-25 | CLI 里“续跑一个早就成功的任务”是常见误操作，返回现状比栈信息有用 |
| D27 | CLI 策略来源 | 读 JSON 脚本转成 `ScriptedPolicy`，用标准库 `argparse` | 2026-10-25 | 第 8 轮换模型策略时 CLI 不用动；不引 click，符合 D4 |
| D28 | 观察者隔离方式 | `model.apply` 对 `args` 深拷贝，`TraceLine` 不携带 payload 引用 | 2026-10-25 | 浅拷贝挡不住嵌套改动；深拷贝的代价是每个 `STEP_DECIDED` 多一次复制 |
| D29 | 校验失败的两种 kind | `invalid_args`（输入错）/ `validator_crashed`（工具自身 bug） | 2026-10-25 | 两者都归 FAILED（工具未执行），但只有后者需要改代码 |
| D30 | 决策失败的落点 | `PolicyError` → `RUN_FAILED` + `StopReason.FAILED` | 2026-10-25 | 决策阶段零副作用，说“可能已生效”会毁掉 UNCERTAIN 的含义；代价是主循环多一个 `except` |
| D31 | 模型输出解析 | 严格：不剥代码块、不截取花括号、不转类型 | 2026-10-25 | 容错会让“没按约定输出”变成“有时能跑”，日志里看不出差别；代价是真模型首次接入会在格式上失败几次 |
| D32 | 工具是否存在的判定位置 | 仍由引擎判（`UnknownTool`），`llm` 不查注册表 | 2026-10-25 | 只有注册表知道答案；策略多查一次就会造出第二份“哪些工具可用”的事实 |
| D33 | `ScriptExhausted` 的归属 | 继承 `PolicyError` | 2026-10-25 | 脚本用完与模型乱输出是同一类事：给不出动作。统一之后 CLI 由栈信息变成 `RUN_FAILED`（退出码 1） |

## 6. 工程检查

| 工具 | 范围 | 命令 | 当前状态 |
|---|---|---|---|
| pytest | `tests/` | `.\.venv\Scripts\python.exe -m pytest` | 实测 236 passed |
| ruff lint | 全仓库 | `.\.venv\Scripts\ruff.exe check .` | 通过 |
| ruff format | 全仓库 | `.\.venv\Scripts\ruff.exe format --check .` | 通过（37 files） |
| mypy | `src/agentrun` | `.\.venv\Scripts\python.exe -m mypy` | 通过（14 source files，0 error） |
| CI | GitHub Actions，Python 3.14 | 同上四条 | **配置已准备，未推送、未运行** |
| Docker | 未引入 | — | 触发信号：CLI 稳定且出现跨机/容器需求 |

mypy 配置：`disallow_untyped_defs`、`warn_return_any`、`warn_unused_ignores`、
`no_implicit_optional`；未开启全局 `strict`，未忽略具体错误码。

## 7. 最近一次实际验证

- 时间：2026-10-25（第 8 轮完成）
- 命令与结果：
  - `pytest` → `236 passed`（新增 36 例：`test_llm.py` 33 例、引擎 2 例、策略 1 例）
  - `ruff check .` → `All checks passed!`
  - `ruff format --check .` → `37 files already formatted`
  - `mypy` → `Success: no issues found in 14 source files`
  - `python scripts/model_policy_demo.py` → 退出码 0：场景 A 三份提示、8 条事件、
    `SUCCEEDED`、余额 `750/250`；场景 B 只有 `RUN_STARTED` + `RUN_FAILED`、0 步、
    余额 `1000/0`；场景 C 崩在第 1 步之后，重启后提示指纹同为 `6fec2dbedd27`，
    续跑至 `SUCCEEDED`、余额 `750/250`
  - `python -m agentrun.cli run`（脚本 `get_balance` + `finish`）→ `SUCCEEDED`，
    退出码 0
  - 同一 CLI 用只含 1 步的脚本跑新任务 → `RUN_FAILED`、退出码 1；改动前是未捕获的
    `ScriptExhausted`
- 未验证：CI 远端运行、真实模型服务、多进程并发

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
| `RUN_FAILED` 的 reason 未进视图 | CLI 显示失败原因需回读事件 | 需要结构化失败原因时 |
| `replay` 信任调用方给定顺序 | 乱序输入不会被发现（`SqliteEventLog` 已用 `ORDER BY seq` 缓解，`replay` 本身仍不校验） | 接入外部事件源时 |
| `trace` 只能一次性读取 | 没有“边跑边看”的实时通道，只能事后 `timeline` 或导出 | 需要实时观察长任务时 |
| `lookup` 不区分记录类型 | 同一 id 先后写过 transfer 与 receipt 时只能返回先写入者 | 第 6 轮对账工具 |
| `codec` 不校验 payload 的业务字段 | 形状合法但语义不全的事件能通过编码 | 第 7 轮写入前的最后校验 |
| `reconcile` 只能补记成功 | 查到记录才有补记路径；判定「确实没生效」需要别的证据来源 | 需要人工判定失败时 |
| 模型不保证确定性 | 同一提示两次可能给出不同动作；续跑结果取决于模型侧的温度设置 | 接真实模型服务时 |
| 提示没有长度预算 | 长 `result` 会把提示撑大，可能超出上下文 | 结果变长或接真实服务时 |
| CLI 仍只装配脚本策略 | 通过命令行无法直接使用模型策略，要写装配代码 | 接真实模型服务时 |
| 账本状态文件无格式版本号 | 改 `Record` 字段后旧文件不可读 | 第一次修改账本 schema |
| `_load → 改 → _save` 无锁 | 两个实例并发写会丢更新 | v2 并发 / 多进程演示 |
| CI 未远端运行 | 流水线可用性未验证 | 建立 GitHub 远端 |
| Docker 未引入 | 无容器化交付 | CLI 稳定 + 跨机需求 |

## 9. 未来模块（未创建）

| 文件 | 计划职责 | 引入轮次 |
|---|---|---|
| `model_client.py` | 真实模型客户端：HTTP、密钥、超时与重试策略 | 决定接真实服务时 |
