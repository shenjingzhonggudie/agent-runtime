"""真实模型产生动作；在决策未落盘时中断，再从同一视图恢复。

故障由演示代码注入。提示可以复现，真实模型的回答不保证逐字相同。
运行：.\\.venv\\Scripts\\python.exe scripts\\model_policy_demo.py
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from _live_demo import build_live_policy, build_registry, run_demo, show_result, transfer_task

from agentrun.demo import Ledger
from agentrun.engine import Engine, StopReason
from agentrun.journal import MemoryEventLog, SqliteEventLog
from agentrun.model import RunView
from agentrun.policy import Action, Policy


class InjectedInterruption(BaseException):
    """只用于演示的控制流中断，不伪装成真实进程退出。"""


class CrashAfter:
    """完成一个工具步骤后，真实模型下一次回答尚未交给引擎时中断。"""

    def __init__(self, inner: Policy) -> None:
        self._inner = inner

    def decide(self, view: RunView) -> Action:
        action = self._inner.decide(view)
        if len(view.steps) >= 1:
            raise InjectedInterruption
        return action


def scenario_happy_path(root: Path) -> bool:
    """所有工具选择与最后的结论都来自真实服务。"""
    print("\n=== 场景 A：真实模型完成一次转账 ===", flush=True)
    ledger = Ledger(root / "ledger", initial={"alice": 1000, "bob": 0})
    policy, client = build_live_policy()
    result = Engine(policy, build_registry(ledger), MemoryEventLog()).run(
        "model-happy", transfer_task(), max_steps=5
    )
    show_result(result, client)
    print(f"余额：alice={ledger.balance('alice')}  bob={ledger.balance('bob')}")
    return result.stop_reason is StopReason.SUCCEEDED and (
        ledger.balance("alice"),
        ledger.balance("bob"),
    ) == (750, 250)


def scenario_resume(root: Path) -> bool:
    """新客户端从持久化视图重建提示，放弃未落盘的回答后继续决策。"""
    print("\n=== 场景 B：注入决策中断，重开日志后继续询问真实模型 ===", flush=True)
    ledger_path = root / "ledger"
    ledger = Ledger(ledger_path, initial={"alice": 1000, "bob": 0})
    db = root / "events.db"
    first_policy, first_client = build_live_policy()
    interrupted = False
    try:
        initial = Engine(CrashAfter(first_policy), build_registry(ledger), SqliteEventLog(db)).run(
            "model-resume", transfer_task(), max_steps=5
        )
    except InjectedInterruption:
        interrupted = True
        print("[故障注入] 第一次工具已完成，下一次模型回答尚未写入决策日志时中断。")
    if not interrupted:
        show_result(initial, first_client)
        return False

    next_policy, next_client = build_live_policy()
    result = Engine(next_policy, build_registry(Ledger(ledger_path)), SqliteEventLog(db)).resume(
        "model-resume"
    )
    show_result(result, next_client)
    same_prompt = bool(next_client.prompt_digests) and (
        first_client.prompt_digests[-1] == next_client.prompt_digests[0]
    )
    print(f"崩前与恢复后的提示指纹相同：{same_prompt}")
    print(f"中断前真实请求：{first_client.calls} 次；恢复后：{next_client.calls} 次")
    print(f"最终余额：alice={ledger.balance('alice')}  bob={ledger.balance('bob')}")
    return (
        same_prompt
        and result.stop_reason is StopReason.SUCCEEDED
        and (ledger.balance("alice"), ledger.balance("bob")) == (750, 250)
    )


def main() -> None:
    """运行两个有界的真实模型场景；服务错误不会回退到固定答案。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        results = [scenario_happy_path(root / "a"), scenario_resume(root / "b")]
        print(f"\n演示核对：{sum(results)}/{len(results)} 通过")
        raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    run_demo(main)
