# 第 19 轮计划（未开工，等「开」）

> 状态：**仅记录，不动代码**。学习者说「开」之后才按批次实施。
> 第 18 轮留空，未定义主题。

## 为什么这些放在同一轮

它们其实是同一个问题的不同侧面：**写入侧缺乏"检查与落地是同一件事"的保证**。

第 13 轮把这条规矩定在了引擎（租约 + epoch + 同一事务）；第 15 轮定在了账本（文件锁）；
第 14 轮定在了读取（单事务快照 + `last_seq` 对账）。剩下的两个缺口正好是同一族：

- 人工复核的 `resolve()`：读-改-写裸奔，并发双击能把日志写成 replay 失败的状态
- 阶段 10 审计与版本边界：日志无法自证"没被改过"，且未知事件类型的行为未定义

其余几项（心跳、重试、场景矩阵、真实模型）是独立小口子，按批次挂在同一轮里。

## 分批次（每批可独立交付、独立验收）

### 19A｜复核幂等与防重（P0，正确性）

**问题**：两个「确认成功」并发到达时，第二条 `REVIEW_RESOLVED` 会被写进日志，
此后 `replay` 抛 `InvalidEvent`，任务永远读不出来。已用
`scripts/_probe_double_resolve.py` 复现。

| 改动 | 内容 | 为什么在这一层 |
|---|---|---|
| 1. 写入前语义校验 | `append` 在事务内 dry-run：`replay(events)` → `apply(new)`，不合法不写 | 日志只认形状，语义矛盾留到读侧才炸；把矛盾挡在门外 |
| 2. 比较并追加 | `append(..., expect_seq=N)`：事务内核对 `last_seq == N`，否则 `Conflict` | 把"检查"和"写入"压进同一事务（第 13 轮的规矩） |
| 3. 幂等键 | `resolve(..., resolution_id=...)`：同 id 第二次 → 不写事件，返回第一次结果 + `duplicate=true` | 双击/网关重试应当成功，不该报错 |
| 4. 冲突语义 | 不同 id 但已非 `WAITING_REVIEW` → 返回「已由谁、何时、以什么结论处理」 | 把"你晚了"和"没有待复核"分开 |

**验收**：

- 探针改成回归测试：并发两次 `resolve` → 一条事件、第二次拿到 conflict 或 duplicate；
  日志 `replay` 始终成功
- 现有 310 个用例不动
- 决策追加：D56 写入前校验的位置、D57 幂等的键由谁给

**完成记录（2026-10-31）**

落地：

- `append(..., expect_seq=)` + `Conflict`，两个实现同规则；默认 `None` 即不检查，
  既有 97 处调用点一处没改
- `resolve_review()` 返回 `ResolveResult`（`recorded` / `duplicate` / `detail`）；
  `resolve()` 保留为薄封装，签名与返回类型不变，现有用例未动
- 幂等键写进 `REVIEW_RESOLVED` 的可选字段 `resolution_id`；`ReviewView` 另记
  `resolved_at`，用于回答「谁在什么时候处理的」
- CLI：`resolve --resolution-id`；重复提交退出 0，不同结论的晚到者退出 2
- 回归用例：`tests/test_journal_expect_seq.py`（16 条）、
  `tests/test_review_resolve_atomic.py`（11 条）

决策：

- **D56 → 写入前语义校验不放进 `append`。** 日志层只提供 `expect_seq`（比较并追加），
  语义判断仍留在 `model.apply` / `reconcile`。两条理由：`events.py` 已声明「Event 只
  承载事实，合法性由 apply 判断」；把 `replay` 搬进日志层会让 `journal` 反向依赖
  `model`，并且 integrity 用例将无法构造坏日志（现有用例里就有一条缺 `step_key` 的
  `STEP_DECIDED`）。
- **D57 → 幂等键由调用方给**（`resolution_id`）。只有调用方知道「同一次人工操作」的
  边界（请求 id、网关重试号）；引擎无法凭空生成一个跨进程稳定的键。

### 19B｜阶段 10：审计与版本边界（P0，可信度）

