"""CLI 契约测试：子命令退出码、脚本解析、状态输出。"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentrun import cli
from agentrun.cli import load_actions, main
from agentrun.events import EventType, make_step_key
from agentrun.journal import SqliteEventLog
from agentrun.model import replay
from agentrun.model_client import ModelClientError
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
    assert not (tmp_path / "events.db").exists()
    assert not (tmp_path / "ledger").exists()


def test_trace_prints_timeline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = write_script(
        tmp_path / "s.json",
        [{"tool": "get_balance", "args": {"owner": "alice"}}, {"finish": "余额充足"}],
    )
    main(["run", "--run-id", "r1", "--script", str(script), *base_args(tmp_path)])
    capsys.readouterr()

    code = main(["trace", "--run-id", "r1", *db_only(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "RUN_STARTED" in out
    assert "决定第 1 步" in out
    assert "任务成功" in out


def test_trace_export_writes_jsonl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = write_script(
        tmp_path / "s.json",
        [{"tool": "get_balance", "args": {"owner": "alice"}}, {"finish": "余额充足"}],
    )
    main(["run", "--run-id", "r1", "--script", str(script), *base_args(tmp_path)])
    capsys.readouterr()
    target = tmp_path / "trace.jsonl"

    code = main(["trace", "--run-id", "r1", "--export", str(target), *db_only(tmp_path)])

    assert code == 0
    assert len(target.read_text(encoding="utf-8").splitlines()) == 5
    assert "已导出 5 条" in capsys.readouterr().out


def test_trace_of_unknown_run_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["trace", "--run-id", "missing", *db_only(tmp_path)])

    assert code == 2
    assert "没有这个任务" in capsys.readouterr().out


def seed_crash_window(tmp_path: Path) -> None:
    """直接写日志，造一个「第 1 步已开始、没有结果」的现场。"""
    from agentrun.events import EventType, make_step_key
    from agentrun.journal import SqliteEventLog

    log = SqliteEventLog(tmp_path / "events.db")
    run_id = "r1"
    log.append(run_id, EventType.RUN_STARTED, {"task": "转账", "max_steps": 5})
    log.append(
        run_id,
        EventType.STEP_DECIDED,
        {
            "step": 1,
            "tool": "transfer",
            "args": {"src": "alice", "dst": "bob", "amount": 250},
            "step_key": make_step_key(run_id, 1),
        },
    )
    log.append(run_id, EventType.STEP_STARTED, {"step": 1, "attempt": 1})


def test_reconcile_miss_then_resolve_then_resume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_crash_window(tmp_path)

    code = main(["reconcile", "--run-id", "r1", *base_args(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "已提交人工复核" in out
    assert "等待复核" in out

    code = main(
        [
            "resolve",
            "--run-id",
            "r1",
            "--decision",
            "applied",
            "--note",
            "账本记录与 step_key 一致",
            "--result",
            '{"found": true, "amount": 250}',
            *db_only(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "applied" in out

    script = write_script(tmp_path / "s.json", [{"finish": "占位"}, {"finish": "复核后结束"}])
    code = main(["resume", "--run-id", "r1", "--script", str(script), *base_args(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "复核后结束" in out


def test_resolve_without_review_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = write_script(
        tmp_path / "s.json",
        [{"tool": "get_balance", "args": {"owner": "alice"}}, {"finish": "余额充足"}],
    )
    main(["run", "--run-id", "r1", "--script", str(script), *base_args(tmp_path)])
    capsys.readouterr()

    code = main(
        [
            "resolve",
            "--run-id",
            "r1",
            "--decision",
            "abort",
            "--note",
            "无需复核",
            *db_only(tmp_path),
        ]
    )

    assert code == 2
    assert "无法复核" in capsys.readouterr().out


class FakeModelClient:
    """只返回预置文本并记录提示，不访问模型服务。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses[len(self.prompts) - 1]


def test_model_run_queries_registered_tool_then_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeModelClient(
        ['{"tool": "get_balance", "args": {"owner": "alice"}}', '{"finish": "余额为 0"}']
    )
    monkeypatch.setattr(cli, "client_from_env", lambda: client)

    code = main(
        [
            "run",
            "--run-id",
            "model-run",
            "--policy",
            "model",
            "--task",
            "查询 alice 余额并报告",
            *base_args(tmp_path),
        ]
    )

    assert code == 0
    assert "余额为 0" in capsys.readouterr().out
    events = SqliteEventLog(tmp_path / "events.db").read("model-run")
    view = replay(events)
    assert view.steps[0].tool == "get_balance"
    assert view.steps[0].result == {"owner": "alice", "balance": 0}
    assert len(client.prompts) == 2
    assert "查询 alice 余额并报告" in client.prompts[0]
    assert '"balance": 0' in client.prompts[1]
    for name in ("get_balance", "transfer", "save_receipt", "lookup_request"):
        assert name in client.prompts[0]
    for parameter in ("owner: str", "src: str", "dst: str", "amount: int", "text: str"):
        assert parameter in client.prompts[0]
    assert "request_id: str" in client.prompts[0]
    assert "非幂等" in client.prompts[0]


