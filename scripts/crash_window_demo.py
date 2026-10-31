"""真实模型选择一次转账动作，再比较三种存储设计的故障窗口。

只有动作选择需要请求模型；三种场景明确重放同一个真实模型动作，以便比较相同输入。
账本、崩溃与重启均为隔离存储实验，不是引擎进程故障测试，也不连接真实支付。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\crash_window_demo.py
"""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from _live_demo import build_live_policy

from agentrun.demo import Ledger
from agentrun.ledger_tools import Transfer
from agentrun.model import RunStatus, RunView
from agentrun.model_client import ModelClientError
from agentrun.policy import PolicyError, ToolCall
from agentrun.tools import ToolContext

INITIAL = {"alice": 1000, "bob": 0}
REQUEST = "storage-window:1"


def line(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def balances(ledger: Ledger) -> str:
    return f"alice={ledger.balance('alice')}  bob={ledger.balance('bob')}"


def apply_transfer(ledger: Ledger, call: ToolCall) -> None:
    """用真实模型生成的参数重放一次模拟账本调用。"""
    ledger.transfer(call.args["src"], call.args["dst"], call.args["amount"], REQUEST)


def scenario_no_log(root: Path, call: ToolCall) -> None:
    """注入丢失日志与错误重试，观察非幂等操作重复生效。"""
    line("场景 A：无持久化日志，注入错误重试")
    ledger = Ledger(root / "a", initial=INITIAL)
    print("[注入] 执行模型选定的转账动作，然后丢弃内存中的完成记录。")
    apply_transfer(ledger, call)
    print(f"[观察] 第一次执行后：{balances(ledger)}")
    print("[注入] 错误恢复代码无视未知状态，重放同一动作；这不是本项目的恢复策略。")
    apply_transfer(ledger, call)
    print(f"[观察] 第二次执行后：{balances(ledger)}")
    assert ledger.balance("alice") == 500 and ledger.balance("bob") == 500
    print("结论：同一 request_id 无法让非幂等转账自动去重。")


def scenario_intent_log(root: Path, call: ToolCall) -> None:
    """注入有意图无结果，依据外部证据停止重试。"""
    line("场景 B：意图已落盘、结果未落盘")
    world = root / "b"
    ledger = Ledger(world / "ledger", initial=INITIAL)
    log_path = world / "intent.jsonl"
    intent = {"event": "STEP_STARTED", "request_id": REQUEST, "args": dict(call.args)}
    log_path.write_text(json.dumps(intent) + "\n", encoding="utf-8")
    print("[注入] 写入教学用意图文件，执行转账，然后跳过结果记录。")
    apply_transfer(ledger, call)
    events = [json.loads(item) for item in log_path.read_text(encoding="utf-8").splitlines()]
    print(f"[观察] {len(events)} 条意图记录，没有结果；余额 {balances(ledger)}")
    found = ledger.lookup(REQUEST)
    print(f"[对账] 查到的外部记录类型：{found.kind if found else None}")
    assert found is not None and found.kind == "transfer"
    assert ledger.balance("alice") == 750 and ledger.balance("bob") == 250
    print("结论：查到已生效证据，保留结果，不再重做；真实引擎流程见 reconcile_demo.py。")


def scenario_same_storage(root: Path, call: ToolCall) -> None:
    """反事实：账本和完成记录在一次原子替换中共同提交。"""
    line("场景 C：同文件原子替换（存储反事实实验）")
    path = root / "c" / "world.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    def save(state: dict) -> None:
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(state), encoding="utf-8")
        os.replace(pending, path)

    amount = call.args["amount"]
    before = {"balances": dict(INITIAL), "log": []}
    after = {
        "balances": {"alice": INITIAL["alice"] - amount, "bob": INITIAL["bob"] + amount},
        "log": [{"request_id": REQUEST, "args": dict(call.args)}],
    }
    save(before)
    print("[注入] 计算好新状态，但跳过共同提交。")
    observed_before = json.loads(path.read_text(encoding="utf-8"))
    print(f"[观察] 磁盘：{observed_before}")
    assert observed_before == before
    save(after)
    print("[注入] 完成共同提交，跳过后续代码。")
    observed_after = json.loads(path.read_text(encoding="utf-8"))
    print(f"[观察] 磁盘：{observed_after}")
    assert observed_after == after
    print("结论：本实验的单文件替换没有半笔业务；真实外部系统通常无法这样共同提交。")


def main() -> None:
    """请求真实动作一次；失败时终止，绝不使用预置动作兜底。"""
    print("先请求真实模型选择动作，再对相同动作进行三个明确标注的存储实验。", flush=True)
    try:
        policy, client = build_live_policy()
        view = RunView(
            run_id="storage-window",
            task="现在直接调用 transfer 从 alice 向 bob 转账250，参数 src=alice、dst=bob、"
            "amount=250。不要预先查询，也不要结束。动作将用于隔离模拟账本存储实验。",
            max_steps=4,
            status=RunStatus.RUNNING,
        )
        action = policy.decide(view)
    except (ModelClientError, PolicyError) as exc:
        print(f"真实模型未能给出动作：{exc}")
        raise SystemExit(2) from None
    if not isinstance(action, ToolCall) or action.tool != "transfer":
        print("真实模型没有选择本实验所需的 transfer；本次停止。")
        raise SystemExit(1)
    if dict(action.args) != {"src": "alice", "dst": "bob", "amount": 250}:
        print("真实模型参数偏离实验输入；本次停止，未执行转账。")
        raise SystemExit(1)
    print(f"真实模型调用 {client.calls} 次；已选择 alice → bob，金额250。", flush=True)
    with TemporaryDirectory(prefix="agentrun-live-windows-") as tmp:
        root = Path(tmp)
        validator = Transfer(Ledger(root / "validation", initial=INITIAL))
        validator.validate(action.args, ToolContext("storage-window", 1, 1, REQUEST))
        scenario_no_log(root, action)
        scenario_intent_log(root, action)
        scenario_same_storage(root, action)


if __name__ == "__main__":
    main()
