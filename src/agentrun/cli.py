"""命令行入口：把已有模块组装成可执行命令。

九个子命令，各自只做一件事：

- ``ask``：用一句话跑一个任务，打印结论与余额。外人的入口。
- ``ledger``：初始化账本或查看余额。
- ``run``：从 JSON 脚本或模型决策跑一个新任务。
- ``resume``：在已有日志上继续跑。
- ``status``：打印视图与每一步的当前状态。
- ``reconcile``：为悬空的那一步补记结论；查不到则提交人工复核。
- ``resolve``：写下人工复核结论（applied / not_applied / abort）。
- ``trace``：打印时间线，或把事件导出成 JSONL。
- ``verify``：校验事件哈希链，报告第一个断点。

默认装配 ``ScriptedPolicy``，保持原有脚本命令兼容；``--policy model`` 则从本地配置与环境变量
构造客户端并装配 ``ModelPolicy``。脚本模式续跑仍需提供脚本；模型模式从日志中的任务
与历史步骤重新决策，不需要脚本。工具始终经注册表与引擎执行。

依赖方向：
    cli → {engine, journal, policy, llm, model_client, tools, reconcile, ledger_tools, demo}
"""

import argparse
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .demo import Ledger, LedgerBusy, LedgerCorrupted
from .engine import Engine, NothingToResume, StopReason
from .journal import JournalCorrupted, Lease, LeaseBusy, RunAlreadyStarted, SqliteEventLog
from .ledger_tools import GetBalance, LookupRequest, SaveReceipt, Transfer
from .llm import ModelPolicy, ToolSpec
from .model import REVIEW_DECISIONS, InvalidEvent, RunView, replay
from .model_client import ModelClientError, client_from_env
from .policy import Action, Finish, Policy, ScriptedPolicy, ToolCall
from .reconcile import (
    NothingToReconcile,
    NothingToResolve,
    pending_step,
    reconcile,
    resolve_review,
)
from .tools import ToolRegistry
from .trace import export_jsonl, timeline

DEFAULT_DB = Path("agentrun.db")
DEFAULT_LEDGER = Path("ledger")
LEDGER_TOOL_SPECS = (
    ToolSpec(
        "get_balance",
        "查询账户余额。参数 owner: str，必需且非空；只读，无业务副作用。"
        "返回 owner: str、balance: int；未知账户余额为 0。",
    ),
    ToolSpec(
        "transfer",
        "转账。参数 src: str、dst: str，必需且非空；amount: int，必需且大于 0，"
        "不接受 bool。非幂等写操作，重复调用会重复转账；结果未知时不得自动重做。"
        "返回 request_id、src、dst、amount；request_id 由引擎生成，无需传入。",
    ),
    ToolSpec(
        "save_receipt",
        "保存回执。参数 text: str，必需且非空。幂等写操作：同一引擎 step_key "
        "重复调用只保存首次内容；不同步骤使用不同键。返回 request_id、text；"
        "request_id 由引擎生成，无需传入。",
    ),
    ToolSpec(
        "lookup_request",
        "查询已生效的操作。参数 request_id: str，必需且非空。只读，无业务副作用。"
        "查到返回 found: true、kind: str、amount: int；未查到返回 found: false，"
        "不能据此断言操作从未生效。",
    ),
)


def build_registry(ledger_path: Path) -> ToolRegistry:
    """按账本目录构造带有四个账本工具的注册表。

    Args:
        ledger_path: 模拟账本目录；不存在时创建。

    Returns:
        ToolRegistry: 已注册 ``get_balance`` / ``transfer`` / ``save_receipt`` /
        ``lookup_request`` 的注册表。
    """
    ledger = Ledger(ledger_path)
    registry = ToolRegistry()
    for tool in (GetBalance(ledger), Transfer(ledger), SaveReceipt(ledger), LookupRequest(ledger)):
        registry.register(tool)
    return registry


