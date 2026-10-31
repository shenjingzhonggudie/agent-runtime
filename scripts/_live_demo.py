"""公开演示共用装配：模型真实联网，业务账本保留在隔离的本地目录。"""

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from agentrun.cli import LEDGER_TOOL_SPECS
from agentrun.demo import Ledger
from agentrun.engine import EngineResult
from agentrun.ledger_tools import GetBalance, LookupRequest, SaveReceipt, Transfer
from agentrun.llm import ModelClient, ModelPolicy
from agentrun.model_client import ModelClientError, client_from_env
from agentrun.tools import SideEffect, ToolContext, ToolRegistry


class CountingClient:
    """转发每一次真实请求，只记录次数与提示指纹，不保存提示或凭据。"""

    def __init__(self, inner: ModelClient) -> None:
        self._inner = inner
        self.calls = 0
        self.completed = 0
        self.prompt_digests: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.prompt_digests.append(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12])
        text = self._inner.complete(prompt)
        self.completed += 1
        return text


def build_live_policy() -> tuple[ModelPolicy, CountingClient]:
    """从本地配置或环境变量建立真实模型策略，没有模拟模型回退。"""
    configured = client_from_env()
    print(f"真实模型：{configured.model}；业务对象：隔离的模拟账本", flush=True)
    client = CountingClient(configured)
    return ModelPolicy(client, LEDGER_TOOL_SPECS), client


def build_registry(ledger: Ledger, *, transfer: Any = None) -> ToolRegistry:
    """注册四个账本工具；允许演示显式替换转账工具以注入故障。"""
    registry = ToolRegistry()
    for tool in (
        GetBalance(ledger),
        Transfer(ledger) if transfer is None else transfer,
        SaveReceipt(ledger),
        LookupRequest(ledger),
    ):
        registry.register(tool)
    return registry


def transfer_task(*, receipt: bool = False) -> str:
    """明确业务目标与安全边界，下一步动作仍由真实模型产生。"""
    tail = "成功后调用一次 save_receipt 保存转账说明，再" if receipt else "成功后"
    return (
        "查询 alice 的余额，然后从 alice 向 bob 转账 250，整个任务只需成功转账一次。"
        f"{tail}返回 finish 总结实际结果。"
        "每次只输出一个动作；先查看历史，不重复已成功的查询、转账或回执。"
        "如果历史转账已成功（包括对账补记 found=true），不要再转账。"
        "如果人工复核明确 not_applied 且没有成功转账，允许用一个新步骤转账一次。"
        "业务明确拒绝后直接 finish 说明原因，不变更金额反复尝试。"
    )


def show_result(result: EngineResult, client: CountingClient) -> None:
    """展示真实请求计数、停止原因与模型给出的最终文本。"""
    print(f"真实模型请求：{client.calls} 次，取得文本：{client.completed} 次")
    print(f"停止原因：{result.stop_reason}；任务状态：{result.view.status}")
    if result.view.answer is not None:
        print(f"模型结论：{result.view.answer}")
    if result.view.failure_reason is not None:
        print(f"失败原因：{result.view.failure_reason}")


class LostResponseTransfer:
    """显式注入业务调用故障；工具参数与是否调用工具仍由真实模型决定。"""

    name = "transfer"
    side_effect = SideEffect.NON_IDEMPOTENT

    def __init__(self, ledger: Ledger, *, applied: bool) -> None:
        self._inner = Transfer(ledger)
        self._applied = applied
        self.calls = 0

    def validate(self, args: Mapping[str, Any], ctx: ToolContext) -> None:
        self._inner.validate(args, ctx)

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        self.calls += 1
        if self._applied:
            self._inner.run(args, ctx)
        print(f"[故障注入] 转账{'已生效' if self._applied else '未执行'}，调用响应丢失")
        raise RuntimeError("演示注入：工具响应丢失")


def run_demo(main: Callable[[], None]) -> None:
    """配置失败时打印脱敏提示并返回非零退出码。"""
    try:
        main()
    except ModelClientError as exc:
        print(f"模型配置不可用：{exc}")
        raise SystemExit(2) from None