**问题**：能发现"少了一条"，但事件与 `last_seq` 一起改小仍然自洽；旧日志遇到将来
新增的事件类型会怎样，未定义。

| 改动 | 内容 | 备注 |
|---|---|---|
| 1. 事件 schema 版本 | 编码头加 `schema`；读侧按版本分派 | 老库默认 v1，不重写 |
| 2. 兼容策略 | 未知字段：忽略；未知 `type`：明确拒绝并给可读错误（不是 `KeyError`） | 定成契约，写进 `PROJECT_LAYOUT` |
| 3. 哈希链 | `events` 增 `prev_hash` / `hash`（sha256 覆盖 run_id+seq+body+prev） | 追加事务内计算 |
| 4. 迁移 | 老库补列，首条 `prev_hash=None`，读时按需校验 | 与第 13 轮补列同法 |
| 5. `verify` 命令 | `agentrun verify --db ...` 校验全链并报告首个断点 | CLI 第七个子命令 |

**验收**：篡改任意一条事件（含同步改 `last_seq`）→ `verify` 报出**第一个**断点；
未篡改 → 全链通过；老库迁移后行为不变。

**完成记录（2026-10-31）**

- `codec.SCHEMA_VERSION = 1`：记录顶层加 `schema`；缺失按 v1 读，老库不重写；高于
  当前版本一律 `DecodeError`——按旧布局硬读会把字段读错，比直接拒绝危险得多。
- `events` 表加 `prev_hash` / `hash` 两列，旧库走同一套 `PRAGMA table_info` +
  `ALTER TABLE` 补列。摘要 = sha256(`run_id` + `seq` + `body` + `prev_hash`)，在追加
  事务内计算，与序号分配同生共死。
- `SqliteEventLog.verify_chain()` 返回 `ChainReport`（总数 / 已校验 / 第一个断点 /
  原因）；三类断点：摘要不符、`prev_hash` 不符、链中间出现无摘要记录。
- 新增第七个子命令 `agentrun verify [--run-id]`，默认校验全部任务，退出码 0 / 1 / 2。
- **`read` 不做链校验**：它是审计动作，塞进读路径会让「读得慢」直接变成「恢复得慢」。
- 迁移前的记录 `hash` 为 NULL，无法自证：校验从第一条带摘要的记录开始，CLI 分别输出
  「全段无摘要」与「前段无法自证」两种措辞，既不算通过也不算篡改。
- 用例：`tests/test_journal_chain.py`（12 条）+ `tests/test_codec.py` 补 schema 版本
  矩阵。关键一条是「删中间一条 + 把后面那条重写成合法 `seq` + 改小 `last_seq`」——
  `read` 会认为一切正常，只有链能抓到。

### 19C｜心跳（P1）

现在只有「写前续租」，长工具调用期间不续租。改动：`Engine` 起一个后台心跳线程，
按 `ttl/3` 续租；失败即置 lost（后续写入由栅栏拒绝）。

**验收**：工具 sleep 超过 `ttl` 时，另一个 owner 仍抢不到；心跳停后能在 TTL 内被接管。

**完成记录（2026-10-31）**

- `_Heartbeat` 按 `ttl / 3` 续租（不是 `ttl / 2`——留两次机会），daemon 线程；
  `stop()` 最多等 `ttl + 1` 秒。续租失败**不重试**，只把原因交给 `Engine._mark_lost`。
- 心跳只覆盖 `run_tool` 那一段，不覆盖主循环：写事件本身很快，`_append` 每次写前
  已经续租，再叠一层只是多一个线程去抢同一把锁。
- 失败后不打断正在跑的工具：调用已经发出去了，半途掐断只会留下一个说不清有没有
  生效的副作用。等它返回，下一次写入抛 `LeaseLost`，本次结局是 `LOST`，任务仍是
  `RUNNING`——接手者看到悬挂的 `STEP_STARTED`，由对账或复核收敛。
- 用例：`tests/test_engine_heartbeat.py`（3 条）。新增 `tests/conftest.py` 的
  `no_heartbeat` fixture，把「关掉心跳」变成显式动作：两条既有接管用例改用它，
  因为它们验证的是「心跳死了以后怎么办」，而不是默认行为。
