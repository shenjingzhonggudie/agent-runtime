# LEARNING_LOG

> 记录真实过程：真实错误、真实假设、Agent 的真实误判。不编造"有教育意义"的故事。
> 状态含义：实现中 / 待验证 / 待复盘 / 完成。只有学习者完成复盘后才能标"完成"。

## 第 0 轮：定位与骨架

- **本轮目标与架构位置**：建立可安装包、两份文档与两条自动检查（lint + 格式），让"装得上、导得进、检查得动"成为可重复的事实。不涉及任何业务语义。

- **已确认的契约 / 决策，以及学习者的理由**：

  | # | 决策 | 结论 | 学习者的理由 |
  |---|---|---|---|
  | D1 | 项目目录 | `C:\Users\王佳浩\Desktop\项目2` | 学习者指定 |
  | D2 | 包名 | `agentrun` | 与参考项目同名，学习者不看参考项目，无混淆风险 |
  | D3 | 场景 | 模拟账本 alice→bob 250，阈值 500；Agent 补工具级不变量"转移后 alice ≥ 300" | 学习者选择默认参数 + 授权 Agent 补一条边界条件 |
  | D4 | 运行时 | Python 3.14.6 + venv + pip，业务仅标准库，开发依赖 pytest + ruff | 学习者确认 |
  | D5 | 版本控制 | 本地 git，骨架提交一次，不推送；最终再推 GitHub | 学习者要求最终再推 |

  D6（模拟账本与引擎日志分开存储）：学习者在第 0 轮未单独表态，按任务书默认保留，第 1 轮事件契约确认时正式落定。

- **学习者的手工推演**：本轮无。第 0 轮不产生状态转移，不强造推演。

- **遇到的问题与最小复现**：
  1. `pip install -e ".[dev]"` 失败：`SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING')`，源为 `https://pypi.tuna.tsinghua.edu.cn/simple`。
  2. 同命令显式 `--index-url https://pypi.org/simple` 成功。

- **最初假设 / Agent 是否误判**：
  - 假设 A："中文路径可能导致 venv / editable install 失败"。**被推翻**：`.venv` 创建、`pip install -e .`、pytest、ruff 全部正常。误判已记录，不事后改写为"早就知道"。
  - 假设 B："安装失败可能是 `pyproject.toml` 的构建后端或 `requires-python` 写错"。**被推翻**：升级 setuptools 后仍失败，失败点在网络层；错误信息里 `Could not find a version that satisfies the requirement setuptools>=75 (from versions: none)` 是镜像不可达的次生表现，不是版本不存在。

- **验证命令、结果与证据**：
  - `python -m venv .venv` → OK，`Python 3.14.6`
  - `pip install --index-url https://pypi.org/simple -e ".[dev]"` → `Successfully installed agentrun-0.1.0.dev0 ... pytest-9.1.1 ruff-0.17.1`
  - `pytest` → `2 passed in 0.04s`
  - `ruff check .` → `All checks passed!`
  - `ruff format --check .` → `3 files already formatted`
  - CI：`.github/workflows/ci.yml` 已写，**未推送、未运行**

- **根因与可迁移规律**：
  - 根因：本机 pip 全局配置指向的镜像 TLS 不可用；非代码缺陷。
  - 规律一：安装失败、lint 违规、测试断言失败必须分开报告。`exitCode` 为 0 但输出含 `error:` 的情况真实存在（本次 `pip` 子进程失败而外层命令仍返回 0），读结果要看内容而非只看退出码。
  - 规律二：环境问题优先用最小命令二分（换源、单独装一个包），不要在业务代码里找原因。
  - 规律三：中文路径在本机 Python 3.14 + pip 26 下不构成障碍；这个结论有实测支撑，但只覆盖本机与当前工具版本。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. **边界**：模拟账本为什么必须与引擎事件日志分开存储？如果图省事把"扣款 + 写事件"放进同一个 SQLite 事务，会抹掉哪一个具体故障窗口，从而使哪一类恢复练习失去意义？
  2. **演化**：从 v1（单 worker）到 v2（多 worker 接管），哪些接口必须变、哪些必须保持不变？请至少指出一个"看起来该变、其实不该变"的地方。

- **学习者原回答摘要**：Q1 选 C（已生效则补记结果、绝不重做），Q2 选 B（引擎因看不到历史而再次下发 transfer）。两题均正确。

- **纠偏与仍未理解的部分**：能判断“不能重做”，但这是选择题；尚未独立推导事件顺序与恢复动作。1B 用事件契约与顺序题验证是否真的理解“意图先于执行”，以及 `lookup` 的 None 只表示“此刻查不到”。

- **补充记录**：学习者对开放式概念题的连续三次回答为“不知道”，Agent 改为先给可运行实验（`scripts/crash_window_demo.py`）再给选择题，回答正确。说明问题出在提问方式，不在学习者；后续每轮先用可观察现象铺垫，再问。

- **债务**：
  1. CI 仅本地配置，远端未验证。触发信号：建立 GitHub 远端仓库。
  2. mypy 未引入。触发信号：第 1 轮事件/视图/公共接口成型。
  3. Docker 未引入。触发信号：CLI 启动方式稳定且出现跨机演示或容器交付需求。
  4. 场景存储形式（JSONL vs SQLite）与字段名未定。触发信号：第 2 轮工具契约。
  5. D6（存储分离）尚未由学习者显式确认。触发信号：第 1 轮事件契约确认。

- **本轮状态**：完成（架构问题已答并纠偏；理解层面仅经选择题验证，见第 1 轮记录）

## 第 1 轮：外部世界与事件契约

- **本轮目标与架构位置**：先把“外部世界”造成真会扣钱、会重复扣钱的模拟账本，再定义事件与视图。
  位置：`engine` 尚不存在，`model` 是唯一的纯逻辑层，`demo` 是唯一有副作用的地方，两者不共享存储。

- **已确认的契约 / 决策，以及学习者的理由**：
  - D7 结果三态、D8 `step_key` 跨重试稳定、D9 预算按决策计、D10 信封与 `seq` 语义、D11 存储损坏抛
    `LedgerCorrupted`。学习者回复“同意”，未逐条给理由；D11 由学习者在“真检查 / 明确 cast”二选一中
    指定“A 你来完成”，即选择运行时校验。
  - 补充契约（Agent 起草、学习者确认）：
    1. `StepView.reason`：学习者追问“STEP_UNCERTAIN 的 reason 在哪”时发现缺失，补字段并加断言。
    2. `RunView.last_seq`：学习者用 `steps[-1].step + 1` 当 seq 参照，暴露视图缺少事件序号，补字段。

- **学习者的手工推演**：本轮未做完整纸上推演。改为运行 `scripts/crash_window_demo.py` 观察三组真实数字：
  v0 重复扣款（1000→500）、v1 有意图无结果（保持 750，`lookup` 命中）、同一事务无中间态。
  随后两道选择题：Q1 选 C（已生效则补记、不重做），Q2 选 B（引擎因无历史再次下发），均正确。

- **遇到的问题与最小复现**：
  1. 首次实现把**每种事件都追加新 StepView**。复现：`decided` 后 `len(steps)==1`，再来 `started` 变成
     `steps==2`、`step==2`，后续全部错位。纠正：只有 `STEP_DECIDED` 增加步骤。
  2. 用 `view.steps[-1].step + 1` 校验 `seq`。根因是视图没有 `last_seq`，属契约缺口，不是学习者疏忽。
  3. `event.payload["tool"]` 缺字段会抛 `KeyError`，测试要求 `InvalidEvent`。学习者改用 `_field` 统一收口，
     并自行补了“`bool` 不算 `int`”的判断。
  4. mypy 首次运行即在 `demo.py` 抓到 2 处 `Any` 返回。学习者的第一版修法是
     `isinstance(self._path, Path)`（恒真，死代码）+ 局部变量注解（运行时不校验）——mypy 绿了但没有任何保护。
     纠正后采用运行时校验 + `LedgerCorrupted`。
  5. 编辑器旧缓冲两次覆盖新字段（`last_seq`、`reason`）。处理：关闭重开文件，并用运行时 `__slots__` 验证，
     不只依赖读文件。

