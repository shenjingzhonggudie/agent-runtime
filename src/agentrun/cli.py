"""命令行入口：把已有模块组装成可执行命令。

六个子命令，各自只做一件事：

- ``run``：从 JSON 脚本跑一个新任务。
- ``resume``：在已有日志上继续跑。
- ``status``：打印视图与每一步的当前状态。
- ``reconcile``：为悬空的那一步补记结论；查不到则提交人工复核。
- ``resolve``：写下人工复核结论（applied / not_applied / abort）。
- ``trace``：打印时间线，或把事件导出成 JSONL。

本入口只装配 ``ScriptedPolicy``：它只依赖 ``len(view.steps)``，所以 ``resume`` 需要
重新给一份同样的脚本。这是刻意的——策略属于调用方，不属于日志。模型策略
（``llm.ModelPolicy``）同样可以由调用方装配，但它需要一个模型客户端，本入口不负责
连接模型服务。

依赖方向：
    cli → {engine, journal, policy, tools, reconcile, ledger_tools, demo}
"""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .demo import Ledger
from .engine import Engine, NothingToResume, StopReason
from .journal import JournalCorrupted, LeaseBusy, SqliteEventLog
from .ledger_tools import GetBalance, LookupRequest, SaveReceipt, Transfer
from .model import REVIEW_DECISIONS, InvalidEvent, RunView, replay
from .policy import Action, Finish, ScriptedPolicy, ToolCall
from .reconcile import (
    NothingToReconcile,
    NothingToResolve,
    pending_step,
    reconcile,
    resolve,
)
from .tools import ToolRegistry
from .trace import export_jsonl, timeline

DEFAULT_DB = Path("agentrun.db")
DEFAULT_LEDGER = Path("ledger")


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


def cmd_run(args: argparse.Namespace) -> int:
    """执行 ``run`` 子命令。"""
    actions = load_actions(args.script)
    log = SqliteEventLog(args.db)
    engine = Engine(
        ScriptedPolicy(actions),
        build_registry(args.ledger),
        log,
        owner=args.owner,
        lease_ttl=args.lease_ttl,
    )
    try:
        result = engine.run(args.run_id, args.task or args.run_id, args.max_steps)
    except LeaseBusy as exc:
        print(f"没有拿到租约：{exc}")
        return 2
    print_view(result.view)
    print(f"停止原因：{result.stop_reason}")
    return exit_code_for(result.stop_reason)


def cmd_resume(args: argparse.Namespace) -> int:
    """执行 ``resume`` 子命令。"""
    actions = load_actions(args.script)
    log = SqliteEventLog(args.db)
    engine = Engine(
        ScriptedPolicy(actions),
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
        view = resolve(
            log,
            args.run_id,
            decision=args.decision,
            note=args.note,
            result=result,
        )
    except NothingToResolve as exc:
        print(f"无法复核：{exc}")
        return 2

    print_view(view)
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


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(prog="agentrun", description="事件溯源执行引擎")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="从脚本跑一个新任务")
    run_p.add_argument("--run-id", required=True)
    run_p.add_argument("--script", required=True, type=Path)
    run_p.add_argument("--task", default="")
    run_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    run_p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    run_p.add_argument("--max-steps", type=int, default=10)
    run_p.add_argument("--owner", default=None, help="worker 标识；给出则启用租约与 epoch 栅栏")
    run_p.add_argument("--lease-ttl", type=float, default=30.0)
    run_p.set_defaults(func=cmd_run)

    resume_p = sub.add_parser("resume", help="接着上次的进度继续")
    resume_p.add_argument("--run-id", required=True)
    resume_p.add_argument("--script", required=True, type=Path)
    resume_p.add_argument("--owner", default=None, help="worker 标识；给出则启用租约与 epoch 栅栏")
    resume_p.add_argument("--lease-ttl", type=float, default=30.0)
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
    resolve_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    resolve_p.set_defaults(func=cmd_resolve)

    trace_p = sub.add_parser("trace", help="打印时间线或导出 JSONL")
    trace_p.add_argument("--run-id", required=True)
    trace_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    trace_p.add_argument("--export", type=Path, default=None)
    trace_p.set_defaults(func=cmd_trace)

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
    except JournalCorrupted as exc:
        print(f"事件日志不完整：{exc}")
        return 2
    except InvalidEvent as exc:
        print(f"日志内容非法：{exc}")
        return 2
    except ValueError as exc:
        print(f"参数或脚本错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
