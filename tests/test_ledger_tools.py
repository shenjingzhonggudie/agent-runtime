"""账本工具的契约测试。

全部通过 ``run_tool`` 调用，顺带验证“参数非法 / 业务拒绝 / 账本损坏”分别落到
哪种结果类别。断言重点不是返回值，而是**账本有没有被改动**。
"""

import pytest

from agentrun.demo import Ledger
from agentrun.ledger_tools import GetBalance, LookupRequest, SaveReceipt, Transfer
from agentrun.tools import SideEffect, ToolContext, ToolStatus, run_tool

CTX = ToolContext(run_id="run-1", step=2, attempt=1, step_key="run-1:2")
START = {"alice": 1000, "bob": 0}


def ledger_at(tmp_path, initial: dict[str, int] | None = None) -> Ledger:
    return Ledger(tmp_path / "world", initial=START if initial is None else initial)


# --- get_balance ---------------------------------------------------------


def test_get_balance_returns_owner_and_balance(tmp_path) -> None:
    outcome = run_tool(GetBalance(ledger_at(tmp_path)), {"owner": "alice"}, CTX)

    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.value == {"owner": "alice", "balance": 1000}


@pytest.mark.parametrize("args", [{}, {"owner": ""}, {"owner": "   "}, {"owner": 1}])
def test_get_balance_rejects_bad_owner(tmp_path, args: dict) -> None:
    outcome = run_tool(GetBalance(ledger_at(tmp_path)), args, CTX)

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error["kind"] == "invalid_args"


def test_get_balance_corrupted_ledger_is_failed_not_uncertain(tmp_path) -> None:
    root = tmp_path / "world"
    root.mkdir()
    (root / "ledger.json").write_text("{not json", encoding="utf-8")

    outcome = run_tool(GetBalance(Ledger(root)), {"owner": "alice"}, CTX)

    # 只读工具读盘失败：能证明没有写入，所以是 FAILED，不是 UNCERTAIN
    assert outcome.status is ToolStatus.FAILED
    assert outcome.status is not ToolStatus.UNCERTAIN


def test_get_balance_is_read_only(tmp_path) -> None:
    ledger = ledger_at(tmp_path)
    run_tool(GetBalance(ledger), {"owner": "alice"}, CTX)

    assert ledger.balance("alice") == 1000
    assert ledger.lookup(CTX.step_key) is None


# --- transfer ------------------------------------------------------------


def test_transfer_uses_step_key_as_request_id(tmp_path) -> None:
    ledger = ledger_at(tmp_path)
    outcome = run_tool(
        Transfer(ledger),
        {"src": "alice", "dst": "bob", "amount": 250},
        CTX,
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.value is not None
    assert outcome.value["request_id"] == CTX.step_key
    assert ledger.balance("alice") == 750
    assert ledger.balance("bob") == 250
    assert ledger.lookup(CTX.step_key) is not None


def test_transfer_ignores_request_id_supplied_by_args(tmp_path) -> None:
    ledger = ledger_at(tmp_path)
    run_tool(
        Transfer(ledger),
        {
            "src": "alice",
            "dst": "bob",
            "amount": 250,
            "request_id": "attacker-controlled",
        },
        CTX,
    )

    # 幂等键只能来自调用上下文，不能由参数决定
    assert ledger.lookup("attacker-controlled") is None
    assert ledger.lookup(CTX.step_key) is not None


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"src": "alice", "dst": "bob"},
        {"src": "alice", "dst": "bob", "amount": 0},
        {"src": "alice", "dst": "bob", "amount": -1},
        {"src": "alice", "dst": "bob", "amount": True},
        {"src": "", "dst": "bob", "amount": 250},
        {"src": "alice", "dst": 1, "amount": 250},
    ],
)
def test_transfer_rejects_bad_args_without_side_effect(tmp_path, args: dict) -> None:
    ledger = ledger_at(tmp_path)
    outcome = run_tool(Transfer(ledger), args, CTX)

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error["kind"] == "invalid_args"
    assert ledger.balance("alice") == 1000
    assert ledger.lookup(CTX.step_key) is None