- **刻意不做：工具调用超时。** 心跳只保证「正常的长调用不被误判」，不防「工具卡住」：
  工具陷在死循环或永不返回的等待里时，心跳照常续，租约不过期，别人接不了手——只能
  由人杀掉进程、由 TTL 兜底。评估过两个替代：给工具加超时（超时记 `UNCERTAIN`，
  语义正确，但要引入 daemon 线程与「没等到结果」的分类，且 Python 杀不掉线程）；
  给心跳设绝对上限（本质只是把租约时间调长，不解决问题）。两条都记进债务表。

### 19D｜重试策略（P1，**需要先答一个架构问题**）

现状：引擎与客户端都不重试（第 12 轮刻意决策）。任务书 §4 把「重试」列在故障语义里，
目前只体现为"重试 = 新 attempt / 新 step_key"。

待定问题（学习者先答）：重试放在客户端、`ModelPolicy`、引擎，还是更外层？
对哪些失败可以重试（`ToolRejected` / `LedgerBusy` / 429 / 超时），各自凭什么判断
"确定没生效"？

**验收**：只在"确定未生效"的失败上重试；重试次数与退避有上限；重试不产生第二条事件。

### 19E｜阶段 11 文档面（P2）

- 场景矩阵：8 个评测场景 × 期望结局 × 证据文件
- 设计说明：一页纸讲清「为什么是事件日志、为什么栅栏、为什么不 ReAct」

**验收**：`README` 能顺着读到这两份文档，不需要读源码。

**完成记录（2026-11-01）**

- `docs/design.md`：三个选择与各自的代价，每个都从「最直觉的做法哪里不够」讲起——
  存状态会在「工具已执行、状态还没写」的窗口撒谎；检查租约会在检查与写入之间失效；
  ReAct 的中间态不落盘，`UNCERTAIN` 就无从判断。末尾单列一节「这些选择共同的代价」。
- `docs/scenarios.md`：8 行矩阵（场景 / 注入的故障 / 期望结局 / 期望余额 / 证明了什么），
  外加一张「场景 → 离线契约测试」的对照表，说明矩阵给谁看、CI 给谁看，两者不能互相替代。
- README 顶部给出两条入口：想知道「为什么这样设计」和「它到底跑通了什么」，不必读源码。
- 两份文档都指向 `PROJECT_LAYOUT.md`（怎么做）与 `LEARNING_LOG.md`（怎么走到这儿的），
  同一件事不在三个地方各写一遍。

### 19F｜真实模型联调（**阻塞：凭据**）

需求方决定：**保留真实联调，不做降级**（2026-10-30）。

需要 `AGENTRUN_BASE_URL` / `AGENTRUN_API_KEY` / `AGENTRUN_MODEL` 三个环境变量。
拿到后可补：真实成功样例、解析失败样例、429 样例，并改掉
`model_client.py` 里那句「未联真实服务验证」。

备选（无凭据时）：本地 Ollama / llama.cpp 的 OpenAI 兼容端点，只能验证"真实输出
守不守 JSON"，证明不了鉴权、配额与计费。

无论走哪条，都要同时补齐不需要凭据的那部分：请求体精确断言、错误映射矩阵
（401 / 429 / 5xx / 超时 / 非 JSON / 缺字段）、`client_from_env` 缺变量报错。

### 19G｜Docker（建议不做）

任务书允许不引入容器化。目前触发信号（CLI 稳定 + 跨机需求）都未成立，只保留债务条目。

### 19H｜模型适配层收口（P1，学习者在 2026-10-30 加入 LangChain 之后必做）

背景：`model_client.py` 已改成 `langchain_openai.ChatOpenAI` 实现，`pyproject.toml`
新增 `langchain-openai>=1.0,<2` 与 `pydantic>=2.7.4,<3`。这推翻了「运行时零第三方
依赖」的既有表述，四处需要对齐。