def load_actions(path: Path) -> list[Action]:
    """从 JSON 文件读出策略脚本。

    Args:
        path: 脚本文件路径，内容为数组，每项要么 ``{"tool": ..., "args": {...}}``，
            要么 ``{"finish": "..."}``。

    Returns:
        list[Action]: 按顺序执行的 ``ToolCall`` 与 ``Finish``。

    Raises:
        ValueError: 文件不是合法 JSON、顶层不是数组、某一项既没有 ``tool`` 也没有
            ``finish``、或字段类型不符。交给调用方决定怎么报告。
    """
    try:
        # utf-8-sig：容忍 Windows 记事本与 PowerShell 5.1 写出的 BOM
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"读不出脚本：{path}（{exc}）") from exc
    if not isinstance(raw, list):
        raise ValueError("脚本顶层必须是数组")

    actions: list[Action] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"脚本第 {index} 项必须是对象")
        if "finish" in item:
            answer = item["finish"]
            if not isinstance(answer, str):
                raise ValueError(f"脚本第 {index} 项的 finish 必须是字符串")
            actions.append(Finish(answer))
        elif "tool" in item:
            name = item["tool"]
            args = item.get("args", {})
            if not isinstance(name, str):
                raise ValueError(f"脚本第 {index} 项的 tool 必须是字符串")
            if not isinstance(args, dict):
                raise ValueError(f"脚本第 {index} 项的 args 必须是对象")
            actions.append(ToolCall(name, args))
        else:
            raise ValueError(f"脚本第 {index} 项必须含 tool 或 finish")
    return actions


def print_view(view: RunView) -> None:
    """打印任务视图，一步一行。"""
    print(f"任务：{view.task}")
    print(f"状态：{view.status}")
    print(f"步数：{len(view.steps)}/{view.max_steps}")
    if view.answer is not None:
        print(f"答案：{view.answer}")
    if view.failure_reason is not None:
        print(f"失败原因：{view.failure_reason}")
    if view.decision_failures:
        print(f"决策失败：{view.decision_failures} 次（任务仍可 resume）")
    for step in view.steps:
        outcome = step.outcome.value if step.outcome is not None else "-"
        print(f"  {step.step:>2}  {step.tool:<14} attempts={step.attempts} outcome={outcome}")
    if view.review is not None:
        review = view.review
        if review.decision is None:
            print(f"复核：step {review.step} 等待人工结论（{review.reason}）")
        else:
            print(f"复核：step {review.step} → {review.decision}（{review.note}）")
    pending = pending_step(view)
    if pending is not None and view.review is None:
        print(f"待对账：step {pending}（已开始，没有结果）")


def exit_code_for(stop: StopReason) -> int:
    """把停止原因映射成进程退出码。

    Args:
        stop: 引擎返回的停止原因。

    Returns:
        int: 只有 ``SUCCEEDED`` 是 0，其余都非 0——``EXHAUSTED`` 是预算用尽，
        不是成功。
    """
    return 0 if stop is StopReason.SUCCEEDED else 1


def build_policy(args: argparse.Namespace, *, new_run: bool) -> Policy:
    """校验策略参数并构造策略；在创建日志或账本之前暴露配置错误。"""
    if args.policy == "script":
        if args.script is None:
            raise ValueError("script 模式必须提供 --script")
        return ScriptedPolicy(load_actions(args.script))
    if args.script is not None:
        raise ValueError("model 模式不能同时提供 --script")
    if new_run and not args.task.strip():
        raise ValueError("model 模式的新任务必须提供非空 --task")
    return ModelPolicy(client_from_env(), LEDGER_TOOL_SPECS)


def cmd_run(args: argparse.Namespace) -> int:
    """执行 ``run`` 子命令。"""
    policy = build_policy(args, new_run=True)
    log = SqliteEventLog(args.db)
    engine = Engine(
        policy,
        build_registry(args.ledger),
        log,
        owner=args.owner,
        lease_ttl=args.lease_ttl,
    )
    try:
        result = engine.run(args.run_id, args.task or args.run_id, args.max_steps)
    except RunAlreadyStarted as exc:
        print(f"任务已存在：{exc}")
        print("要接着上次的进度跑，请用 resume；只想看现状，请用 status。")
        return 2
    except LeaseBusy as exc:
        print(f"没有拿到租约：{exc}")
        return 2
    print_view(result.view)
    print(f"停止原因：{result.stop_reason}")
    return exit_code_for(result.stop_reason)


def cmd_resume(args: argparse.Namespace) -> int:
    """执行 ``resume`` 子命令。"""
    policy = build_policy(args, new_run=False)
    log = SqliteEventLog(args.db)
    engine = Engine(
        policy,
        build_registry(args.ledger),
        log,
        owner=args.owner,
        lease_ttl=args.lease_ttl,
    )
    try:
        result = engine.resume(args.run_id)
    except NothingToResume as exc:
        print(f"无法续跑：{exc}")
        return 2
    except LeaseBusy as exc:
        print(f"没有拿到租约：{exc}")
        return 2
    print_view(result.view)
    print(f"停止原因：{result.stop_reason}")
    return exit_code_for(result.stop_reason)


