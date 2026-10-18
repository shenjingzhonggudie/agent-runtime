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

- **学习者原回答摘要**：待回答。

- **纠偏与仍未理解的部分**：待回答后填写。

- **债务**：
  1. CI 仅本地配置，远端未验证。触发信号：建立 GitHub 远端仓库。
  2. mypy 未引入。触发信号：第 1 轮事件/视图/公共接口成型。
  3. Docker 未引入。触发信号：CLI 启动方式稳定且出现跨机演示或容器交付需求。
  4. 场景存储形式（JSONL vs SQLite）与字段名未定。触发信号：第 2 轮工具契约。
  5. D6（存储分离）尚未由学习者显式确认。触发信号：第 1 轮事件契约确认。

- **本轮状态**：待复盘（实现与本地验证已完成，架构问题待学习者回答）