- **最初假设 / Agent 是否误判**：
  - 误判一：以为 `StepView` 字段已够用，实际漏了 `reason` 与 `last_seq`，两处都由学习者先发现。
  - 误判二：以为“先讲概念再提问”可行，连续三轮得到“不知道”。改为“可观察实验 → 选择题”后回答正确。
  - 未误判：`demo` 的原子替换与“拒绝零副作用”设计，在后续测试中一直成立。

- **验证命令、结果与证据**：
  - `pytest` → `43 passed in 1.58s`
  - `mypy` → `Success: no issues found in 4 source files`
  - `ruff check .` → `All checks passed!`；`ruff format --check .` → `11 files already formatted`
  - `python scripts/crash_window_demo.py` → 三场景输出（见上）

- **根因与可迁移规律**：
  1. 视图必须携带“判定下一条事件是否合法”所需的全部信息；缺一个 `last_seq`，调用方就会拿别的编号凑。
  2. 类型注解不执行运行时校验；恒真的 `isinstance` 比不写更糟，因为它制造已检查的错觉。
  3. “确定未生效”与“读不出可信状态”必须用不同异常表达，否则恢复逻辑会把损坏当成没发生。

- **架构问题**：删掉 `STEP_STARTED`、把尝试次数直接记在 `STEP_COMPLETED` 上，v1 恢复会失去什么？

- **学习者原回答摘要**：选 B——恢复时只能看到“完成了”，看不到“开始过”，“执行到一半崩溃”与“还没执行”分不开。

- **纠偏与仍未理解的部分**：选择正确。补充：`STEP_STARTED` 的价值不是计数，而是把“已决定”和“已交给外部世界”
  分成两个可观察时刻；没有它就没有 `UNCERTAIN` 的来源。仍未验证：能否自己从空白写出折叠逻辑并解释每个分支。

- **债务**：
  1. `StepView.args` 与事件 payload 共享 dict → 第 7 轮监听器隔离。
  2. `RUN_FAILED` 的 reason 未进视图 → 第 6 轮 CLI。
  3. `replay` 信任输入顺序 → 第 5 轮 Journal。
  4. `request_id` 跨 transfer/receipt 未隔离 → 第 2 轮工具契约。
  5. 账本状态文件无版本号 → 第一次改 `Record` schema。
  6. `_load → 改 → _save` 无锁 → v2 并发。
  7. CI 未远端运行 → 建立远端仓库。
  8. Docker 未引入 → CLI 稳定且出现跨机需求。

- **本轮状态**：完成（实现、验证、复盘均完成）；理解深度仅由选择题与代码复核支撑，未做独立推导，列为第 2 轮观察点。

## 第 2 轮：工具执行

- **本轮目标与架构位置**：定义 `Tool` 协议、副作用分类、注册表和单次执行边界；`engine` 之后调用
  `run_tool` 获取结果，再写事件。`tools` 不依赖 `model / events / journal`，也不认识任何业务字段。

- **已确认的契约 / 决策，以及学习者的理由**：
  - D12 副作用三值枚举、D13 校验放在工具自身、D14 三种异常 + 未知异常归 UNCERTAIN、D15 tools 自带结果类型。
    学习者一次性回复“同意”，未逐条给理由；但随后正确回答了第 1 轮的 `STEP_STARTED` 必要性选择题（B）。
  - 分类规则的关键约束（Agent 起草）：`validate` 抛任何异常都归 FAILED（因为工具一定没执行过）；
    `run` 抛除 `ToolRejected` 外的任何异常都归 UNCERTAIN（因为无法证明外部世界没变）。

- **学习者的手工推演**：本轮未做；实现前只按“先判 args → 校验 → 执行 → 分类”的顺序推进。手推安排在本轮复盘。

- **遇到的问题与最小复现**：
  1. 首版用字符串写状态：`ToolOutcome(status="FAILED", ...)`。后果：**7 个测试失败 + 5 个 mypy 错误**。
     根因是 `ToolStatus` 为 `StrEnum`，`"FAILED" == ToolStatus.FAILED` 为真但 `is` 为假，测试用 `is` 断言。
  2. 长度 121 字符的行在 `ruff format .` 之后仍然超标。原因不是格式化失败，而是**编辑器里的旧缓冲在
     format 之后又保存了一次**，把已拆行的结果覆盖回单行。复现：format 后读磁盘是分行版本，几秒后
     再读又变回单行 121 字符。

- **最初假设 / Agent 是否误判**：Agent 先读了缓存中的文件内容，误以为 `ToolStatus` 已生效并判断“逻辑结构正确、
  只差枚举”；随后实测磁盘发现仍是字符串。修正依据是 `ruff check` 与 `mypy` 的报错位置，不是记忆。

- **验证命令、结果与证据**：
  - `pytest` → `57 passed in 1.93s`
  - `mypy` → `Success: no issues found in 5 source files`
  - `ruff check .` → `All checks passed!`；`ruff format --check .` → `13 files already formatted`

- **根因与可迁移规律**：
  1. `StrEnum` 的值相等不等于同一对象；测试用 `is` 是为了防止字符串冒充枚举，`==` 会放过这个错误。
  2. 分类依据是“**工具有没有被调用过**”，不是“异常类型看起来多严重”。校验阶段的任何异常都不可能伴随副作用。
  3. 格式化工具与编辑器缓冲会互相覆盖：`ruff format` 只作用于磁盘，编辑器未保存的旧内容一旦保存就会回滚它。
     正确顺序是“先保存全部编辑 → 再格式化 → 不再改”。

- **架构问题**：
  1. 第 1 步查余额成功、第 2 步转账被账本拒绝、第 3 步策略决定结束。此时第 2 步的 `Outcome` 是什么？
     任务状态又是什么？（选项见对话）
  2. 如果把 `transfer` 这类非幂等工具的错误一律当 FAILED，第 6 轮恢复时会发生什么？

- **学习者原回答摘要**：Q1 选 C（错误，正确为 A），Q2 选 B（正确）。

- **纠偏与仍未理解的部分**：Q1 的错在把“单步失败”等同于“任务失败”。已用 8 条事件的完整序列纠正：
  第 2 步 `STEP_FAILED`（余额不足）之后仍可 `RUN_SUCCEEDED`（结论是“未达阈值，不转账”）。
  `RunStatus` 只由 `RUN_SUCCEEDED / RUN_FAILED / RUN_EXHAUSTED` 改变，单步结果从不改任务状态。
  Q2 正确：结果类别与重试资格是两个维度，只读工具可以“结果未知但允许重试”。

- **债务**：
  1. `request_id` 跨 transfer / receipt 未隔离 → 第 3 轮写账本工具时定。
  2. `validate` 崩溃与参数不合法共用 `kind=invalid_args` → 第 7 轮时间线拆分。
  3. `run` 返回非 Mapping 未校验 → 第 4 轮结果落盘前。
  4. 前一轮遗留：`args` 共享引用、`RUN_FAILED` 原因未进视图、`replay` 信任顺序、账本无版本号、读改写无锁、CI 未远端运行、Docker 未触发。

- **本轮状态**：完成（实现、验证、复盘均完成；Q1 判断错误已纠正）。