def print_lease(lease: Lease | None) -> None:
    """打印租约现状：谁持有、还剩多久、是否已经过期。

    Args:
        lease: 当前登记的租约；``None`` 表示无人持有。

    Notes:
        「已过期」只表示此刻可以被接管，不表示对方一定已经停下——它可能只是慢。
        把这句话写死在输出里，是为了避免有人看到"过期"就以为"没人干活"。
    """
    if lease is None:
        print("租约：无人持有")
        return
    remaining = lease.expires_at - time.time()
    if remaining > 0:
        print(f"租约：{lease.owner}（epoch {lease.epoch}），剩 {remaining:.1f} 秒")
    else:
        print(f"租约：{lease.owner}（epoch {lease.epoch}），已过期（可被接管）")


def cmd_status(args: argparse.Namespace) -> int:
    """执行 ``status`` 子命令。"""
    log = SqliteEventLog(args.db)
    events = log.read(args.run_id)
    if not events:
        print(f"没有这个任务：{args.run_id}")
        return 2
    for event in events:
        print(f"{event.seq:>3}  {event.type}")
    print()
    print_lease(log.lease_of(args.run_id))
    print_view(replay(events))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """执行 ``reconcile`` 子命令。"""
    log = SqliteEventLog(args.db)
    registry = build_registry(args.ledger)
    try:
        result = reconcile(log, args.run_id, registry.get("lookup_request"))
    except NothingToReconcile as exc:
        print(f"无需对账：{exc}")
        return 2
    print(result.detail)
    if result.recorded:
        print_view(replay(log.read(args.run_id)))
        return 0
    if result.requested:
        print("任务已进入等待复核：用 resolve 给出结论")
        return 0
    return 2


