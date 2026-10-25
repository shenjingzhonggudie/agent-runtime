"""CLI 契约测试：子命令退出码、脚本解析、状态输出。"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentrun.cli import load_actions, main
from agentrun.policy import Finish, ToolCall


def write_script(path: Path, items: list[dict[str, Any]]) -> Path:
    """把脚本写成 JSON 文件并返回路径。"""
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def base_args(tmp_path: Path) -> list[str]:
    """公共参数：把数据库与账本都放进临时目录。"""
    return ["--db", str(tmp_path / "events.db"), "--ledger", str(tmp_path / "ledger")]


def db_only(tmp_path: Path) -> list[str]:
    """只给数据库参数；``status`` 不接账本。"""
    return ["--db", str(tmp_path / "events.db")]


def test_load_actions_parses_tool_and_finish(tmp_path: Path) -> None:
    script = write_script(
        tmp_path / "s.json",
        [{"tool": "get_balance", "args": {"owner": "alice"}}, {"finish": "完成"}],
    )

    actions = load_actions(script)

    assert actions == [ToolCall("get_balance", {"owner": "alice"}), Finish("完成")]


def test_load_actions_defaults_args_to_empty(tmp_path: Path) -> None:
    script = write_script(tmp_path / "s.json", [{"tool": "get_balance"}])
    assert load_actions(script) == [ToolCall("get_balance", {})]


def test_load_actions_rejects_unknown_item(tmp_path: Path) -> None:
    script = write_script(tmp_path / "s.json", [{"nope": 1}])
    with pytest.raises(ValueError, match="tool 或 finish"):
        load_actions(script)


def test_load_actions_rejects_non_list(tmp_path: Path) -> None:
    script = tmp_path / "s.json"
    script.write_text('{"tool": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="数组"):
        load_actions(script)


def test_load_actions_rejects_broken_json(tmp_path: Path) -> None:
    script = tmp_path / "s.json"
    script.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="读不出脚本"):
        load_actions(script)


def test_run_succeeds_and_status_shows_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = write_script(
        tmp_path / "s.json",
        [{"tool": "get_balance", "args": {"owner": "alice"}}, {"finish": "余额充足"}],
    )

    code = main(["run", "--run-id", "r1", "--script", str(script), *base_args(tmp_path)])
    assert code == 0
    assert "SUCCEEDED" in capsys.readouterr().out

    code = main(["status", "--run-id", "r1", *db_only(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "RUN_STARTED" in out
    assert "RUN_SUCCEEDED" in out
    assert "余额充足" in out


def test_run_with_unknown_tool_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = write_script(tmp_path / "s.json", [{"tool": "nope"}])

    code = main(["run", "--run-id", "r1", "--script", str(script), *base_args(tmp_path)])

    assert code == 1
    assert "FAILED" in capsys.readouterr().out


def test_status_of_unknown_run_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["status", "--run-id", "missing", *db_only(tmp_path)])
    assert code == 2
    assert "没有这个任务" in capsys.readouterr().out


def test_resume_of_unknown_run_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = write_script(tmp_path / "s.json", [{"finish": "完成"}])

    code = main(["resume", "--run-id", "missing", "--script", str(script), *base_args(tmp_path)])

    assert code == 2
    assert "无法续跑" in capsys.readouterr().out


def test_reconcile_without_pending_step_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = write_script(
        tmp_path / "s.json",
        [{"tool": "get_balance", "args": {"owner": "alice"}}, {"finish": "余额充足"}],
    )
    main(["run", "--run-id", "r1", "--script", str(script), *base_args(tmp_path)])
    capsys.readouterr()

    code = main(["reconcile", "--run-id", "r1", *base_args(tmp_path)])

    assert code == 2
    assert "无需对账" in capsys.readouterr().out


def test_bad_script_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = tmp_path / "s.json"
    script.write_text("{not json", encoding="utf-8")

    code = main(["run", "--run-id", "r1", "--script", str(script), *base_args(tmp_path)])

    assert code == 2
    assert "脚本错误" in capsys.readouterr().out