## 协作方式变更（2026-10-24，第 3 轮 3B 起点）

- 学习者要求：**Agent 只写测试与注释（docstring），其余代码全部由学习者写**，包括工具协议修改、
  `ledger_tools.py`、演示脚本、配置变更。
- 影响：
  1. 不再由 Agent 提供可直接运行的骨架；改为在消息中给出精确签名规格，由学习者落笔。
  2. 测试仍然先写：学习者先建“只有签名 + NotImplementedError”的模块，Agent 补测试，形成有效红。
  3. 架构文档与学习日志仍由 Agent 维护；代码内的 docstring 由 Agent 补，且不得改写实现。
- 理由（学习者）：希望在实现层获得完整的手感，而不是在既有骨架上填空。

## 第 3 轮：引擎循环与账本工具

- **本轮目标与架构位置**：把策略、工具、日志串成可运行的循环（3A），再把模拟账本包装成四个满足
  `Tool` 协议的应用工具，并用一个演示脚本验证“重启后靠对账避免重复扣款”（3B）。
  位置：`engine` 位于最外层，向下调用 `policy` 取动作、`tools` 执行、`journal` 记事实、`model` 折叠视图；
  `ledger_tools` 是 `demo` 与 `tools` 之间唯一的适配层。

- **已确认的契约 / 决策，以及学习者的理由**：
  - 3A：`Engine.run` 的七步顺序（预算判断先于问策略、`STEP_DECIDED` 先于 `STEP_STARTED`、
    UNCERTAIN 立即停止且不追加事件）。
  - 3B：D16 工具签名注入 `ToolContext`、D17 账本异常一律映射为 `ToolRejected`、
    D18 对账策略留在演示脚本而不进 `policy.py`。学习者对 D16 的采纳理由是“工具拿不到 `step_key`
    就只能从 `args` 里编一个键，那就等于没有幂等”。

- **学习者的手工推演**：3B 阶段未做纸上推演。改为先写工具骨架，再由 Agent 补测试形成有效红，
  之后逐个工具改到绿。

- **遇到的问题与最小复现**：
  1. `LookupRequest.run` 调用了 `Ledger` 上不存在的 `lookup_request`。复现：`mypy` →
     `"Ledger" has no attribute "lookup_request"`。根因是账本方法名是 `lookup`，写的时候按工具名
     反推了方法名。
  2. `FakeTool` 的方法仍是 `validate(self, args)` / `run(self, args)`，没有跟上 D16 的 `ctx`。
     后果：`2 failed, 103 passed`。补上 `ctx: ToolContext` 后 105 全绿。
  3. `ruff check` 报 `engine.py` 导入未排序、`ruff format` 报 `ledger_tools.py` 多一空行，
     拖到最后才修。这两处都不影响行为，但让“全绿”这个信号迟到了两轮。
  4. **演示脚本第一版是不可运行的骨架**：`root.name + "/ledger"` 传了 `str` 给要求 `Path` 的
     `Ledger`（`AttributeError: 'str' object has no attribute 'mkdir'`）、`GetBalance` 与 `Transfer`
     各注册两次（`DuplicateTool`）、每个工具各建一个 `Ledger` 实例、`save_receipt` 没有对应工具
     （会走成 `UnknownTool`）、没有任何 `print`、模块顶层直接执行、文件放在 `src/agentrun/` 会被
     打进发行包、相对与绝对导入混用。

- **最初假设 / Agent 是否误判**：
  - 误判：给出“精确到调用链的规格”后，假设学习者会先跑一次再交付。实际交付物连第一行都过不去。
    责任不全在学习者——规格里没有写“交付前必须先执行一次”，而这一条恰恰是唯一能拦住上述
    8 个问题的动作。已在后续轮次改为“交付物必须附一次真实执行的输出”。
  - 未误判：`ToolContext` 注入后 `run_tool` 的分类逻辑无需改动，说明第 2 轮把“分类依据是
    有没有执行过”收口在 `run_tool` 内部是对的。

- **验证命令、结果与证据**：
  - `pytest` → `105 passed`
  - `mypy` → `Success: no issues found in 9 source files`
  - `ruff check .` → `All checks passed!`；`ruff format --check .` → `22 files already formatted`
  - `python scripts/ledger_demo.py` → 退出码 0。第一次运行 11 条事件，
    `alice 1000→750`、`bob 0→250`；第二次运行换新日志、复用同一账本目录，
    仅 5 条事件（`lookup_request` → `found=True` → `RUN_SUCCEEDED`），余额保持 `750/250`。

- **根因与可迁移规律**：
  1. 幂等键必须来自引擎（`step_key`），不能来自策略。策略给的 `args` 是业务参数，
     一旦允许它携带调用标识，调用方就能绕过步骤级去重。
  2. “确定未生效”与“读不出可信状态”在适配层必须收敛成同一个对外语义，否则引擎会为了区分它们
     而被迫认识账本内部结构。本轮的取舍是都映射为 `ToolRejected`，依据是账本读盘先于写盘。
  3. 演示脚本的价值在于**能跑**。它不产生契约，但它是唯一能证明“这四个工具真的能装进引擎”的证据；
     一个跑不起来的演示，其信息量低于没有演示。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. 第二次运行中，`ReconcilePolicy` 用 `make_step_key(run_id, 2)` 去查上一次的转账。如果它改用
     第一次运行 `STEP_COMPLETED` 里返回的 `result["request_id"]`，在什么情况下会查不出结果？
  2. `Found=False` 时策略直接重做转账。账本 `lookup` 的文档说 `None` 只表示“此刻查不到”，
     那么本演示在哪种真实故障下会重复扣款？

- **学习者原回答摘要**：3B 收尾阶段未答题（排期见下一轮）。

- **纠偏与仍未理解的部分**：本轮暴露的是**交付纪律**问题，不是理解问题：规格里的每个约束
  （传 `Path`、注册不能重名、幂等键取 `ctx.step_key`）都能在写的时候被 2 秒的 `mypy` 或一次执行
  发现，但直到 Agent 跑了一次才被暴露。下一轮起把“交付前自己跑一遍”列为硬性前置。

- **债务**：
  1. `lookup` 不区分记录类型 → 第 6 轮对账工具。
  2. 对账逻辑只存在于演示脚本，引擎自己不会在 UNCERTAIN 后主动对账 → 第 6 轮。
  3. `validate` 崩溃与参数不合法共用 `kind=invalid_args` → 第 7 轮时间线。
  4. `run` 返回非 Mapping 未校验 → 第 4 轮结果落盘前。
  5. 前几轮遗留：`args` 共享引用、`RUN_FAILED` 原因未进视图、`replay` 信任顺序、账本无版本号、
     读改写无锁、CI 未远端运行、Docker 未触发。

- **本轮状态**：3A、3B 实现与验证完成；**复盘待进行**（两道架构问题未答）。

## 第 4 轮：事件编码

- **本轮目标与架构位置**：给事件加一层无损失的 JSON 编解码，作为第 5 轮落盘的前置条件。
  位置：`codec` 只夹在日志层与 `events` 之间——写入前 `encode_event`，读出后 `decode_event`；
  它不判断事件顺序（`model.apply` 负责），不分配 `seq`（日志层负责）。

- **已确认的契约 / 决策，以及学习者的理由**：
  - D19 编码载体是 `str`、D20 非 JSON 类型一律报错、D21 独立 `codec.py`。
    学习者一次性回复“同意”。

- **学习者的手工推演**：本轮未做。第 1 步实现后由 Agent 复核，发现方向性错误（见下）。