| 改动 | 内容 | 判据 |
|---|---|---|
| 1. 文档口径 | `PROJECT_LAYOUT` / `README` 统一改成"核心 + 模型适配依赖"；删掉"零第三方"的旧话。**不再维护独立的依赖文档**（需求方 2026-10-30 删除 `DEPENDENCIES.md`），需要时用 `scripts/_dep_graph.py` 现场输出 | 两份文档口径一致，仓库里没有指向已删文件的链接 |
| 2. 类名漂移 | `HttpModelClient` 已不是 HTTP 客户端。二选一：改名（`LangChainModelClient`）或在文档里注明"名字保留兼容" | 类名与职责一致，或"名不副实"有明确解释 |
| 3. 依赖边界 | 决策：主依赖 vs extras（`[project.optional-dependencies].model`）+ 惰性 import | 决策编号入库；若选 extras，未装模型包时 `agentrun --help` 仍可用 |
| 4. 契约测试 | `test_model_client.py` 覆盖：请求体字段（`model`/`messages`/`temperature`）、401 / 429 / 5xx / 超时 / 非 JSON / 缺字段的错误映射、`SecretStr` 不进日志、`client_from_env` 缺变量报错 | 每个分支至少一条断言；错误信息里检索不到 key 与 prompt |
| 5. 重试归属 | 记入 19D 的前置问题（客户端不重试是原则，还是 LangChain 默认行为已经带重试？） | 明确 LangChain 是否自带重试并把默认关掉或写进契约 |

**完成记录（2026-10-31）**

决策：

- **D58 → 主依赖，不做 extras、不做惰性 import。** 产品形态是「一条命令能跑的东西」
  （19I 的 `ask`），模型适配是主路径而不是可选插件。extras 加惰性 import 会引入第二个
  安装矩阵，以及「装没装模型包」这个隐性状态；收益只是省几十 MB 磁盘。`pyproject.toml`
  与 `PROJECT_LAYOUT.md` 第 13 行已按主依赖描述，口径一致。
- **D59 → 保留 `HttpModelClient` 名字。** 公共构造参数（`base_url` / `api_key` /
  `model` / `timeout` / `reasoning_effort`）与 `complete(prompt) -> str` 接口都没变，
  改名会打断 CLI、脚本与文档的引用，收益只有「名字更准」。`PROJECT_LAYOUT.md` 第 86 行
  已注明这一点，属于判据允许的「名不副实有明确解释」那一支。
- **重试归属**：`ChatOpenAI` 显式配置 `max_retries=0`，契约测试断言了这一项。客户端
  不重试，重试是引擎层的决定（19D2 的 `DECISION_FAILED`）。

改动：

- **收掉门禁里长期红的 6 条**：3 条是本机 `agentrun.local.toml` 没隔离（`client_from_env`
  与 `cli run` 读到了真实配置），3 条是按旧实现写的断言（不传 `http_client`、关
  `root_client`）。现有 **408 条全绿**。
- **新增错误映射矩阵**：401 / 429 / 5xx / 超时 / 非 JSON / 缺 `content` / `content`
  类型错，逐条断言 `retryable`。
- **修掉一个真实缺陷**：LangChain 把底层网络异常包了一层，只对最外层做 `isinstance`
  时超时被判成不可重试——这类误判不报警，只让本该自愈的抖动变成任务失败。改成沿
  `__cause__` / `__context__` 找证据（`_causes`），超时与连接失败恢复为可重试。
- `scripts/_dep_graph.py --markdown` 改为输出到终端，不再重建 `DEPENDENCIES.md`。

### 19I｜产品面入口（P1，把"引擎"变成"能用的东西"）

问题：`agentrun run --policy model --task ...` 已能让 LLM 驱动工具，但缺"人的入口"，
外人打开仓库只看到 `scripts/` 一堆实验。

| 改动 | 内容 |
|---|---|
| 1. 脚本分层 | `scripts/eval_suite.py` 留在根；其余进 `scripts/demo/` 与 `scripts/evidence/`（`_probe_*`、`crash_*`、`lease_worker`、`ledger_hammer`） |
| 2. 账本命令 | `agentrun ledger init --A=1000 --B=0` / `agentrun ledger balance [--account A]` |
| 3. 一句话入口 | `agentrun ask "给 B 转 250"`：自动生成 `run_id`、装配模型策略、人类可读输出与退出码 |
| 4. README 剧本 | "5 分钟：init → ask → status → balance"，含真实输出片段 |

