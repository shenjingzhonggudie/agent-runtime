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