- **遇到的问题与最小复现**：
  1. 学习者写的第一版 `encode_event` 在函数末尾仍是 `raise NotImplementedError`，
     并且把「`run_id` / `seq` / `payload` 不能为空」这类**业务校验**写进了编码函数，
     越过了 `model.apply` 的职责边界。6 个目标用例全部未过。
  2. 同一版本里 `encode_event` 抛的是 `DecodeError`：编码阶段失败抛了解码异常，
     `pytest.raises(EncodeError)` 抓不到，因为二者是兄弟类。
  3. 嵌套只检查了一层：`{"trace": [1, (2, 3)]}` 中 `v` 是 `list`，类型检查通过，
     但内部藏着 `tuple`。
  4. `isinstance(v, (tuple, set, bytes,Decimal,datetime))` 缺少空格，`ruff format` 会改。

- **最初假设 / Agent 是否误判**：
  - 误判：以为“给出四步路线 + 测试名”就足以让学习者独立完成。实际反馈是
    “234 我不知道你说的什么意思”——路线里用「拒绝规则」「回归」这类术语指代具体行为，
    没有给出“输入 → 期望异常”的实例。改为贴出三段具体输入与后果后，问题才变得可判断。
  - 未误判：`bool` 是 `int` 子类这条，在学习者第一版里没有漏（他还没写到解码的 `seq`）。

- **验证命令、结果与证据**：
  - `pytest tests/test_codec.py` → 39 passed
  - `pytest` → 146 passed
  - `mypy` → `Success: no issues found in 10 source files`
  - `ruff check .` → `All checks passed!`；`ruff format --check .` → `24 files already formatted`

- **根因与可迁移规律**：
  1. `json` 的默认行为是「能转就转」，不是「能保真就转」。`tuple`→`list`、非 `str` 键→`str`、
     `nan` 原样输出，三者都不报错但都改变数据。凡是「往返必须相等」的场合，编码器必须
     自己递归检查，不能把判断权交给序列化库。
  2. `nan != nan` 让「解码后与编码前相等」这条不变量直接失效，所以它不是精度问题，
     是契约问题。
  3. 编码器不承担业务校验。`payload` 该有哪些字段由各事件的契约规定；编码器一旦开始
     判「不能为空」，就与 `model.apply` 形成了两处需要同步维护的判断。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. `encode_event` 把 `payload` 深拷贝成 `dict` 再序列化，而 `Event.payload` 只承诺是
     `Mapping`。如果某个工具返回的映射在编码之后、入参方仍持有引用并被修改，第 5 轮会
     看到什么现象？
  2. 如果给 `codec` 加一个版本号字段，读旧记录时应该由 `codec` 负责迁移，还是由日志层？
     迁移代码放在 `codec` 里会带来什么依赖？

- **学习者原回答摘要**：本轮未答题。

- **纠偏与仍未理解的部分**：学习者能判断“该报错”，但第一版把“报错”写成了在错误的层、
  用错误的异常类。说明卡点是**职责边界**与**异常归属**，不是语法。下一轮起在规格里
  显式标注“这一段属于谁的职责”。

- **债务**：
  1. `codec` 不校验 payload 的业务字段 → 第 5 轮写入前的最后校验。
  2. 对账逻辑只存在于演示脚本 → 第 6 轮。
  3. `lookup` 不区分记录类型 → 第 6 轮。
  4. `validate` 崩溃与参数不合法共用 `kind=invalid_args` → 第 7 轮时间线。
  5. 前几轮遗留：`args` 共享引用、`RUN_FAILED` 原因未进视图、`replay` 信任顺序、
     账本无版本号、读改写无锁、CI 未远端运行、Docker 未触发。

- **本轮状态**：实现与验证完成；**复盘待进行**（两道架构问题未答，连同第 3 轮两题）。

## 第 5 轮：SQLite 事件日志

- **本轮目标与架构位置**：把事件从内存搬到 SQLite，让「进程死了，视图还能重建」成为可验证的
  事实。位置：`journal` 夹在 `engine` 与 `codec` 之间——`append` 负责分配 `seq` 并落盘，
  `read` 负责按序读回并交给 `replay`；`engine` 一行未改，因为它只认 `EventLog` 协议。

- **已确认的契约 / 决策，以及学习者的理由**：
  - D22 两张表（`runs` 存计数器、`events` 存整条 body）、D23 `body` 存 `encode_event` 完整
    输出、D24 每次操作新开连接。学习者回复「按你推荐的」。

- **学习者的手工推演**：本轮未做。设计阶段由 Agent 直接给出表结构与事务边界。

- **遇到的问题与最小复现**：
  1. `sqlite3` 默认 `isolation_level=""` 会在任何写语句前插入隐式 `BEGIN`（DEFERRED），
     之后再发 `BEGIN IMMEDIATE` 会报 `cannot start a transaction within a transaction`。
     规避方式：连接时传 `isolation_level=None`，事务边界全部手写。这是本轮唯一必须提前
     处理的坑，不是运行后才发现的。
  2. 若 `BEGIN IMMEDIATE` 本身失败（例如 `database is locked`），此时事务并未开启，
     直接 `ROLLBACK` 会二次抛错并盖掉原始异常。改为 `if conn.in_transaction:` 后再回滚。
  3. `ruff format` 把多段 SQL 字符串拼接合并回单行，导致两次格式检查失败。最终按
     formatter 的输出为准，而不是按手写的折行。
  4. 演示脚本里一条 print 超过 100 列（`E501`），需拆成两段 f-string；`ruff format`
     单独拆行后仍超长，说明 formatter 不解决行宽，只有 `check` 会报。

- **最初假设 / Agent 是否误判**：
  - 误判：以为 `append` 的「取 seq 与写入」必然需要 `RETURNING`。实际用
    `SELECT last_seq` + `UPDATE` 两步即可，且在 `BEGIN IMMEDIATE` 下同样安全；
    `RETURNING` 在 SQLite 3.35 才可用，多一个版本约束。
  - 未误判：`body` 存整条事件、`seq` 另设一列，这个冗余设计在 `read` 时提供了
    「列与内容不一致」的校验点，测试 `test_bad_body_raises_decode_error` 依赖它。

- **验证命令、结果与证据**：
  - `pytest tests/test_journal_sqlite.py` → 13 passed
  - `pytest` → 159 passed
  - `mypy` → `Success: no issues found in 10 source files`
  - `ruff check .` → `All checks passed!`；`ruff format --check .` → `26 files already formatted`
  - `python scripts/recovery_demo.py` → 退出码 0。5 条事件重开后与原序列相等、
    重放视图与停止时相等；崩溃现场 3 条事件重放为 `RUNNING` / `attempts=1` / `outcome=None`。

- **根因与可迁移规律**：
  1. 「读计数器 + 写新值」这类两步操作，正确性完全取决于事务的**开始时机**，而不是
     语句本身。DEFERRED 与 IMMEDIATE 的差别只在并发下暴露，单线程测试永远发现不了；
     并发测试（4 线程 × 5 次）是唯一能证明它写对了的手段。
  2. 失败路径的顺序同样属于契约：先 `encode` 再写计数器，才能保证编码失败不消耗 `seq`。
     反过来写，异常被捕获后序号已经跳号，而跳号在恢复时会被读成「丢了一条事件」。
  3. 冗余可以换来校验点。`seq` 同时存在列里和 body 里，看起来重复，但正是它让
     「记录被改过」变成可检测的错误，而不是悄悄重放出一个错误视图。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. 现在 `read` 把一个 run 的全部事件一次性载入内存再 `replay`。如果某个 run 有十万条
     事件，第一个出问题的不是内存，而是别的东西——是什么？
  2. `Engine.run` 每次都会追加 `RUN_STARTED`，所以已有日志无法直接续跑。要让引擎「接着
     上次的视图继续」，改动最小的做法是什么？（提示：`model.apply` 的终态规则）

