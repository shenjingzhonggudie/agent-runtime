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