def cmd_resolve(args: argparse.Namespace) -> int:
    """执行 ``resolve`` 子命令。"""
    result: dict[str, Any] | None = None
    if args.result is not None:
        try:
            parsed = json.loads(args.result)
        except ValueError as exc:
            raise ValueError(f"--result 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--result 必须是 JSON 对象")
        result = parsed

    log = SqliteEventLog(args.db)
    try:
        outcome = resolve_review(
            log,
            args.run_id,
            decision=args.decision,
            note=args.note,
            result=result,
            resolution_id=args.resolution_id,
        )
    except NothingToResolve as exc:
        print(f"无法复核：{exc}")
        return 2

    if not outcome.recorded:
        # 重复提交按成功处理：网关重试与手抖双击不该让调用方以为出了问题
        print(outcome.detail)
        return 0 if outcome.duplicate else 2

    print_view(outcome.view)
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    """执行 ``trace`` 子命令。"""
    log = SqliteEventLog(args.db)
    events = log.read(args.run_id)
    if not events:
        print(f"没有这个任务：{args.run_id}")
        return 2
    if args.export is not None:
        count = export_jsonl(events, args.export)
        print(f"已导出 {count} 条到 {args.export}")
        return 0
    for line in timeline(events):
        print(f"{line.seq:>3}  {line.kind:<14} {line.summary}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """执行 ``verify`` 子命令：逐条重算摘要，指出第一个断点。

    Args:
        args: 解析后的命令行参数，含 ``run_id``（可空）与 ``db``。

    Returns:
        int: 0 表示全部完好；1 表示至少一条任务链有断点；2 表示日志里没有任务。

    Notes:
        默认校验全部任务：审计的用途是「我不知道哪里被动过」，要求调用方先报出
        ``run_id`` 就把这个问题又推回给了人。
    """
    log = SqliteEventLog(args.db)
    run_ids = (args.run_id,) if args.run_id else log.run_ids()
    if not run_ids:
        print("日志里没有任何任务")
        return 2

    broken = 0
    for run_id in run_ids:
        report = log.verify_chain(run_id)
        if not report.ok:
            broken += 1
            print(f"{run_id}：断点在 seq {report.first_break} —— {report.reason}")
            print(f"  已校验 {report.hashed}/{report.total} 条")
        elif report.hashed == 0:
            print(f"{run_id}：{report.total} 条事件全部是迁移前的记录，没有摘要，无法校验")
        elif report.hashed < report.total:
            print(f"{run_id}：{report.hashed}/{report.total} 条带摘要，链完好（前段无法自证）")
        else:
            print(f"{run_id}：{report.total} 条事件，链完好")
    return 1 if broken else 0


def print_balances(ledger: Ledger) -> None:
    """打印全部账户余额，按账户名排序。

    Args:
        ledger: 已打开的账本。

    Notes:
        空账本也要说一句「还没有账户」：否则输出看起来像命令失败，人会去翻日志找错。
    """
    balances = ledger.snapshot()
    if not balances:
        print("  （账本里还没有账户）")
        return
    for name, value in sorted(balances.items()):
        print(f"  {name}: {value}")


def cmd_ledger_init(args: argparse.Namespace) -> int:
    """初始化账本并打印初始余额。

    Args:
        args: 含 ``ledger`` 目录与 ``account_a`` / ``account_b``。

    Returns:
        int: 0 表示建好或已存在，2 表示一个账户都没给。

    Raises:
        LedgerBusy: 另一个进程正在初始化同一个目录。

    Notes:
        账本已经存在时 ``initial`` 会被忽略——这是 ``Ledger`` 的构造语义，不能拿它重置
        余额。不在这里加「--force 覆盖」：重置该是一次显式、带备份的动作，不该藏在
        init 里顺手发生。
    """
    initial: dict[str, int] = {}
    if args.account_a is not None:
        initial["A"] = args.account_a
    if args.account_b is not None:
        initial["B"] = args.account_b
    if not initial:
        print("至少给一个初始余额，例如 --A=1000 --B=0")
        return 2

    ledger = Ledger(args.ledger, initial=initial)
    print(f"账本：{args.ledger}")
    print_balances(ledger)
    return 0


def cmd_ledger_balance(args: argparse.Namespace) -> int:
    """打印账本余额。

    Args:
        args: 含 ``ledger`` 目录与可选 ``account``。

    Returns:
        int: 0 表示读到；未知账户按 0 显示。
    """
    ledger = Ledger(args.ledger)
    if args.account is not None:
        print(f"{args.account}: {ledger.balance(args.account)}")
        return 0
    print_balances(ledger)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """用一句自然语言跑一个任务，并打印人能读的结果。

    Args:
        args: 含 ``prompt``、``run_id``（可空）、``db``、``ledger``、``max_steps``、
            ``owner`` 与 ``lease_ttl``。

    Returns:
        int: 与 ``run`` 一致——只有任务成功是 0；``DECISION_FAILED``、等待复核、
            预算耗尽都返回 1。

    Raises:
        ModelClientError: 配置缺失或不可用，由 ``main`` 统一转成退出码 2。

    Notes:
        ``run_id`` 省略时按时间生成。它要人可读、可复制：出问题时要拿着它去跑
        ``status`` 或 ``resume``，一串哈希不适合当这个入口。

        与 ``run`` 的差别只在输出：这个命令默认就是「我要看结果」，所以直接打印结论、
        步数与余额，并把下一步该敲什么写出来。
    """
    run_id = args.run_id or f"ask-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    policy = ModelPolicy(client_from_env(), LEDGER_TOOL_SPECS)
    log = SqliteEventLog(args.db)
    engine = Engine(
        policy,
        build_registry(args.ledger),
        log,
        owner=args.owner,
        lease_ttl=args.lease_ttl,
    )

    print(f"任务：{args.prompt}")
    try:
        result = engine.run(run_id, args.prompt, args.max_steps)
    except RunAlreadyStarted as exc:
        print(f"任务已存在：{exc}")
        return 2
    except LeaseBusy as exc:
        print(f"没有拿到租约：{exc}")
        return 2

    view = result.view
    print(f"结论：{view.answer if view.answer is not None else '（未得出结论）'}")
    print(f"步数：{len(view.steps)}/{view.max_steps}")
    print("余额：")
    print_balances(Ledger(args.ledger))
    print(f"停止原因：{result.stop_reason}")
    if result.stop_reason is not StopReason.SUCCEEDED:
        print(f"回看：agentrun status --run-id {run_id}")
        print(f"继续：agentrun resume --run-id {run_id} --policy model")
    return exit_code_for(result.stop_reason)


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(prog="agentrun", description="事件溯源执行引擎")
    sub = parser.add_subparsers(dest="command", required=True)

    ask_p = sub.add_parser("ask", help="用一句话跑一个任务（模型策略）")
    ask_p.add_argument("prompt", help="自然语言任务，例如「给 B 转 250」")
    ask_p.add_argument("--run-id", default=None, help="省略时按时间生成")
    ask_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    ask_p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ask_p.add_argument("--max-steps", type=int, default=10)
    ask_p.add_argument("--owner", default=None, help="worker 标识；给出则启用租约与 epoch 栅栏")
    ask_p.add_argument("--lease-ttl", type=float, default=45.0)
    ask_p.set_defaults(func=cmd_ask)

    ledger_p = sub.add_parser("ledger", help="初始化或查看模拟账本")
    ledger_sub = ledger_p.add_subparsers(dest="ledger_command", required=True)

    ledger_init = ledger_sub.add_parser("init", help="创建账本并设置初始余额")
    ledger_init.add_argument("--A", type=int, default=None, dest="account_a", help="账户 A 的余额")
    ledger_init.add_argument("--B", type=int, default=None, dest="account_b", help="账户 B 的余额")
    ledger_init.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ledger_init.set_defaults(func=cmd_ledger_init)

    ledger_balance = ledger_sub.add_parser("balance", help="打印账户余额")
    ledger_balance.add_argument("--account", default=None, help="只打印这个账户")
    ledger_balance.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ledger_balance.set_defaults(func=cmd_ledger_balance)

    run_p = sub.add_parser("run", help="从脚本或模型决策跑一个新任务")
    run_p.add_argument("--run-id", required=True)
    run_p.add_argument("--policy", choices=("script", "model"), default="script")
    run_p.add_argument("--script", type=Path, help="script 模式必需；model 模式不可使用")
    run_p.add_argument("--task", default="", help="任务描述；model 模式必需且非空")
    run_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    run_p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    run_p.add_argument("--max-steps", type=int, default=10)
    run_p.add_argument("--owner", default=None, help="worker 标识；给出则启用租约与 epoch 栅栏")
    run_p.add_argument("--lease-ttl", type=float, default=45.0)
    run_p.set_defaults(func=cmd_run)

    resume_p = sub.add_parser("resume", help="接着上次的进度继续")
    resume_p.add_argument("--run-id", required=True)
    resume_p.add_argument("--policy", choices=("script", "model"), default="script")
    resume_p.add_argument("--script", type=Path, help="script 模式必需；model 模式从日志恢复")
    resume_p.add_argument("--owner", default=None, help="worker 标识；给出则启用租约与 epoch 栅栏")
    resume_p.add_argument("--lease-ttl", type=float, default=45.0)
    resume_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    resume_p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    resume_p.set_defaults(func=cmd_resume)

    status_p = sub.add_parser("status", help="查看任务状态与事件")
    status_p.add_argument("--run-id", required=True)
    status_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    status_p.set_defaults(func=cmd_status)

    recon_p = sub.add_parser("reconcile", help="为悬空的一步补记结论")
    recon_p.add_argument("--run-id", required=True)
    recon_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    recon_p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    recon_p.set_defaults(func=cmd_reconcile)

    resolve_p = sub.add_parser("resolve", help="写下人工复核结论")
    resolve_p.add_argument("--run-id", required=True)
    resolve_p.add_argument("--decision", required=True, choices=REVIEW_DECISIONS)
    resolve_p.add_argument("--note", required=True, help="复核依据：谁、看了什么、为什么")
    resolve_p.add_argument("--result", default=None, help="applied 时的外部事实（JSON 对象）")
    resolve_p.add_argument(
        "--resolution-id",
        default=None,
        help="幂等键；同一个键重复提交只写一次结论，重复的那次按成功返回",
    )
    resolve_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    resolve_p.set_defaults(func=cmd_resolve)

    trace_p = sub.add_parser("trace", help="打印时间线或导出 JSONL")
    trace_p.add_argument("--run-id", required=True)
    trace_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    trace_p.add_argument("--export", type=Path, default=None)
    trace_p.set_defaults(func=cmd_trace)

    verify_p = sub.add_parser("verify", help="校验事件哈希链，报告第一个断点")
    verify_p.add_argument("--run-id", default=None, help="只校验这个任务；省略则校验全部")
    verify_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    verify_p.set_defaults(func=cmd_verify)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数并分派子命令。

    Args:
        argv: 命令行参数；省略时取 ``sys.argv[1:]``。

    Returns:
        int: 进程退出码。只有任务成功时为 0。

    Raises:
        SystemExit: 参数不合法时由 ``argparse`` 抛出。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    func = args.func
    try:
        return int(func(args))
    except ModelClientError:
        # 配置异常也可能来自第三方客户端，不原样输出异常，避免凭据进入终端。
        print(
            "模型配置错误：请检查 AGENTRUN_BASE_URL、AGENTRUN_API_KEY、"
            "AGENTRUN_MODEL 和 AGENTRUN_TIMEOUT"
        )
        return 2
    except JournalCorrupted as exc:
        print(f"事件日志不完整：{exc}")
        return 2
    except LedgerCorrupted as exc:
        print(f"账本不可读：{exc}")
        return 2
    except LedgerBusy as exc:
        print(f"账本忙：{exc}")
        return 2
    except InvalidEvent as exc:
        print(f"日志内容非法：{exc}")
        return 2
    except ValueError as exc:
        print(f"参数或脚本错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