- **学习者原回答摘要**：本轮未答题。**复盘题已累积 6 道**（第 3 轮 2 道、第 4 轮 2 道、
  第 5 轮 2 道），均与第 6 轮对账/CLI 的设计直接相关。

- **纠偏与仍未理解的部分**：本轮实现由 Agent 完成，学习者未参与落笔，因此「是否真正掌握
  事务边界与并发」没有任何证据。下一轮若继续，建议由学习者独立重写 `append` 并在不看
  答案的情况下通过并发测试。

- **债务**：
  1. `Engine.run` 每次都追加 `RUN_STARTED` → 无法在已有日志上续跑 → 第 6 轮 CLI。
  2. 崩溃现场靠演示脚本手工 `append` 三条事件 → 引擎没有「走到一半停下」的正常入口 → 第 6 轮。
  3. `replay` 仍不校验输入顺序（`SqliteEventLog` 用 `ORDER BY seq` 缓解）→ 第 6 轮接外部事件源。
  4. `codec` 不校验 payload 的业务字段 → 第 6 轮写入前的最后校验。
  5. 前几轮遗留：`args` 共享引用、`RUN_FAILED` 原因未进视图、账本无版本号、读改写无锁、
     CI 未远端运行、Docker 未触发。

- **本轮状态**：实现与验证完成；**复盘待进行**。

## 第 6 轮：续跑、对账与 CLI

- **本轮目标与架构位置**：把“日志里存着事实”变成“能从事实接着走”。三个新件：
  `Engine.resume`（引擎侧的续跑入口）、`reconcile.py`（把悬空步骤补记成确定）、
  `cli.py`（把模块组装成命令）。`journal`、`model`、`tools` 一行未改。

- **已确认的契约 / 决策，以及学习者的理由**：
  - D25 续跑预算取 `view.max_steps`、D26 续跑终态任务返回视图而不抛异常、
    D27 CLI 读 JSON 脚本 + 标准库 `argparse`。学习者回复“全部由你完成”，未逐条给理由。

- **学习者的手工推演**：本轮未做。

- **遇到的问题与最小复现**：
  1. `Engine.run` 无条件追加 `RUN_STARTED`，导致已有日志无法续跑（`model.apply` 会拒绝
     第二条 `RUN_STARTED`）。修法是把主循环抽成 `_drive(run_id, max_steps)`，`run` 只负责
     写首条事件。既有的 159 个测试全部仍绿，说明抽取没有改变契约。
  2. `resume` 遇到「最后一步 `STEP_STARTED` 但没有结果」时若直接进入主循环，
     `policy.decide` 会按 `len(view.steps)` 取下一个动作，**那一步被永久跳过**。改为在
     `resume` 入口就返回 `UNCERTAIN`，一步也不执行。
  3. CLI 测试里给 `status` 传了 `--ledger`，`argparse` 直接 `SystemExit: 2`
     （`unrecognized arguments`）。`status` 不接账本参数，测试需分开构造参数。
  4. `ScriptedPolicy` 的索引是 `len(view.steps)`。对账补记之后步数变成 1，续跑会取
     `script[1]`；测试第一次只给了单元素脚本，于是 `ScriptExhausted`。这暴露了一个真实
     约束：**续跑提供的脚本必须与原脚本按步号对齐**，否则行为整体偏移一位。
  5. PowerShell 的 `Out-File -Encoding utf8` 在 5.1 会写 BOM，`json.loads` 读带 BOM 的
     文本会报 `Expecting value`。改用 `utf-8-sig` 读取，两种写法都能读。

- **最初假设 / Agent 是否误判**：
  - 误判：设计 `status` 子命令时以为“所有子命令都该接受 `--db` 和 `--ledger`”。
    实际上 `status` 只读日志，多一个参数就多一条错误路径。
  - 未误判：`reconcile` 只补记成功、不为 `found=False` 写任何事件，这个保守取舍与
    第 3 轮定下的「`None` 只表示此刻查不到」完全一致，没有在实现阶段被简化掉。

- **验证命令、结果与证据**：
  - `pytest` → `183 passed`
  - `mypy` → `Success: no issues found in 12 source files`
  - `ruff check .` → `All checks passed!`；`ruff format --check .` → `31 files already formatted`
  - `python scripts/reconcile_demo.py` → 退出码 0。崩溃现场 3 条事件 → 首次 `resume`
    返回 `UNCERTAIN` 且余额保持 `750/250` → 对账补记 step 1 → 再次 `resume` 跑完 8 条事件，
    最终余额仍是 `750/250`
  - `python -m agentrun.cli run` + `status` → 退出码 0，5 条事件，状态 `SUCCEEDED`

- **根因与可迁移规律**：
  1. 「恢复」不是「重跑」。`resume` 的第一件事是判断“能不能接着跑”，而不是直接进入循环；
     判断错了，`steps` 的索引会让程序在不报错的情况下跳过一步。
  2. 补记的语义是**用外部世界的事实回填日志**，不是重放调用。因此补记的 `result` 与原始
     返回值不保证同构——凡是下游依赖 `result` 内部字段的逻辑，都要接受这一点。
  3. CLI 的参数是 API。给不需要账本的子命令加 `--ledger`，代价是使用者多一条会失败的路径。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. 若某个任务在第 1 步就 `RUN_EXHAUSTED`，此时 `resume` 会走哪条分支？返回什么状态？
     这个行为符合“预算属于任务”的设定吗？
  2. `reconcile` 补记的 `result` 来自账本记录（`found` / `kind` / `amount`），不是
     `Transfer.run` 原本的返回值（`request_id` / `src` / `dst` / `amount`）。如果下游有逻辑
     读取 `result["src"]`，补记之后会发生什么？

- **学习者原回答摘要**：本轮未答题。**复盘题累积 8 道**（第 3 轮 2、第 4 轮 2、第 5 轮 2、
  第 6 轮 2）。

- **纠偏与仍未理解的部分**：第 6 轮全部实现仍由 Agent 完成。到这一轮为止，学习者已经
  连续四轮没有亲手写业务代码，也连续四轮没有答题；「能读懂」与「能写对」之间的差距仍未
  被验证。下次交互建议先做一次不看答案的重写（任选 `Engine.resume` 或 `reconcile`）。

- **债务**：
  1. `reconcile` 只能补记成功，没有判定“确实没生效”的证据来源 → 需要人工判定时。
  2. 策略不持久化，`resume` 必须重新提供一致的脚本 → 第 8 轮模型策略。
  3. `codec` 不校验 payload 的业务字段 → 第 7 轮写入前的最后校验。
  4. `lookup` 不区分记录类型 → 需要区分 transfer / receipt 时。
  5. 前几轮遗留：`args` 共享引用、`RUN_FAILED` 原因未进视图、`replay` 信任顺序、
     账本无版本号、读改写无锁、CI 未远端运行、Docker 未触发。

- **本轮状态**：实现与验证完成；**复盘待进行**。

## 第 7 轮：时间线与观察者隔离

- **本轮目标与架构位置**：把事件渲染成人能读的形状，并保证观察行为不会反向污染事实。
  位置：`trace` 挂在 `events` / `codec` 之上，只读；`engine`、`journal` 一行未改。

- **已确认的契约 / 决策，以及学习者的理由**：
  - D28 深拷贝隔离 + `TraceLine` 不携带 payload 引用、D29 校验失败分
    `invalid_args` / `validator_crashed` 两种 kind。学习者回复「继续」，实现由 Agent 完成。

- **学习者的手工推演**：本轮未做。

