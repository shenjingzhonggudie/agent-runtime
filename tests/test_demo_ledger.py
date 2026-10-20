"""模拟账本的黑盒测试。

只使用公开 API，不检查内部文件格式：怎么存由实现者决定。
每个测试都自己开账本，互不共享目录。
"""

import json

import pytest

from agentrun.demo import Ledger, LedgerCorrupted, LedgerRejected

ALICE = "alice"
BOB = "bob"
START = {ALICE: 1000, BOB: 0}


def open_ledger(root, initial: dict[str, int] | None = None) -> Ledger:
    return Ledger(root, initial=START if initial is None else initial)


def test_initial_balances(tmp_path) -> None:
    ledger = open_ledger(tmp_path / "world")
    assert ledger.balance(ALICE) == 1000
    assert ledger.balance(BOB) == 0


def test_transfer_moves_money(tmp_path) -> None:
    ledger = open_ledger(tmp_path / "world")
    ledger.transfer(ALICE, BOB, 250, "req-1")
    assert ledger.balance(ALICE) == 750
    assert ledger.balance(BOB) == 250


def test_balances_survive_reopen_without_reset(tmp_path) -> None:
    root = tmp_path / "world"
    open_ledger(root).transfer(ALICE, BOB, 250, "req-1")

    # 故意传一个不同的初始值：已存在的账本必须忽略它，不能重置
    reopened = open_ledger(root, initial={ALICE: 999_999, BOB: 999_999})
    assert reopened.balance(ALICE) == 750
    assert reopened.balance(BOB) == 250


@pytest.mark.parametrize("amount", [0, -1])
def test_rejects_non_positive_amount(tmp_path, amount: int) -> None:
    ledger = open_ledger(tmp_path / "world")
    with pytest.raises(LedgerRejected):
        ledger.transfer(ALICE, BOB, amount, "req-bad")
    assert ledger.balance(ALICE) == 1000
    assert ledger.lookup("req-bad") is None


def test_rejects_overdraft(tmp_path) -> None:
    ledger = open_ledger(tmp_path / "world")
    with pytest.raises(LedgerRejected):
        ledger.transfer(ALICE, BOB, 1001, "req-big")
    assert ledger.balance(ALICE) == 1000
    assert ledger.lookup("req-big") is None


def test_rejects_when_reserve_would_break(tmp_path) -> None:
    # 500 - 250 = 250，低于 300 的储备线
    ledger = open_ledger(tmp_path / "world", initial={ALICE: 500, BOB: 0})
    with pytest.raises(LedgerRejected):
        ledger.transfer(ALICE, BOB, 250, "req-reserve")
    assert ledger.balance(ALICE) == 500
    assert ledger.lookup("req-reserve") is None


def test_same_request_id_charges_twice(tmp_path) -> None:
    # 非幂等：即使 request_id 相同，账本也不去重
    ledger = open_ledger(tmp_path / "world")
    ledger.transfer(ALICE, BOB, 250, "req-dup")
    ledger.transfer(ALICE, BOB, 250, "req-dup")
    assert ledger.balance(ALICE) == 500
    assert ledger.balance(BOB) == 500


def test_receipt_first_write_wins(tmp_path) -> None:
    ledger = open_ledger(tmp_path / "world")
    ledger.save_receipt("rec-1", "first")
    ledger.save_receipt("rec-1", "second")

    record = ledger.lookup("rec-1")
    assert record is not None
    assert record.kind == "receipt"
    assert record.text == "first"


def test_lookup_unknown_returns_none(tmp_path) -> None:
    assert open_ledger(tmp_path / "world").lookup("never-used") is None


def test_lookup_finds_transfer(tmp_path) -> None:
    ledger = open_ledger(tmp_path / "world")
    ledger.transfer(ALICE, BOB, 250, "req-1")

    record = ledger.lookup("req-1")
    assert record is not None
    assert record.kind == "transfer"
    assert (record.src, record.dst, record.amount) == (ALICE, BOB, 250)


def test_corrupted_json_is_not_treated_as_empty(tmp_path) -> None:
    root = tmp_path / "world"
    root.mkdir()
    (root / "ledger.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(LedgerCorrupted):
        Ledger(root).balance(ALICE)


def test_non_integer_balance_is_rejected(tmp_path) -> None:
    root = tmp_path / "world"
    root.mkdir()
    (root / "ledger.json").write_text(
        json.dumps({"balances": {ALICE: "1000"}, "records": []}), encoding="utf-8"
    )

    with pytest.raises(LedgerCorrupted):
        Ledger(root).balance(ALICE)


def test_missing_state_keys_are_rejected(tmp_path) -> None:
    root = tmp_path / "world"
    root.mkdir()
    (root / "ledger.json").write_text(json.dumps({"balances": {}}), encoding="utf-8")

    with pytest.raises(LedgerCorrupted):
        Ledger(root).balance(ALICE)


def test_rejected_then_accepted_uses_new_request_id(tmp_path) -> None:
    ledger = open_ledger(tmp_path / "world", initial={ALICE: 500, BOB: 0})
    with pytest.raises(LedgerRejected):
        ledger.transfer(ALICE, BOB, 250, "req-a")

    ledger.transfer(ALICE, BOB, 100, "req-b")
    assert ledger.balance(ALICE) == 400
    assert ledger.lookup("req-a") is None
    assert ledger.lookup("req-b") is not None
