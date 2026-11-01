"""产品入口：``ask`` 与 ``ledger``（19I）。

验收是一条命令链能从空目录跑到「余额真的变了」，全程不需要看 ``scripts/``。所以这里
从 ``ledger init`` 开始，一路断言到转账后的余额，中间不手工装配任何引擎组件。
"""

import json
from pathlib import Path

import pytest

from agentrun import cli
from agentrun.model_client import ModelClientError


class FakeClient:
    """按脚本顺序返回固定文本，不联网。

    Attributes:
        prompts: 每次 ``complete`` 收到的提示，用来确认「同一视图渲染同一份提示」。
    """

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        """返回下一条预置回复；用完之后抛错，避免测试静默多问几轮。"""
        self.prompts.append(prompt)
        if not self._replies:
            raise ModelClientError("脚本用完了")
        return self._replies.pop(0)


def use_client(monkeypatch: pytest.MonkeyPatch, *replies: str) -> FakeClient:
    """把 CLI 的客户端工厂换成脚本替身。"""
    client = FakeClient(list(replies))
    monkeypatch.setattr(cli, "client_from_env", lambda: client)
    return client


def state_args(tmp_path: Path) -> list[str]:
    """把事件日志与账本都放进临时目录。"""
    return ["--db", str(tmp_path / "events.db"), "--ledger", str(tmp_path / "ledger")]


def init_ledger(tmp_path: Path) -> None:
    """先把账本建好。"""
    code = cli.main(["ledger", "init", "--A=1000", "--B=0", "--ledger", str(tmp_path / "ledger")])
    assert code == 0


# --- 账本 -----------------------------------------------------------------


def test_ledger_init_creates_accounts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    init_ledger(tmp_path)

    printed = capsys.readouterr().out
    assert "A: 1000" in printed
    assert "B: 0" in printed


def test_ledger_init_requires_at_least_one_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["ledger", "init", "--ledger", str(tmp_path / "ledger")])

    assert code == 2
    assert "至少给一个初始余额" in capsys.readouterr().out


def test_ledger_balance_can_read_one_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init_ledger(tmp_path)
    capsys.readouterr()

    code = cli.main(["ledger", "balance", "--account", "A", "--ledger", str(tmp_path / "ledger")])

    assert code == 0
    assert "A: 1000" in capsys.readouterr().out


def test_ledger_balance_on_empty_ledger_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """空账本要有一句明确的话，否则看起来像命令失败了。"""
    code = cli.main(["ledger", "balance", "--ledger", str(tmp_path / "fresh")])

    assert code == 0
    assert "还没有账户" in capsys.readouterr().out


# --- ask ------------------------------------------------------------------


def test_ask_runs_from_empty_directory_to_changed_balance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    init_ledger(tmp_path)
    capsys.readouterr()
    use_client(
        monkeypatch,
        json.dumps({"tool": "transfer", "args": {"src": "A", "dst": "B", "amount": 250}}),
        json.dumps({"finish": "已转账 250"}),
    )

    code = cli.main(["ask", "从 A 转 250 到 B", *state_args(tmp_path)])

    assert code == 0
    printed = capsys.readouterr().out
    assert "已转账 250" in printed
    assert "A: 750" in printed
    assert "B: 250" in printed


def test_ask_refuses_a_second_run_with_the_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_client(monkeypatch, json.dumps({"finish": "完成"}))
    argv = ["ask", "查余额", "--run-id", "r1", *state_args(tmp_path)]

    assert cli.main(argv) == 0
    capsys.readouterr()

    assert cli.main(argv) == 2
    assert "任务已存在" in capsys.readouterr().out


def test_ask_points_at_the_next_command_when_it_does_not_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """没跑成时，输出要直接给出下一步该敲什么。"""
    use_client(monkeypatch, json.dumps({"tool": "nope", "args": {}}))

    code = cli.main(["ask", "查余额", "--run-id", "r2", *state_args(tmp_path)])

    assert code == 1
    printed = capsys.readouterr().out
    assert "status --run-id r2" in printed
    assert "resume --run-id r2" in printed