def test_transfer_business_rejection_is_failed_not_uncertain(tmp_path) -> None:
    ledger = ledger_at(tmp_path, initial={"alice": 500, "bob": 0})
    outcome = run_tool(
        Transfer(ledger),
        {"src": "alice", "dst": "bob", "amount": 250},
        CTX,
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.status is not ToolStatus.UNCERTAIN
    assert outcome.error is not None
    assert outcome.error["kind"] == "rejected"
    assert ledger.balance("alice") == 500
    assert ledger.lookup(CTX.step_key) is None


def test_transfer_corrupted_ledger_is_failed_because_nothing_was_written(tmp_path) -> None:
    root = tmp_path / "world"
    root.mkdir()
    (root / "ledger.json").write_text("{not json", encoding="utf-8")
    ledger = Ledger(root)

    outcome = run_tool(
        Transfer(ledger),
        {"src": "alice", "dst": "bob", "amount": 250},
        CTX,
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.status is not ToolStatus.UNCERTAIN


# --- save_receipt --------------------------------------------------------


def test_save_receipt_is_idempotent(tmp_path) -> None:
    ledger = ledger_at(tmp_path)
    tool = SaveReceipt(ledger)

    first = run_tool(tool, {"text": "已转账 250"}, CTX)
    second = run_tool(tool, {"text": "另一份内容"}, CTX)

    assert first.status is ToolStatus.SUCCEEDED
    assert second.status is ToolStatus.SUCCEEDED

    record = ledger.lookup(CTX.step_key)
    assert record is not None
    assert record.kind == "receipt"
    assert record.text == "已转账 250"


@pytest.mark.parametrize("args", [{}, {"text": ""}, {"text": "   "}, {"text": 1}])
def test_save_receipt_rejects_bad_text(tmp_path, args: dict) -> None:
    ledger = ledger_at(tmp_path)
    outcome = run_tool(SaveReceipt(ledger), args, CTX)

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error["kind"] == "invalid_args"
    assert ledger.lookup(CTX.step_key) is None


# --- lookup_request ------------------------------------------------------


def test_lookup_reports_missing_request(tmp_path) -> None:
    outcome = run_tool(
        LookupRequest(ledger_at(tmp_path)),
        {"request_id": "run-1:9"},
        CTX,
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.value == {"found": False}


def test_lookup_finds_applied_transfer(tmp_path) -> None:
    ledger = ledger_at(tmp_path)
    run_tool(Transfer(ledger), {"src": "alice", "dst": "bob", "amount": 250}, CTX)

    outcome = run_tool(LookupRequest(ledger), {"request_id": CTX.step_key}, CTX)

    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.value is not None
    assert outcome.value["found"] is True
    assert outcome.value["kind"] == "transfer"
    assert outcome.value["amount"] == 250


@pytest.mark.parametrize("args", [{}, {"request_id": ""}, {"request_id": 1}])
def test_lookup_rejects_bad_request_id(tmp_path, args: dict) -> None:
    outcome = run_tool(LookupRequest(ledger_at(tmp_path)), args, CTX)

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error["kind"] == "invalid_args"


# --- 工具声明 ------------------------------------------------------------


def test_tools_declare_expected_names_and_side_effects(tmp_path) -> None:
    ledger = ledger_at(tmp_path)
    assert (GetBalance(ledger).name, GetBalance(ledger).side_effect) == (
        "get_balance",
        SideEffect.READ_ONLY,
    )
    assert (Transfer(ledger).name, Transfer(ledger).side_effect) == (
        "transfer",
        SideEffect.NON_IDEMPOTENT,
    )
    assert (SaveReceipt(ledger).name, SaveReceipt(ledger).side_effect) == (
        "save_receipt",
        SideEffect.IDEMPOTENT,
    )
    assert (LookupRequest(ledger).name, LookupRequest(ledger).side_effect) == (
        "lookup_request",
        SideEffect.READ_ONLY,
    )
