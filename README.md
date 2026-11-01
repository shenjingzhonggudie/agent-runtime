# Agent 任务执行引擎（agentrun）

[![CI](https://github.com/shenjingzhonggudie/agent-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/shenjingzhonggudie/agent-runtime/actions/workflows/ci.yml)

以持久化事件为事实来源、根据工具反馈决定下一步动作、并能在明确安全边界内恢复的轻量 Agent 执行引擎。

默认场景为隔离目录中的**模拟账本**（余额查询、非幂等转移、幂等回执、按调用标识对账），不连接任何真实支付、银行或邮件系统。

## 当前状态

功能已收尾（2026-11-01）：18 轮迭代全部完成，本地 439 个用例、`mypy`、`ruff` 与 CI 全绿，镜像构建与容器内运行已实跑验证，真实模型 8 个场景 8/8 通过。剩下两批（进程内重试、工具调用超时）是明确留给下一版的决定，不是遗漏——边界见下方「边界」。

展示入口使用 DeepSeek 真实模型（`deepseek-flash`）：从本机配置读取凭据，经 LangChain 客户端取得决策，再由引擎执行工具和记录事件。账本仍是隔离的本地模拟账本；脚本策略保留供测试和明确选择时使用。

- 事件落盘到 SQLite，进程重启后 `replay` 出的视图与停止前完全一致。
- 单次工具调用分三类结果：`SUCCEEDED` / `FAILED`（确定没生效）/ `UNCERTAIN`（不知道）。
- 遇到 `UNCERTAIN` 引擎立即停止；对账查到已生效则补记结果，查不到则保持未知，绝不自动重做非幂等调用。
- `trace` 把事件渲染成时间线，也能导出 JSONL；观察者拿到的都是副本，改不动日志。
- 模型策略把视图渲染成提示、把模型输出严格解析成 `ToolCall` / `Finish`；不联网，客户端由调用方注入。
- 解析失败、模型超时、脚本写短一律 `PolicyError`：一个工具都没调用，外部世界未变。标了 `retryable` 的写 `DECISION_FAILED` 并暂停（任务仍是 `RUNNING`，可 `resume`），攒满 `MAX_DECISION_FAILURES` 或不可重试的才写 `RUN_FAILED`。
- 提示只由视图与工具清单决定，重启后同一个视图得到同一份提示；模型模式续跑不需要脚本，脚本模式仍须提供原脚本。
- 自动对账查不到记录时不再停在「不知道」，而是写 `REVIEW_REQUESTED` 进入 `WAITING_REVIEW`，等人工给结论。
- 复核结论三种：`applied` 补记结果、`not_applied` 允许把调用当作新步骤重做、`abort` 终止任务（`ABORTED`）。提交时带 `resolution_id`，同一个键重复提交只写一条结论，第二次按成功返回。
- 等待复核期间引擎一步也不走：`resume` 只把状态带回来，不会替人猜。
- 进程级故障矩阵：`after_decided`（决策已落盘）、`before_tool`（意图已落盘）、`after_tool`（副作用已发生）三个窗口都用 `os._exit` 真退进程，再用新进程恢复。
- 只有决策、没有意图时复用已落盘的决策，不重新问策略——参数不会被换掉。
- `STEP_UNCERTAIN` 不是终局：对账命中补记成功，查不到转人工，收敛之前 `resume` 一步不走。
- 真实模型评测覆盖 8 个场景，打印停止原因、步数、事件数、余额、模型调用次数与耗时。
- `RUN_FAILED` 的原因进入视图，`status` 直接打印「为什么失败」，不必再翻事件。
- 提示里超长的参数与结果按 `field_limit`（默认 200 字）截断并标注截掉多少；截断是确定性的，同一视图仍渲染出同一份提示。
- `HttpModelClient` 通过 LangChain `ChatOpenAI` 发 OpenAI 兼容请求：配置温度 0，关闭自动重试、流式、Responses API 与缓存；错误信息不含密钥与提示。失败按状态码与响应形状标记 `retryable`（401 不重试，429 / 5xx / 超时重试），分类只在客户端做，引擎只读这个标记。
- `run` / `resume --policy model` 从本地配置与环境变量创建客户端；命令行仍兼容显式脚本模式。
- 自动测试使用本地 HTTP 服务和隔离配置；真实服务联调与演示另外执行，不让 pytest 读取本机密钥。
- 多 worker：`--owner` 启用租约，每次写事件带 `epoch` 栅栏；租约过期后可被接管，旧 worker 的写入被拒绝（`LOST`），任务状态不变。
- 工具调用期间有后台心跳按 `ttl/3` 续租：一次慢调用不会让租约过期，接手者也不会在「这一步还在执行」时闯进来。心跳失败只置 lost、不打断正在跑的工具——等它返回，下一次写入由栅栏拒绝。
- 栅栏只保护日志：它不能阻止旧 worker 已经发出的工具调用，那部分仍靠对账与复核兜底。
- `scripts/evidence/lease_worker.py` 与 `tests/test_process_takeover.py` 用两个真实进程验证接管。
- 日志读侧验真：缺号、末尾被删、事件归属别的 `run_id` 一律抛 `JournalCorrupted`，不返回残缺序列；两次 SELECT 在同一个只读事务里，不会被并发写者插队。
- 每条事件另存上一条的摘要，`verify` 重算全链并指出第一个断点：把中间一条删掉、把后面那条重写成合法 `seq`、再改小 `last_seq`，序列会重新自洽，`read` 看不出问题——只有链会指错。迁移前的记录没有摘要，`verify` 会如实说「这段无法自证」。
- 账本所有公开方法都在 `ledger.lock` 的跨进程独占锁里完成读改写；拿不到锁抛 `LedgerBusy`（**确定未生效**），工具层映射成「业务拒绝」而不是「结果未知」。锁由内核持有，进程被杀即释放。
- 一次 `run` / `resume` 结束时（成功、失败、耗尽、未知、失联都算）归还租约：干净退出的任务下一个 worker 立刻能接手，只有崩溃才等 TTL。`status` 会打印租约持有者与剩余时间，过期则标「可被接管」。

## 边界

这套东西解决的是「任务在崩溃、接管、副作用不确定的时候如何不撒谎」，不是「什么任务都能跑」：

- **账本是模拟的**：隔离目录里的 JSON 文件，不连任何真实支付、银行或邮件系统。
- **模型调用不重试**：客户端显式 `max_retries=0`。一次 429 或超时写成 `DECISION_FAILED` 并暂停，要 `resume` 才继续。
- **工具调用没有超时**：工具卡在死循环里时心跳照常续租，只能由人杀进程、由 TTL 兜底。
- **联调范围有限度**：8 个场景在 DeepSeek 上跑通（8/8、15 次真实调用），覆盖成功、业务拒绝、预算耗尽、只读、幂等重放、对账、复核重做与终止。429 限流、401 鉴权失败、上下文超长只有离线契约测试覆盖；仓库里不放凭据。
- **租约依赖本机时钟**：跨机器部署时 `expires_at` 不可比。
- **镜像只验证过 linux/amd64**：没有 arm64 构建，也没有 `HEALTHCHECK` 与资源限额。

完整清单（含每条债务的触发条件）见 `PROJECT_LAYOUT.md` §8。

想先看「为什么这样设计」，读 [`docs/design.md`](docs/design.md)：三个选择（事件日志、栅栏、不用 ReAct）与各自的代价。

想先看「它到底跑通了什么」，读 [`docs/scenarios.md`](docs/scenarios.md)：8 个真实模型场景的期望结局，以及每个场景对应的离线契约测试。

实现契约与接口见 `PROJECT_LAYOUT.md`，逐轮的取舍与返工见 `LEARNING_LOG.md`。

## 5 分钟：从空目录到余额变化

```powershell
# 1. 建账本：A 有 1000，B 是 0
.\.venv\Scripts\python.exe -m agentrun.cli ledger init --A=1000 --B=0

# 2. 用一句话让它跑：模型自己选工具、自己填参数
.\.venv\Scripts\python.exe -m agentrun.cli ask "从账户 A 转 250 到账户 B"

# 3. 再确认一次余额
.\.venv\Scripts\python.exe -m agentrun.cli ledger balance
```

第二步的实际输出（模型措辞每次不同，形状不变）：

```
任务：从账户 A 转 250 到账户 B

结论：任务已完成：已从账户 A 向账户 B 转账 250，并通过 lookup_request 确认该笔转账已生效。
步数：4/6
余额：
  A: 750
  B: 250
停止原因：SUCCEEDED
```

不需要看 `scripts/`：`ask` 会自动生成 `run_id`、装配模型策略、执行工具，并打印结论、步数与余额。没跑成时它会直接把下一步该敲的 `status` / `resume` 写出来。

三种入口的分工：

| 命令 | 什么时候用 |
|---|---|
| `ask` | 第一次用，或者只想看结果 |
| `run` / `resume` | 要指定 `run_id`、用脚本策略、或多 worker 时 |
| `status` / `trace` / `verify` | 回看、排障、证明日志没被改过 |

## 环境准备

已验证环境：Windows + Python 3.14.6。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -e ".[dev]"
```

本机 pip 全局配置指向清华镜像，2026-10-18 实测该镜像 TLS 握手失败（`SSLEOFError`），官方 PyPI 可用。如再次遇到，追加 `--index-url https://pypi.org/simple`，不要为此修改全局 pip 配置。

**必须用 `.venv` 里的解释器**：`agentrun` 只装在虚拟环境中，用系统 Python 会得到 `ModuleNotFoundError`。

## 在 Docker 里跑

不想装 Python 也可以用容器。镜像只装运行依赖（不含 pytest / ruff / mypy），数据落在命名卷 `agentrun-data` 里。

```powershell
# 构建（中国大陆网络可换源：加 --build-arg PIP_INDEX=...）
docker compose build

# 建账本
docker compose run --rm agentrun ledger init --A=1000 --B=0

# 用一句话跑
docker compose run --rm agentrun ask "从账户 A 转 250 到账户 B"

# 校验事件链
docker compose run --rm agentrun verify
```

模型配置走环境变量，镜像里不含 `agentrun.local.toml`：

```powershell
$env:AGENTRUN_BASE_URL = "https://api.example.com/v1"
$env:AGENTRUN_API_KEY = "..."
$env:AGENTRUN_MODEL = "..."
docker compose run --rm agentrun ask "从账户 A 转 250 到账户 B"
```

容器内工作目录固定 `/data`，所以默认的 `agentrun.db` 与 `ledger/` 都落在卷里，不用每次传路径。想换成宿主机目录，把 `agentrun-data:/data` 换成 `./state:/data`，并把宿主机目录属主改成容器内的 uid `10001`（Windows 的 Docker Desktop 不需要）。

Windows PowerShell 下不需要交互输入时加 `-T`：compose 会跳过申请伪终端，进度信息不再和程序输出抢同一块屏幕。

```powershell
docker compose run --rm -T agentrun ledger balance
```

两条限制：

- 多容器共享同一个卷并同时写入时，SQLite 的文件锁只在同一台宿主机、同一文件系统下可靠；NFS / SMB 上不成立。
- 镜像里没有 `agentrun.local.toml`，也不会读宿主机那一份。配置只能走环境变量或显式挂载。

## 命令行

展示时使用模型策略，任务输入为自然语言：

```powershell
.\.venv\Scripts\python.exe -m agentrun.cli run --run-id demo-r1 --policy model `
  --task "查询 alice 的余额，得到结果后结束" --max-steps 4
```

下面是保留的显式脚本模式，用于学习确定性动作和离线排障。它不调用模型。JSON 中每项要么调用工具，要么结束：

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

# 多 worker：带 owner 启用租约；另一个 owner 会得到「租约忙」
.\.venv\Scripts\python.exe -m agentrun.cli run --run-id r1 --script task.json --owner w1

# 查看事件与每一步状态
.\.venv\Scripts\python.exe -m agentrun.cli status --run-id r1

# 接着上次的进度继续（策略不持久化，需要重新给脚本）
.\.venv\Scripts\python.exe -m agentrun.cli resume --run-id r1 --script task.json

# 为「已开始、没有结果」的那一步补记结论；查不到则转入人工复核
.\.venv\Scripts\python.exe -m agentrun.cli reconcile --run-id r1

# 写下人工复核结论：applied / not_applied / abort；--resolution-id 是幂等键
.\.venv\Scripts\python.exe -m agentrun.cli resolve --run-id r1 --decision applied `
  --note "账本记录与 step_key 一致" --result '{"found": true, "amount": 250}' `
  --resolution-id review-0001

# 打印时间线，或导出成一行一条的 JSONL
.\.venv\Scripts\python.exe -m agentrun.cli trace --run-id r1
.\.venv\Scripts\python.exe -m agentrun.cli trace --run-id r1 --export trace.jsonl

# 校验事件哈希链：删掉中间一条再改小 last_seq 也逃不掉
.\.venv\Scripts\python.exe -m agentrun.cli verify --db agentrun.db
```

默认 `--db agentrun.db`、`--ledger ledger`，账本与事件日志必须分开存储（D6）。

退出码：`0` 表示任务成功（或 `resolve` / `reconcile` 成功写入，含同一个 `resolution_id` 的重复提交）；`1` 表示任务失败、预算耗尽、决策失败待续跑或仍在等待复核；`2` 表示参数、脚本、日志或复核时机有问题。

安装为命令后可直接用 `agentrun <子命令>`。

脚本是策略的一种：写短了不会抛栈，下一次决策会记 `RUN_FAILED`，退出码 1。

## 模型策略与 LangChain 客户端

`llm.py` 只定义适配器，不绑定任何模型服务：客户端只需实现 `complete(prompt) -> str`。

```python
from agentrun.engine import Engine
from agentrun.llm import ModelPolicy, ToolSpec
from agentrun.model_client import client_from_env

client = client_from_env()
policy = ModelPolicy(client, [ToolSpec("get_balance", "读取账户余额")])
result = Engine(policy, registry, log).run("r1", "查一下 alice 的余额", max_steps=5)
```

安装依赖时，`pyproject.toml` 会安装 `langchain-openai`；不需要引入整个 LangChain Agent 框架。
客户端仍提供 `complete(prompt) -> str`，工具调用、事件记录与恢复由本项目负责。

项目根目录的 `agentrun.local.toml` 保存本机配置，已被 `.gitignore` 忽略。本机已配置好，不需要在每个终端重新输入密钥。新环境可将 `agentrun.example.toml` 复制为该文件，再填写自己的密钥：

```toml
[model]
base_url = "https://api.deepseek.com"
api_key = "填写自己的密钥"
model = "deepseek-flash"
timeout = 60.0
reasoning_effort = "none"
```

模型名及地址依据 [DeepSeek 官方接入文档](https://api-docs.deepseek.com/quick_start/agent_integrations/)。`reasoning_effort = "none"` 用于关闭思考模式，缩短这些简单工具决策的等待时间，参见 [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。

`client_from_env()` 按字段优先取环境变量，再取本地 TOML：`AGENTRUN_BASE_URL`、`AGENTRUN_API_KEY`、`AGENTRUN_MODEL`、`AGENTRUN_TIMEOUT`、`AGENTRUN_REASONING_EFFORT`。`AGENTRUN_CONFIG` 可指定另一份配置文件；默认路径按源码所在的项目根目录定位，不依赖当前终端目录。缺配置会明确失败，不会回退到假模型。

命令行显式选择模型策略，不传 `--script`：

```powershell
.\.venv\Scripts\python.exe -m agentrun.cli run --run-id model-r1 --policy model `
  --task "查询 alice 的余额并说明结果" --max-steps 5

.\.venv\Scripts\python.exe -m agentrun.cli resume --run-id model-r1 --policy model
```

模型模式创建任务时必须提供非空 `--task`；模型模式不能同时传 `--script`。
默认脚本模式仍需 `--script`，原有命令不用修改。
策略种类和模型配置不写入任务日志，续跑模型任务时须再次选择 `--policy model` 并保留本地配置或提供环境变量。
`resume` 沿用日志中的任务描述、预算和已落盘决策；结果未知或等待复核时不会调用模型或重做工具。
使用 `--owner` 启用租约时，`--lease-ttl` 应覆盖一次模型或工具调用的耗时并留出余量，
例如请求超时 30 秒时可设置 `--lease-ttl 60`；当前没有后台续租线程。

客户端只接收标准 Chat Completions 的非空字符串回答，不使用 SDK 原生工具执行或结构化输出。
LangChain/服务对某些模型的温度参数可能有限制；相同提示也不保证模型每次回答相同。
本地 HTTP 测试覆盖 LangChain 请求、坏响应、超时、零自动重试和错误脱敏。它们不请求付费模型；真实演示会联网，并按服务实际用量计费。

## 真实模型展示

在项目根目录运行，均自动读取本地配置：

```powershell
# 建议先看：真实模型选择工具、得到反馈，并展示中断后续跑
.\.venv\Scripts\python.exe scripts\demo\model_policy_demo.py

# 再看：账本工具、持久化重放、对账、人工复核
.\.venv\Scripts\python.exe scripts\demo\ledger_demo.py
.\.venv\Scripts\python.exe scripts\demo\recovery_demo.py
.\.venv\Scripts\python.exe scripts\demo\reconcile_demo.py
.\.venv\Scripts\python.exe scripts\demo\review_demo.py

# 崩溃窗口对比，以及 8 个真实模型评测场景
.\.venv\Scripts\python.exe scripts\demo\crash_window_demo.py
.\.venv\Scripts\python.exe scripts\eval_suite.py
```

每个展示打印实际模型请求次数，不会在网络失败时改用预设回答。模型得到历史步骤与工具说明，返回 JSON 动作；工具参数也来自模型回答。引擎负责校验、记录和执行。已经完成、正在等复核或结果未知的任务会按恢复规则停止，可能不需要新的模型请求。

这里的真实部分是模型服务。余额和转账发生在隔离的模拟账本，崩溃与丢响应由演示代码明确注入；人工复核结论由演示者给出，不交给模型替人审批。`scripts/demo/crash_window_demo.py` 先获得真实模型决策，再在三种存储设计中重放同一动作作对比，属于教学实验。

## 本地检查

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m mypy
```

`eval_suite.py` 是另外执行的联网展示评测。8 个场景核对任务状态、余额与调用次数；运行失败会返回非零退出码。模型输出存在变化，某次通过不代表所有输入均能成功。

提交前运行上述离线检查；CI 运行 pytest、ruff 检查、格式检查与 mypy。真实模型演示按需单独运行，不接入自动 CI。

## 目录

```text
src/agentrun/        包本体：账本、事件、折叠、编解码、工具、策略、模型适配器与客户端、日志、引擎、对账、CLI
tests/               契约测试
scripts/             教学脚本：崩溃窗口、账本工具、恢复重放、对账续跑、模型策略、人工复核、进程级故障注入、评测集
.github/workflows/   CI 配置
agentrun.example.toml  可提交的配置模板，不含真实凭据
agentrun.local.toml    本机模型配置，被 Git 忽略
PROJECT_LAYOUT.md    当前契约：目录、接口、决策、验证记录
LEARNING_LOG.md      学习记录与复盘
```