- **遇到的问题与最小复现**：
  1. 第 1 轮起就挂着的债务「`StepView.args` 与 payload 共享同一 dict」在这一轮兑现。
     修法是在 `apply` 里 `deepcopy(dict(args))`。第一想法是 `dict(args)` 浅拷贝，但
     `args` 允许嵌套 dict/list，观察者改内层就能穿过隔离，测试
     `test_step_args_nested_structures_are_isolated` 专门盯这一点。
  2. `run_tool` 原来把 `validate` 抛的所有异常都标成 `kind="invalid_args"`，导致
     「用户参数写错」和「校验函数自己有 bug」在日志里长得一样。改为按异常类型分
     `invalid_args` / `validator_crashed`，两者仍都是 FAILED（工具未执行）。
  3. `trace` 一开始想接受 `EventLog` 而不是 `Sequence[Event]`，但那会让观察者能顺手
     调 `append`。改成只接事件序列，观察者拿不到写入口。
  4. `export_jsonl` 里 `path.open(..., newline="\n")` 不能省：Windows 默认写 `\r\n`，
     交给外部工具消费时会出现多余回车。

- **最初假设 / Agent 是否误判**：
  - 误判：以为「监听器」需要一个长驻的订阅机制。实际上本轮的收益全部来自「只读 +
    副本」，实时通道留作债务。加了订阅反而要为退出、异常、背压引入新契约。
  - 未误判：`timeline` 不排序。日志乱序本身是问题，替调用方排好会把问题藏了。

- **验证命令、结果与证据**：
  - `pytest` → `200 passed`
  - `mypy` → `Success: no issues found in 13 source files`
  - `ruff check .` → `All checks passed!`；`ruff format --check .` → `34 files already formatted`
  - `python -m agentrun.cli trace --run-id demo` → 5 行时间线，形如
    `2  STEP_DECIDED   决定第 1 步：get_balance {'owner': 'alice'}`
  - `--export trace.jsonl` → 5 行，每行独立可被 `decode_event` 读回

- **根因与可迁移规律**：
  1. 「只读」不能靠自觉。只要把 `EventLog` 交给观察者，它就能 `append`；正确的做法是
     在类型上就不给写入口。
  2. 共享可变对象是延迟爆炸的漏洞。第 1 轮埋下，直到第 7 轮才因为「有人真的去改视图」
     而暴露；如果一直没人改，它永远不会报错，但也就永远不安全。
  3. 异常分类的粒度决定日志的诊断价值。`invalid_args` 与 `validator_crashed` 都归
     FAILED，但前者改输入、后者改代码，合成一个 kind 就等于把两类工作混在一起。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. `apply` 现在对每个 `STEP_DECIDED` 做 `deepcopy`。如果回放十万条事件，这个选择的
     代价体现在哪里？有没有既隔离又不必每条都复制的做法？
  2. `TraceLine` 里没有 `payload`。如果使用者需要看完整参数，应该从哪里取？这样做
     和「直接把 payload 塞进 TraceLine」在安全性上差在哪？

- **学习者原回答摘要**：本轮未答题。**复盘题累积 10 道**。

- **纠偏与仍未理解的部分**：第 7 轮实现仍由 Agent 完成。至此七轮里学习者亲手写的
  业务代码只有第 3 轮的部分工具与第 4 轮第一版 `codec`（未通过）。项目的「能跑」已经
  成立，「学习者的能力」仍未被证明。

- **债务**：
  1. `trace` 只能一次性读取，没有边跑边看的实时通道 → 需要观察长任务时。
  2. `reconcile` 只能补记成功 → 需要人工判定失败时。
  3. 策略不持久化 → 第 8 轮模型策略。
  4. `RUN_FAILED` 的 reason 未进视图 → 需要结构化失败原因时。
  5. 前几轮遗留：`replay` 信任顺序、账本无版本号、读改写无锁、CI 未远端运行、Docker 未触发。

- **本轮状态**：实现与验证完成；**复盘待进行**。

## 第 8 轮：模型策略与决策失败

- **本轮目标与架构位置**：把「下一步做什么」从调用方写死的脚本换成模型输出，同时给
  「策略给不出动作」定一条可断言零副作用的失败路径。位置：新增 `llm.py`，挂在 `policy` /
  `model` 之上、只被调用方装配；`engine.py` 只在 `decide` 外面加一个 `except PolicyError`，
  主循环其余部分未动。

- **已确认的契约 / 决策，以及学习者的理由**：
  - D30 `PolicyError` → `RUN_FAILED`、D31 严格解析不剥代码块、D32 工具是否存在仍由引擎判、
    D33 `ScriptExhausted` 继承 `PolicyError`。学习者回复「A，全部由你来完成」，实现与文档由
    Agent 完成。

- **学习者的手工推演**：本轮未做。

- **遇到的问题与最小复现**：
  1. `parse_action` 的未知字段检查第一版写成「只允许 `tool` / `finish`」，于是每个合法的
     `args` 都被判成未知字段，`test_parse_tool_call` 直接红。修法：允许集合随形状变化——
     带 `tool` 时是 `{tool, args}`，带 `finish` 时是 `{finish}`。
  2. 演示脚本把 `SCRIPT[-1]`（一段 JSON 文本）当成期望答案去比较，三条链路都对，结论却打印
     「不符合预期」。这正是本轮要区分的两件事：模型输出的文本 ≠ 解析后的 `Finish.answer`。
  3. 崩溃点原本选在 `decide` 之前，导致「同一视图 → 同一提示」根本演示不出来——模型还没
     看到那一步的提示就断了。改成「先拿到动作再抛 `SystemExit`」：日志仍停在 1 步，两份提示
     才可比。
  4. `json.loads` 接受 `bytes`。不先判 `isinstance(text, str)`，`b'{"tool": ...}'` 会一路解析
     成功；协议写的是 `str`，运行时不会替你检查。