def test_model_resume_restores_task_and_history_without_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    log.append("r1", EventType.RUN_STARTED, {"task": "报告已有查询结果", "max_steps": 5})
    log.append(
        "r1",
        EventType.STEP_DECIDED,
        {
            "step": 1,
            "tool": "get_balance",
            "args": {"owner": "alice"},
            "step_key": make_step_key("r1", 1),
        },
    )
    log.append("r1", EventType.STEP_STARTED, {"step": 1, "attempt": 1})
    log.append(
        "r1",
        EventType.STEP_COMPLETED,
        {"step": 1, "result": {"owner": "alice", "balance": 123}},
    )
    client = FakeModelClient(['{"finish": "已有余额为 123"}'])
    monkeypatch.setattr(cli, "client_from_env", lambda: client)

    code = main(["resume", "--run-id", "r1", "--policy", "model", *base_args(tmp_path)])

    assert code == 0
    assert "已有余额为 123" in capsys.readouterr().out
    assert len(client.prompts) == 1
    assert "报告已有查询结果" in client.prompts[0]
    assert '"balance": 123' in client.prompts[0]
    assert len(replay(log.read("r1")).steps) == 1


def test_model_resume_executes_persisted_decision_before_asking_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    log.append("r1", EventType.RUN_STARTED, {"task": "完成保存的余额查询", "max_steps": 5})
    log.append(
        "r1",
        EventType.STEP_DECIDED,
        {
            "step": 1,
            "tool": "get_balance",
            "args": {"owner": "saved-owner"},
            "step_key": make_step_key("r1", 1),
        },
    )
    client = FakeModelClient(['{"finish": "保存的查询已完成"}'])
    monkeypatch.setattr(cli, "client_from_env", lambda: client)

    code = main(["resume", "--run-id", "r1", "--policy", "model", *base_args(tmp_path)])

    assert code == 0
    assert "保存的查询已完成" in capsys.readouterr().out
    events = log.read("r1")
    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.STEP_DECIDED,
        EventType.STEP_STARTED,
        EventType.STEP_COMPLETED,
        EventType.RUN_SUCCEEDED,
    ]
    step = replay(events).steps[0]
    assert step.args == {"owner": "saved-owner"}
    assert step.result == {"owner": "saved-owner", "balance": 0}
    assert step.attempts == 1
    assert len(client.prompts) == 1
    assert '"owner": "saved-owner"' in client.prompts[0]
    assert 'SUCCEEDED {"balance": 0' in client.prompts[0]


@pytest.mark.parametrize("window", ["started", "uncertain", "review"])
def test_model_resume_does_not_decide_while_waiting_for_external_facts(
    window: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_crash_window(tmp_path)
    log = SqliteEventLog(tmp_path / "events.db")
    if window == "uncertain":
        log.append("r1", EventType.STEP_UNCERTAIN, {"step": 1, "reason": "响应丢失"})
    elif window == "review":
        log.append("r1", EventType.REVIEW_REQUESTED, {"step": 1, "reason": "等待人工确认"})
    before = log.read("r1")
    client = FakeModelClient([])
    monkeypatch.setattr(cli, "client_from_env", lambda: client)

    code = main(["resume", "--run-id", "r1", "--policy", "model", *base_args(tmp_path)])

    assert code == 1
    expected = "WAITING_REVIEW" if window == "review" else "UNCERTAIN"
    assert f"停止原因：{expected}" in capsys.readouterr().out
    assert client.prompts == []
    assert log.read("r1") == before


def test_model_missing_configuration_exits_2_without_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("AGENTRUN_BASE_URL", "AGENTRUN_API_KEY", "AGENTRUN_MODEL", "AGENTRUN_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)

    code = main(
        [
            "run",
            "--run-id",
            "r1",
            "--policy",
            "model",
            "--task",
            "查询余额",
            *base_args(tmp_path),
        ]
    )

    assert code == 2
    assert "模型配置错误" in capsys.readouterr().out
    assert not (tmp_path / "events.db").exists()
    assert not (tmp_path / "ledger").exists()


def test_model_configuration_error_does_not_echo_sensitive_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable() -> None:
        raise ModelClientError("sk-private-key sensitive-prompt")

    monkeypatch.setattr(cli, "client_from_env", unavailable)

    code = main(
        [
            "run",
            "--run-id",
            "r1",
            "--policy",
            "model",
            "--task",
            "查询余额",
            *base_args(tmp_path),
        ]
    )

    out = capsys.readouterr().out
    assert code == 2
    assert "模型配置错误" in out
    assert "sk-private-key" not in out
    assert "sensitive-prompt" not in out
    assert not (tmp_path / "events.db").exists()
    assert not (tmp_path / "ledger").exists()


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["run"], "script 模式必须提供 --script"),
        (["resume"], "script 模式必须提供 --script"),
        (["run", "--policy", "model"], "非空 --task"),
        (["run", "--policy", "model", "--task", "  "], "非空 --task"),
        (["run", "--policy", "model", "--script", "unused.json"], "不能同时提供 --script"),
        (["resume", "--policy", "model", "--script", "unused.json"], "不能同时提供 --script"),
    ],
)
def test_policy_arguments_are_rejected_before_creating_state(
    arguments: list[str],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_client() -> None:
        raise AssertionError("参数无效时不应读取模型配置")

    monkeypatch.setattr(cli, "client_from_env", unexpected_client)

    code = main([*arguments, "--run-id", "r1", *base_args(tmp_path)])

    assert code == 2
    assert message in capsys.readouterr().out
    assert not (tmp_path / "events.db").exists()
    assert not (tmp_path / "ledger").exists()
