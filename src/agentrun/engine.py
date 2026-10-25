"""任务驱动器：把策略、工具与事件日志串成一个循环。

它只做三件事：按契约顺序追加事件、调用策略与工具、决定何时停止。
它不实现工具、不实现策略、不做持久化事务、不做对账。

依赖方向：
    engine → {policy, tools, journal, model, events}
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .events import Event, EventType, make_step_key
from .journal import EventLog, Lease, LeaseBusy, LeaseLost
from .model import Outcome, RunStatus, RunView, replay
from .policy import Finish, Policy, PolicyError
from .tools import Tool, ToolContext, ToolRegistry, ToolStatus, UnknownTool, run_tool


class StopReason(StrEnum):
    """本次 ``Engine.run`` 为什么停下来。

    注意它与 ``RunStatus`` 不是一回事：遇到 UNCERTAIN 时循环停了，但任务状态仍是
    RUNNING——事实不足，既不成功也不失败，等对账或人工判断（第 6 轮）。

    FAILED 有两种来源，都满足「零副作用」：工具名未注册（配置错误）与策略给不出动作
    （``PolicyError``）。不要把它们记成 UNCERTAIN——那只留给定不下来的时候。

    ``WAITING_REVIEW`` 与 ``ABORTED`` 来自日志里的复核记录：引擎自己不请求复核，
    只在 ``resume`` 时把状态原样带回，让调用方知道「这里需要人」还是「已经放弃了」。

    ``LOST`` 表示本次工作期间租约被接管：写入被日志拒绝，任务状态仍是 ``RUNNING``
    （或由接管者推进）。这不是任务的结局，是这个 worker 的结局。
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXHAUSTED = "EXHAUSTED"
    UNCERTAIN = "UNCERTAIN"
    WAITING_REVIEW = "WAITING_REVIEW"
    ABORTED = "ABORTED"
    LOST = "LOST"


class NothingToResume(Exception):
    """日志里没有该任务的任何事件，无法续跑。

    抛出它表示调用方给错了 ``run_id``，而不是「任务已经跑完了」。已经跑完的任务
    走 ``Engine.resume`` 会正常返回现有视图，不会抛它。
    """


@dataclass(frozen=True, slots=True)
class EngineResult:
    """一次运行的结果。

    Attributes:
        view: 停止时的视图，由日志重放得到。
        stop_reason: 停止原因。
    """

    view: RunView
    stop_reason: StopReason