**验收**：一条命令链能从空目录跑到"账本余额变化"，全程不需要看 `scripts/`。

**完成记录（2026-11-01）**

- `agentrun ask "从账户 A 转 250 到账户 B"`：自动生成 `run_id`
  （`ask-YYYYMMDD-HHMMSS`，要能复制去跑 `status` / `resume`），装配模型策略，打印
  结论、步数、余额与停止原因；没跑成时直接写出下一步该敲的命令。
- `agentrun ledger init --A=1000 --B=0` / `ledger balance [--account A]`：账本不再需要
  去翻 `scripts/`。账本已存在时 `initial` 被忽略（`Ledger` 的构造语义），不加 `--force`
  覆盖——重置该是显式、带备份的动作。
- `Ledger.snapshot()`：只读、取锁、返回副本，供 `balance` 打印全部账户。
- 脚本分层：`scripts/` 根只留 `eval_suite.py` 与 `_dep_graph.py`；教学脚本进 `demo/`，
  探针与测试夹具进 `evidence/`。四条测试里写死的脚本路径同步更新；`eval_suite.py` 用
  `sys.path` 把 `demo/` 加进搜索路径——入口不该要求调用方先设 `PYTHONPATH`。
- README 顶部加「5 分钟：从空目录到余额变化」，含一次真实运行的输出片段。
- 端到端实测：空目录 → `ledger init` → `ask` → 余额 `A: 750 / B: 250`，4 步，
  `SUCCEEDED`。
- 用例：`tests/test_cli_ask.py`（7 条）。

一个踩到的坑：`ask "给 B 转 250"` 会被模型理解成「从 B 转出」，因为它不知道谁是付款
方。README 的示例因此写成「从账户 A 转 250 到账户 B」——提示里没有账户语义，措辞必须
自带主客体。

### 19J｜新事件字段进审计（并入 19B）

LangChain 改造与后续新增事件可能带入新 payload 字段。19B 的 schema 版本与哈希链要覆盖
"新增字段如何兼容"，不能只按旧字段设计。

## 顺序与依赖

```
19A（正确性，先做） → 19B（审计，含 19J 的新字段兼容）
19H（模型适配收口，越早越好：它决定 19D 与文档口径）
19C（心跳，独立）    19D（重试，需先回答放哪层）
19E（文档，可并行）   19I（产品入口，依赖 19H 的文档口径）
19F 等凭据；19G 不做
```

## 不做什么

- 不改 `transfer` 的非幂等契约（那是"不确定步骤"整个练习的前提）
- 不为复核引入租约（19A 的 `expect_seq` 已经够）
- 不再新增第三方依赖：`langchain-openai` 与 `pydantic` 是既有决定，审计与锁仍用标准库
- 不删证据脚本：只按 19I 分层目录，不改变它们证明的东西

## 待决策清单（开工时逐条确认）

| 编号 | 待定 | 影响 |
|---|---|---|
| D56 | 写入前语义校验放在 `append` 还是 `reconcile` | 19A |
| D57 | 幂等键由调用方给还是引擎生成 | 19A |
| D58 | langchain 是主依赖还是 extras（配惰性 import） | 19H、19I |
| D59 | `HttpModelClient` 改名还是保留 | 19H |
| D60 | 重试放哪层、对哪些失败重试 | 19D |

## 当前仓库状态（截至计划写下时）

```
 M .gitignore             M pyproject.toml          M src/agentrun/cli.py
 M PROJECT_LAYOUT.md      M scripts/_dep_graph.py   M src/agentrun/model_client.py
 M README.md              M tests/test_cli.py       M tests/test_model_client.py
?? ROUND19_PLAN.md        ?? scripts/_probe_double_resolve.py
--- HEAD: bc4fae1，main 与 origin/main 一致（未推送新改动）
--- 本地检查：pytest 341 passed / mypy 15 files 0 error / ruff 全绿
```