- **最初假设 / Agent 是否误判**：
  - 误判：一开始觉得剥掉 ```json 代码块是「贴心」。实际效果是「模型没按约定输出」有时能跑、
    有时不能，而这两种情况在日志里长得一样。改为硬拒绝。
  - 未误判：`PolicyError` 不写 `UNCERTAIN`。决策失败时一个工具都没调用；`UNCERTAIN` 的语义
    是「可能已经改了」。把两者合并会抹掉整个项目最贵的区分。

- **验证命令、结果与证据**：
  - `pytest` → `236 passed`（新增 36 例：`test_llm.py` 33、引擎 2、策略 1）
  - `mypy` → `Success: no issues found in 14 source files`
  - `ruff check .` → `All checks passed!`；`ruff format --check .` → `37 files already formatted`
  - `python scripts/model_policy_demo.py` → 退出码 0。A：3 次模型调用、8 条事件、余额
    `750/250`；B：`RUN_STARTED` + `RUN_FAILED`、0 步、`1000/0`，原因是「模型输出不是合法
    JSON」；C：崩在 1 步之后，重启后提示指纹同为 `6fec2dbedd27`，续跑到 `SUCCEEDED`
  - CLI：正常脚本 `SUCCEEDED` / 退出码 0；只含 1 步的脚本 → `RUN_FAILED` / 退出码 1
    （改动前是未捕获的 `ScriptExhausted`）

- **根因与可迁移规律**：
  1. 「确定性」要靠边界保证，不能靠模型自觉。提示里只允许出现视图与工具清单；时间、随机数、
     进程状态一旦进去，续跑就变成另一场决策。
  2. 把外部组件的异常收敛成自己的异常，是分层最省事的做法：`ModelPolicy` 不认识
     `TimeoutError`，引擎也不认识模型 SDK。
  3. 严格解析比宽容解析好维护。宽容把格式问题延后成行为差异，而行为差异只能靠人读日志猜。
  4. 一个判断只在一个地方做。工具是否存在由注册表判，模型不查；查两处就必然有一天不一致。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. 提示是 `view` 的纯函数。若模型在第 3 步给出一个「参数合法、业务上明显不合理」的调用
     （比如转 250 万），当前链路里谁会拦住它？应该在哪一层拦？
  2. `ModelPolicy` 每次都重新渲染整段历史，而 `resume` 又要求提示逐字节一致。步数增长后这
     两个要求会在哪里冲突？可能的折中是什么？

- **学习者原回答摘要**：本轮未答题。**复盘题累积 12 道**（第 3 轮 2、第 4 轮 2、第 5 轮 2、
  第 6 轮 2、第 7 轮 2、第 8 轮 2）。

- **纠偏与仍未理解的部分**：第 8 轮实现仍由 Agent 完成。八轮里学习者亲手写的业务代码仍是
  第 3 轮的部分工具与第 4 轮第一版 `codec`（未通过）。建议下一次交互先做一次不看答案的重写
  （任选 `parse_action` 或 `Engine.resume`）。

- **债务**：
  1. 模型不保证确定性：同一提示两次可能不同动作，续跑依赖温度设置 → 接真实服务时。
  2. 提示没有长度预算，长 `result` 会撑大提示 → 结果变长时。
  3. CLI 仍只装配脚本策略 → 接真实服务时。
  4. `reconcile` 只能补记成功 → 需要人工判定失败时。
  5. 前几轮遗留：`RUN_FAILED` 的 reason 未进视图、`replay` 信任顺序、账本无版本号、
     读改写无锁、CI 未远端运行、Docker 未触发。

- **本轮状态**：实现与验证完成；**复盘待进行**。

## 第 9 轮：人工复核

- **本轮目标与架构位置**：把「自动对账查不到」从死胡同变成一条会结束的路径。位置：
  `model` 增加暂停态与两种复核事件，`reconcile` 增加提交复核与 `resolve`，
  `engine.resume` 多一个闸门；`journal`、`codec`、`tools` 一行未改。

- **已确认的契约 / 决策，以及学习者的理由**：
  - D34 分工变更：学习者只读，Agent 代行决策与全部实现，等待点 A/B/C 取消。
    学习者原话「整个项目全部由你来完成，我最后会阅读全部内容」。
  - D35 复核三值结论、D36 `WAITING_REVIEW` 是暂停态、D37 查不到升级为复核、
    D38 复核后重做使用新 `step_key`。理由均为「不知道要有出口、重做不等于重试」。

- **学习者的手工推演**：本轮未做（按 D34 分工已变更）。

- **遇到的问题与最小复现**：
  1. 第一版测试用 `log.append` 断言非法事件会抛异常，8 个用例全红在
     `DID NOT RAISE`。根因：`journal` 只分配 `seq` 并落盘，合法性由 `model.apply`
     判断。改成 `apply(view, ev(last_seq + 1, ...))` 后全部通过——分层没变，是测试
     测错了层。
  2. `abort` 如果顺手把该步记成 FAILED，视图就会声称「确定没生效」，而终止恰恰不掌握
     这个事实。改为不动 `outcome`，只让状态表达「不再继续」。
  3. 演示脚本里 `NothingToResolve` 导入未使用，被 ruff 当场抓到。

- **最初假设 / Agent 是否误判**：
  - 误判：一开始想让 `resolve(applied)` 复用 `STEP_COMPLETED` 的写入路径，结果一个
    事件里既要放结论又要放依据，信息不够。改为 `REVIEW_RESOLVED` 自带 `result`，
    语义才完整。
  - 未误判：`WAITING_REVIEW` 做成暂停态而不是终态。`resume` 的四种分支没有新增
    类型，只多一个原因值；如果设成终态，复核后必须另开一个 run，日志会断成两截。

- **验证命令、结果与证据**：
  - `pytest` → `258 passed`（新增 22 例）
  - `mypy` → `Success: no issues found in 14 source files`
  - `ruff check .` → `All checks passed!`；`ruff format --check .` → `39 files already formatted`
  - `python scripts/review_demo.py` → 退出码 0。A：查不到 → `WAITING_REVIEW` →
    `not_applied` → 重做，9 条事件、`SUCCEEDED`、余额 `750/250`；B：`abort` →
    `ABORTED`，再次 `resume` 不追加事件、余额 `1000/0`
  - CLI 端到端：seeded 崩溃现场 → `reconcile`（0，已提交复核）→ `resolve
    --decision applied`（0）→ `resume`（0，答案写入视图）

- **根因与可迁移规律**：
  1. 「不知道」需要一个出口。只有 `UNCERTAIN` 没有复核，系统只能永远停在不知道；
     有了复核，它才是一条会结束的路径。
  2. 人工结论必须落成事件，不能只改内存；否则重启后结论消失，等于没复核。
  3. 重做与重试是两种动作：重试复用调用键，重做必须换新的键，否则工具端的去重会
     吞掉第二次真实业务。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. 崩溃窗口里根本没有 `STEP_UNCERTAIN` 事件——进程没来得及写结果。这对「一步的
     outcome 如何表达」意味着什么？如果改成先补写 `STEP_UNCERTAIN` 再复核，会多出
     什么约束？
  2. `resolve` 只写事件、不碰工具。若复核结论是 `applied`，但账本后来又变了，视图
     与外部世界还能对齐吗？该由谁、在什么时候发现这种偏差？

- **学习者原回答摘要**：分工变更，本轮不答题。**复盘题累积 14 道**。

- **纠偏与仍未理解的部分**：实现全部由 Agent 完成（按 D34）。仍未验证的是学习者
  能否独立重写；这条只能等最终阅读时由学习者自己确认。

- **债务**：
  1. 复核依据只是自由文本，系统无法判断证据真伪 → 需要审计时。
  2. 复核没有超时与升级策略，任务可能无限停在 `WAITING_REVIEW` → 接入多人运维时。
  3. 复核请求不会自动重试对账，外部系统延迟写入时只能靠人 → 出现最终一致的外部
     系统时。
  4. 前几轮遗留：`RUN_FAILED` 的 reason 未进视图、`replay` 信任顺序、账本无版本号、
     读改写无锁、CI 未远端运行、Docker 未触发。

- **本轮状态**：实现与验证完成；**复盘待进行**。

## 第 10 轮：进程级故障矩阵与离线评测集

- **本轮目标与架构位置**：把「重启后能恢复」从模拟升级为真实进程验证，并补上「只有
  决策、没有意图」的恢复路径。位置：`engine.resume` 增加一条执行既有步骤的分支，
  `model` 允许 `UNCERTAIN` 被对账收敛，`reconcile.pending_step` 承认未知步骤；新增
  `scripts/crash_runner.py`（测试夹具）与 `scripts/eval_suite.py`（评测集）。

- **已确认的契约 / 决策**：D39 `STEP_UNCERTAIN` 是可收敛状态，允许随后一条
  `STEP_COMPLETED`；D40 只有决策没有意图时复用决策、不重新问策略。

- **学习者的手工推演**：未做（D34）。

- **遇到的问题与最小复现**：
  1. 评测集第一次跑是 7/8，坏在「已生效→对账补记」：工具抛异常后引擎写下
     `STEP_UNCERTAIN`，而 `pending_step` 只找 `outcome is None` 的步骤，于是对账说
     「没有待对账的步骤」。后一轮 `resume` 也不拦——最后一步是 UNCERTAIN 时它直接
     继续往下决策。
     根因：前几轮的恢复测试全都用「崩溃窗口」（结果事件没写），从来没测过「写了不知道」
     的情形。`UNCERTAIN` 被当成终局结果，实际只是一条「此刻无法确认」的记录。
     修法：`pending_step` 纳入 UNCERTAIN；`resume` 遇到 UNCERTAIN 返回 `UNCERTAIN`；
     `REVIEW_REQUESTED` / `REVIEW_RESOLVED` 接受未知步骤；模型只放行
     `UNCERTAIN → STEP_COMPLETED` 这一种收敛。
  2. 评测集里「工具拒绝」用 600 元，结果余额变成 400/600——演示账本的规则是
     「余额不得低于 300」，600 并不越限。改成 800 才触发拒绝。写测试时最容易犯的错，
     是拿自己以为的规则当被测系统的规则。
  3. 崩溃夹具的预算先写 2 步：重做一步 + 结束一步用完预算，`Finish` 没有机会落地，
     任务停在 EXHAUSTED。改成 3 步后通过。边界不是实现错误，是夹具没算清。
  4. `os._exit` 在 `append` 内的位置决定窗口：写在 `inner.append` 之前是「意图未落盘」，
     写在之后是「意图已落盘、工具未调用」。第一版把两者写反，`attempts` 对不上。

- **最初假设 / Agent 是否误判**：
  - 误判：以为 `STEP_UNCERTAIN` 已经是终局结果，只要写下来就够了。它其实是三种状态里
    最需要后续处理的那个。
  - 未误判：决策复用。`STEP_DECIDED` 落盘而 `STEP_STARTED` 没落盘时，工具一定没被
    调用（协议顺序保证），直接执行既安全又不会换掉参数。

- **验证命令、结果与证据**：
  - `pytest` → `268 passed`；`mypy` → 14 files，0 error；`ruff check` 通过；
    `ruff format --check` → 42 files
  - `python scripts/eval_suite.py` → `8/8 通过`，总计约 150 ms
  - `tests/test_process_crash.py` → 三个窗口：`before_tool` 账本未动、`after_tool`
    已生效且对账后 `STEP_STARTED` 只有 1 条、`after_decided` 策略首次调用时
    `len(steps) == 1`
  - 子进程退出码 9，由新进程恢复；恢复后余额分别落在 `750/250` 或 `1000/0`

- **根因与可迁移规律**：
  1. 「三种结果」里最危险的是 UNCERTAIN，因为它既不是成功也不是失败；只要状态机不给
     它出口，系统就会在别处把「不知道」悄悄当成「没发生」。
  2. 崩溃窗口要在**每两次相邻写入**之间验证，而不是只挑一个最像的窗口。三个窗口对应
     三种事实，恢复动作完全不同。
  3. 评测集的价值不在「证明能跑」，而在「暴露你以为的规则和实现的规则不一样」——
     本轮两处（600 元拒绝、预算 2 步）都是这类。
  4. 进程级测试必须真退进程。抛异常会走 `finally`、会关连接，那是另一条路径。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. `UNCERTAIN → STEP_COMPLETED` 的收敛只允许一次。若对账先后给出矛盾证据（先查到、
     后又查不到），当前设计会怎么表现？应该信哪一次？
  2. 三个崩溃窗口里，`after_tool` 的恢复依赖「工具端按 `step_key` 去重」。如果某个
     外部工具没有幂等键，本项目的恢复语义还能成立吗？边界应该写在哪一层？

- **学习者原回答摘要**：未答题。**复盘题累积 16 道**。

- **纠偏与仍未理解的部分**：实现全部由 Agent 完成。`os._exit` 不等于 `kill -9` 或
  断电，文件系统缓存与 SQLite 的提交语义未验证；这条已写进债务，不在本轮声称。

- **债务**：
  1. 进程级故障只模拟 `os._exit`，未验证 `kill -9`、断电与 fsync 丢失 → 需要真机或
     容器级故障注入时。
  2. 评测集绑在 `demo.Ledger` 上，没有真实外部系统与网络抖动 → 接入真实服务时。
  3. 前几轮遗留：`RUN_FAILED` 的 reason 未进视图、`replay` 信任顺序、账本无版本号、
     读改写无锁、CI 未远端运行、Docker 未触发。

- **本轮状态**：实现与验证完成；**复盘待进行**。

## 第 11 轮：v1 硬化（失败原因与提示预算）

- **本轮目标与架构位置**：收掉两笔拖了七轮的债——失败原因藏在事件里、提示没有长度
  上限。位置：`model.RunView` 增加 `failure_reason`，`cli.print_view` 打印它；
  `llm.build_prompt` / `ModelPolicy` 增加 `field_limit`，渲染层按字段截断。

- **已确认的契约 / 决策**：D41 失败原因原文进视图、不解析不分类；D42 按字段截断并
  标注截掉多少，而不是整段提示一刀切。

- **学习者的手工推演**：未做（D34）。

- **遇到的问题与最小复现**：
  1. 第一版把截断加在整段提示上：`prompt[:limit]`。结果工具清单与输出约定一起被切掉，
     模型只能看到半张表。改为在 `_json` / `_render_step` 里按字段截断，结构完整。
  2. 截断标记本身也算长度：先截 200 字再拼「截断 N 字」，总长会超过 200。测试用
     `len(prompt) < 800` 而不是 `<= field_limit`，明确断言的是「不会无限增长」，不是
     「恰好等于上限」。
  3. `failure_reason` 一开始想加进 `answer` 旁边并顺便格式化（如「失败：xxx」）。
     最后保留原文：一旦这里开始做文案，日志与视图就会出现两套说法。

- **最初假设 / Agent 是否误判**：
  - 误判：以为「提示预算」就是截断总长度。真正要保护的是结构——任务、工具、输出约定
    必须完整，只有历史里的长字段可以被裁。
  - 未误判：失败原因只做转发。它是证据的指针，不是给用户看的文案。

- **验证命令、结果与证据**：
  - `pytest` → `271 passed`（新增 3 例 + 1 条补断言）
  - `mypy` → 14 files，0 error；`ruff check` 通过；`ruff format --check` → 42 files
  - `python scripts/eval_suite.py` → `8/8 通过`；7 个教学脚本退出码均 0
  - CLI `status` 在失败任务上输出 `失败原因：未注册的工具：'nope'`

- **根因与可迁移规律**：
  1. 给模型看的文本要保留结构：预算应该作用在「可变的字段」上，而不是整段字符串。
  2. 一切进入提示的变换都必须是确定性的：截断位置、标记文字、排序规则，任何一处随
     环境变化，续跑就变成另一次决策。
  3. 视图只存事实、不做措辞。「失败：xxx」这种加工放在展示层，视图一加工，日志与
     视图就开始各说各话。

- **架构问题（学习者先答，Agent 不附答案）**：
  1. `field_limit=200` 会固定丢掉尾部信息。若某类工具的关键字段总在尾部（例如长回执的
     结论），除了调大上限，还有什么办法既控制长度又不丢结论？
  2. `failure_reason` 现在是一段自由文本。若要在 CLI 里按失败类型过滤（配置错、策略错、
     工具错），应该改事件契约，还是在展示层解析字符串？

- **学习者原回答摘要**：未答题。**复盘题累积 18 道**。

- **纠偏与仍未理解的部分**：实现全部由 Agent 完成。提示预算只保证「不会无限增长」，
  没有按 token 估算；这条写进债务。

- **债务**：
  1. 截断按字符数而不是 token，中文与代码的 token 密度不同 → 接入真实模型时。
  2. 截断会丢尾部信息，长回执的结论可能在后面 → 结果内容变长时。
  3. 前几轮遗留：`replay` 信任顺序、账本无版本号、读改写无锁、CI 未远端运行、
     Docker 未触发。

- **本轮状态**：实现与验证完成；**复盘待进行**。