class Engine:
    """串行驱动一次任务运行。

    Args:
        policy: 决策来源。
        registry: 工具注册表。
        log: 事件日志（v0 为内存实现）。
        owner: 可选的 worker 标识。给出时，``run`` / ``resume`` 会先取租约，之后每次
            写入都带 epoch 栅栏；没拿到租约抛 ``LeaseBusy``，工作途中被接管则返回
            ``StopReason.LOST``。
        lease_ttl: 租约时长（秒），默认 30。每写一条事件前续租一次。

    Notes:
        ``owner=None`` 时完全退化为 v1 的单 worker 行为：不取租约、不校验栅栏。
        这是刻意的——单进程演示不该被并发语义拖慢。
    """

    def __init__(
        self,
        policy: Policy,
        registry: ToolRegistry,
        log: EventLog,
        *,
        owner: str | None = None,
        lease_ttl: float = 30.0,
    ) -> None:
        self._policy = policy
        self._registry = registry
        self._log = log
        self._owner = owner
        self._lease_ttl = lease_ttl
        self._lease: Lease | None = None

    def _append(self, run_id: str, type_: EventType, payload: Mapping[str, Any]) -> Event:
        """按当前租约追加事件。

        Args:
            run_id: 任务标识。
            type_: 事件类型。
            payload: 事件字段。

        Returns:
            Event: 已写入的事件。

        Raises:
            LeaseLost: 租约已被接管或过期；本次写入没有发生。

        Notes:
            每次写入前先续租：工具调用可能耗时，靠「开始时取一次」撑不到最后。续租
            与写入之间仍有窗口，所以真正的保护在日志的栅栏校验——续租只是把窗口缩小。
        """
        if self._lease is None:
            return self._log.append(run_id, type_, payload)
        self._lease = self._log.renew(self._lease, self._lease_ttl)
        return self._log.append(run_id, type_, payload, lease=self._lease)

    def _acquire(self, run_id: str) -> None:
        """取得租约；未配置 owner 时什么也不做。

        Raises:
            LeaseBusy: 租约仍被其他 worker 占用。
        """
        if self._owner is None:
            return
        lease = self._log.acquire(run_id, self._owner, self._lease_ttl)
        if lease is None:
            raise LeaseBusy(f"{run_id!r} 的租约被其他 worker 占用")
        self._lease = lease

    def _view(self, run_id: str) -> RunView:
        """从日志重放当前视图。

        Notes:
            每次都用 ``replay`` 而不是增量 ``apply``：v0 的步数很小，用“事实重建”
            可以让一致性无可争辩——视图永远等于日志的折叠结果。
        """
        return replay(self._log.read(run_id))

    def run(self, run_id: str, task: str, max_steps: int) -> EngineResult:
        """执行一次任务，直到成功、失败、耗尽或结果未知。

        Args:
            run_id: 任务标识，由调用方生成并保持稳定。
            task: 任务描述，写入 RUN_STARTED。
            max_steps: 最大决策步数（按 STEP_DECIDED 计数，重试不额外消耗）。

        Returns:
            EngineResult: 停止时的视图与原因。

        Raises:
            InvalidEvent: 事件序列非法（属于编程错误，不应在正常运行中出现）。
            LeaseBusy: 配置了 ``owner``，但租约被其他 worker 持有。

        Notes:
            只负责写 ``RUN_STARTED``，随后把控制权交给 ``_drive``。同一个 ``run_id``
            重复调用会再写一条 ``RUN_STARTED``，而 ``model.apply`` 会拒绝它——要接着
            上次的进度继续，请用 ``resume``。
        """
        self._acquire(run_id)
        try:
            self._append(run_id, EventType.RUN_STARTED, {"task": task, "max_steps": max_steps})
            return self._drive(run_id, max_steps)
        except LeaseLost:
            events = self._log.read(run_id)
            if not events:
                raise
            return EngineResult(view=replay(events), stop_reason=StopReason.LOST)

    def resume(self, run_id: str) -> EngineResult:
        """在已有日志上继续执行。

        Args:
            run_id: 已存在于日志中的任务标识。

        Returns:
            EngineResult: 停止时的视图与原因。预算取 ``view.max_steps``，即任务自己
            的预算，不接收新的预算参数。

        Raises:
            NothingToResume: 日志里没有该任务的任何事件。
            InvalidEvent: 日志内容非法（编程错误或存储损坏，不应在正常运行中出现）。
            LeaseBusy: 配置了 ``owner``，但租约被其他 worker 持有。

        Notes:
            五种情形，只有第 3 条会追加事件：

            1. 视图已是终态（成功 / 失败 / 预算耗尽 / 人工终止）→ 原样返回，
               ``stop_reason`` 由状态推出。
            2. 视图在等待人工复核 → 返回 ``WAITING_REVIEW``。复核结论只能由
               ``reconcile.resolve`` 写，引擎不能替人猜。
            3. 最后一步只有 ``STEP_DECIDED``、没有 ``STEP_STARTED``（``attempts == 0``）
               → **复用已落盘的决策**：补写意图并执行该工具，不调用策略。按协议此时
               工具一定没被调用过，所以重新执行是安全的；重新决策反而会让日志里留下
               两个决策，也可能换掉用户已经确认的参数。
            4. 最后一步没有终局结论（没有结果事件，或结果是 ``UNCERTAIN``）→ 返回
               ``UNCERTAIN``。外部世界可能已经改变，既不能重做也不能跳过，必须先对账
               或复核。
            5. 其余情况 → 交给 ``_drive`` 继续。

            第 4 条是这一层最重要的判断：宁可停着不动，也不用“重试一次看看”去赌一个
            非幂等调用没有生效。
        """
        events = self._log.read(run_id)
        if not events:
            raise NothingToResume(f"没有可续跑的任务：{run_id!r}")
        view = replay(events)
        if view.status is not RunStatus.RUNNING:
            return EngineResult(view=view, stop_reason=StopReason(view.status.value))
        self._acquire(run_id)
        try:
            if view.steps:
                last = view.steps[-1]
                if last.outcome is None and last.attempts == 0:
                    try:
                        tool = self._registry.get(last.tool)
                    except UnknownTool:
                        self._append(
                            run_id,
                            EventType.RUN_FAILED,
                            {"reason": f"未注册的工具：{last.tool!r}"},
                        )
                        return EngineResult(view=self._view(run_id), stop_reason=StopReason.FAILED)
                    stop = self._execute(run_id, last.step, tool, last.args, attempt=1)
                    if stop is not None:
                        return stop
                    return self._drive(run_id, view.max_steps)
                if last.outcome is None or last.outcome is Outcome.UNCERTAIN:
                    return EngineResult(view=view, stop_reason=StopReason.UNCERTAIN)
            return self._drive(run_id, view.max_steps)
        except LeaseLost:
            return EngineResult(view=self._view(run_id), stop_reason=StopReason.LOST)

    def _execute(
        self,
        run_id: str,
        step: int,
        tool: Tool,
        args: Mapping[str, Any],
        attempt: int,
    ) -> EngineResult | None:
        """执行一个已经决策的步骤：写意图、调用工具一次、写结果。

        Args:
            run_id: 任务标识。
            step: 步骤号；该步必须已有 ``STEP_DECIDED``。
            tool: 已解析的工具对象；不在这里查注册表，避免同一个判断做两次。
            args: 决策时的参数；直接交给工具的 ``validate``。
            attempt: 尝试次数，v0 恒为 1。

        Returns:
            EngineResult | None: 结果为 UNCERTAIN 时返回停止结果，其他情况返回
            ``None``，由调用方决定继续还是结束。

        Raises:
            InvalidEvent: 追加的事件与日志状态冲突。

        Notes:
            ``STEP_STARTED`` 必须先写：它把「打算调用」和「已经交给外部世界」分成两个
            可观察时刻。如果先执行后写意图，崩溃窗口就会变成「做了但日志不知道」，而
            本项目的整个恢复语义都建立在这条顺序上。
        """
        self._append(run_id, EventType.STEP_STARTED, {"step": step, "attempt": attempt})
        ctx = ToolContext(
            run_id=run_id,
            step=step,
            attempt=attempt,
            step_key=make_step_key(run_id, step),
        )
        outcome = run_tool(tool, args, ctx)

        if outcome.status == ToolStatus.SUCCEEDED:
            self._append(
                run_id,
                EventType.STEP_COMPLETED,
                {"step": step, "result": dict(outcome.value or {})},
            )
            return None
        if outcome.status == ToolStatus.FAILED:
            self._append(
                run_id,
                EventType.STEP_FAILED,
                {"step": step, "error": dict(outcome.error or {})},
            )
            return None
        self._append(
            run_id, EventType.STEP_UNCERTAIN, {"step": step, "reason": outcome.reason or ""}
        )
        return EngineResult(view=self._view(run_id), stop_reason=StopReason.UNCERTAIN)

    def _drive(self, run_id: str, max_steps: int) -> EngineResult:
        """主循环：按契约顺序追加事件、调用策略与工具、决定何时停止。

        Args:
            run_id: 任务标识。
            max_steps: 最大决策步数。

        Returns:
            EngineResult: 停止时的视图与原因。

        Raises:
            InvalidEvent: 事件序列非法。

        Notes:
            每轮顺序（不可调换）：

            1. 已决策步数 ``len(view.steps) >= max_steps`` → 追加 RUN_EXHAUSTED，
               停止，原因 EXHAUSTED。**预算判断在问策略之前**，所以 ``max_steps=0``
               一次决策都不做。
            2. 调 ``policy.decide(view)``。抛 ``PolicyError`` → 追加 RUN_FAILED，
               停止，原因 FAILED。返回 ``Finish`` → 追加 RUN_SUCCEEDED，停止，原因
               SUCCEEDED。
            3. 返回 ``ToolCall`` → 从注册表取工具；``UnknownTool`` → 追加
               RUN_FAILED，停止，原因 FAILED（配置错误，不是业务失败）。
            4. 追加 STEP_DECIDED（``step = len(view.steps) + 1``，
               ``step_key = make_step_key(run_id, step)``）。
            5. 调 ``_execute``：先追加 STEP_STARTED（``attempt = 1``），再调
               ``run_tool`` 一次，按结果追加 STEP_COMPLETED / STEP_FAILED /
               STEP_UNCERTAIN。**顺序不能反**：先记决策，再记意图，最后才允许碰外部
               世界。
            6. 结果是 UNCERTAIN → 立即停止，原因 UNCERTAIN，视图保持 RUNNING，
               不再追加任何事件。其他结果回到第 1 步。

            工具结果的三种分类由 ``run_tool`` 判定，这里只按结果追加事件，不重新
            解释异常：一旦引擎自己猜测"这个异常大概没事"，UNCERTAIN 就失去意义。
        """
        while True:
            view = self._view(run_id)
            if len(view.steps) >= max_steps:
                self._append(run_id, EventType.RUN_EXHAUSTED, {"max_steps": max_steps})
                return EngineResult(view=self._view(run_id), stop_reason=StopReason.EXHAUSTED)
            try:
                action = self._policy.decide(view)
            except PolicyError as exc:
                self._append(run_id, EventType.RUN_FAILED, {"reason": str(exc)})
                return EngineResult(view=self._view(run_id), stop_reason=StopReason.FAILED)

            if isinstance(action, Finish):
                self._append(run_id, EventType.RUN_SUCCEEDED, {"answer": action.answer})
                return EngineResult(view=self._view(run_id), stop_reason=StopReason.SUCCEEDED)
            try:
                tool = self._registry.get(action.tool)
            except UnknownTool:
                self._append(
                    run_id, EventType.RUN_FAILED, {"reason": f"未注册的工具：{action.tool!r}"}
                )
                return EngineResult(view=self._view(run_id), stop_reason=StopReason.FAILED)

            step = len(view.steps) + 1
            self._append(
                run_id,
                EventType.STEP_DECIDED,
                {
                    "step": step,
                    "tool": action.tool,
                    "args": dict(action.args),
                    "step_key": make_step_key(run_id, step),
                },
            )
            stop = self._execute(run_id, step, tool, action.args, attempt=1)
            if stop is not None:
                return stop
