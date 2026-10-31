"""真实模型客户端的离线契约：本地 HTTP 服务，不访问外网。

覆盖 LangChain 请求形状、鉴权头、坏响应、单次请求、错误脱敏、超时，
以及与 ``ModelPolicy`` 的拼接。
"""

import contextlib
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agentrun import model_client
from agentrun.llm import ModelPolicy, ToolSpec
from agentrun.model import RunStatus, RunView
from agentrun.model_client import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    HttpModelClient,
    ModelClientError,
    client_from_env,
)
from agentrun.policy import Finish


class _Handler(BaseHTTPRequestHandler):
    """返回完整 Chat Completions 响应或指定错误，并记录收到的请求。

    正常响应包含 assistant role、choice index 等协议字段，不用旧版最小 JSON
    模拟服务推断所有不完整响应仍与 LangChain 兼容。
    """

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append(  # type: ignore[attr-defined]
            {"auth": self.headers.get("Authorization"), "body": body, "path": self.path}
        )
        mode = self.server.mode  # type: ignore[attr-defined]

        if mode == "timeout":
            time.sleep(0.4)

        status = 200
        if mode in ("http401", "http429", "http500"):
            status = int(mode.removeprefix("http"))
            # 服务错误可能回显请求正文和鉴权头；客户端异常不能把它们带出。
            raw = json.dumps(
                {"error": {"message": f"{body!r} {self.headers.get('Authorization')}"}}
            ).encode()
        elif mode == "garbage":
            raw = b"not-json"
        elif mode == "empty":
            raw = json.dumps({"choices": []}).encode()
        elif mode == "missing":
            # 有 choices、但没有 content：结构不完整，属于响应形状问题
            raw = json.dumps(
                {
                    "id": "chatcmpl-local",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "offline-model",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant"}, "finish_reason": "stop"}
                    ],
                }
            ).encode()
        else:
            content = (
                123 if mode == "badcontent" else self.server.content  # type: ignore[attr-defined]
            )
            raw = json.dumps(
                {
                    "id": "chatcmpl-local",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "offline-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        """测试里不需要访问日志。"""


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把默认配置文件指到一个不存在的路径。

    Notes:
        ``client_from_env`` 默认读项目根目录的 ``agentrun.local.toml``。开发机上这个
        文件通常存在并指向真实服务；不隔离的话，「缺变量报错」与「默认超时 30 秒」
        这两条断言会随机器而变——测试描述的是代码，不是开发者的本机配置。
    """
    monkeypatch.setattr(model_client, "DEFAULT_CONFIG_PATH", tmp_path / "absent.toml")


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    # 避免机器上的 HTTP(S)_PROXY 把 127.0.0.1 也代理走
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.seen = []
    httpd.mode = "ok"
    httpd.content = '{"finish": "完成"}'
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def client_for(server: Any, timeout: float = 2.0) -> HttpModelClient:
    host, port = server.server_address[:2]
    return HttpModelClient(
        base_url=f"http://{host}:{port}/v1",
        api_key="secret-token",
        model="offline-model",
        timeout=timeout,
    )


def test_sends_expected_request_and_returns_content(server: Any) -> None:
    reply = client_for(server).complete("你好")

    assert reply == '{"finish": "完成"}'
    seen = server.seen[0]
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["body"]["model"] == "offline-model"
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"] == [{"role": "user", "content": "你好"}]
    assert len(server.seen) == 1


@pytest.mark.parametrize("mode", ["http429", "http500", "garbage", "empty", "badcontent"])
def test_bad_responses_become_client_errors(server: Any, mode: str) -> None:
    server.mode = mode
    prompt = "private-business-prompt"

    with pytest.raises(ModelClientError) as excinfo:
        client_for(server).complete(prompt)

    rendered = "".join(traceback.format_exception(excinfo.value))
    assert "secret-token" not in rendered
    assert prompt not in rendered
    assert len(server.seen) == 1


def test_timeout_becomes_retryable_client_error(server: Any) -> None:
    server.mode = "timeout"

    with pytest.raises(ModelClientError) as excinfo:
        client_for(server, timeout=0.1).complete("你好")

    assert excinfo.value.retryable is True
    assert len(server.seen) == 1


@pytest.mark.parametrize(
    ("mode", "retryable"),
    [
        ("http401", False),
        ("http429", True),
        ("http500", True),
        ("garbage", False),
        ("empty", False),
        ("missing", True),
        ("badcontent", False),
    ],
)
def test_error_classification_decides_who_may_retry(
    server: Any, mode: str, retryable: bool
) -> None:
    """错误映射表：这次失败值不值得再试一次。

    Notes:
        401 是确定性错误，重试一百次还是 401；429 与 5xx 是瞬时故障，退一步就能过去；
        响应形状不对（非 JSON、结构缺字段）说明对端没有按协议说话，同样不该盲目重投。
        分类只能发生在客户端——只有它看得见状态码与响应形状。
    """
    server.mode = mode

    with pytest.raises(ModelClientError) as excinfo:
        client_for(server).complete("你好")

    assert excinfo.value.retryable is retryable, (
        f"{mode}: 实际 retryable={excinfo.value.retryable}，"
        f"cause={type(excinfo.value.__cause__).__name__}"
    )


def test_returns_original_plain_text_without_stripping(server: Any) -> None:
    server.content = " \n这只是模型原文，不必在客户端符合动作 JSON。\n "

    assert client_for(server).complete("你好") == server.content


@pytest.mark.parametrize("content", [None, "", " \n\t", [], [{"type": "text", "text": "你好"}]])
def test_rejects_content_that_is_not_nonblank_plain_text(server: Any, content: Any) -> None:
    server.content = content

    with pytest.raises(ModelClientError):
        client_for(server).complete("你好")

    assert len(server.seen) == 1


def test_constructs_nonstreaming_chat_model_without_retries_or_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakeChatModel:
        def __init__(self, **kwargs: Any) -> None:
            seen["options"] = kwargs

        def invoke(self, messages: Any) -> Any:
            seen["messages"] = messages
            return SimpleNamespace(content=" untouched \n")

    monkeypatch.setattr(model_client, "ChatOpenAI", FakeChatModel)
    client = HttpModelClient("https://example.invalid/v1/", "secret-token", "offline-model", 5)

    assert client.complete("private-prompt") == " untouched \n"

    options = dict(seen["options"])
    # 连接由客户端自己持有：显式传进去而不是让 LangChain 用共享缓存；
    # 调用结束后必须已经关闭，否则每次决策都漏一个 socket
    http_client = options.pop("http_client")
    assert isinstance(http_client, httpx.Client)
    assert http_client.is_closed is True
    assert options == {
        "model": "offline-model",
        "api_key": SecretStr("secret-token"),
        "base_url": "https://example.invalid/v1",
        "timeout": 5,
        "temperature": 0,
        "max_retries": 0,
        "use_responses_api": False,
        "streaming": False,
        "cache": False,
    }
    assert seen["messages"] == [("human", "private-prompt")]
    assert "secret-token" not in repr(client)


@pytest.mark.parametrize("phase", ["construct", "invoke", "content"])
def test_sdk_errors_do_not_leak_credentials_or_prompt(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    clients: list[httpx.Client] = []

    class BrokenResponse:
        @property
        def content(self) -> str:
            raise RuntimeError("secret-token private-prompt")

    class BrokenChatModel:
        def __init__(self, **kwargs: Any) -> None:
            if phase == "construct":
                raise RuntimeError("secret-token private-prompt")
            clients.append(kwargs["http_client"])

        def invoke(self, messages: Any) -> Any:
            if phase == "invoke":
                raise RuntimeError("secret-token private-prompt")
            return BrokenResponse()

    monkeypatch.setattr(model_client, "ChatOpenAI", BrokenChatModel)
    client = HttpModelClient("https://example.invalid/v1", "secret-token", "offline-model")
    prompt = "private-prompt"

    with pytest.raises(ModelClientError) as excinfo:
        client.complete(prompt)

    rendered = "".join(traceback.format_exception(excinfo.value))
    assert "secret-token" not in rendered
    assert "private-prompt" not in rendered
    # 构造阶段就失败时模型对象还没建出来，连接由 with 负责关闭；其余阶段在这里核对
    if phase == "construct":
        assert clients == []
    else:
        assert all(item.is_closed for item in clients)


def test_model_policy_accepts_http_client(server: Any) -> None:
    policy = ModelPolicy(client_for(server), [ToolSpec("get_balance", "查余额")])

    view = RunView(
        run_id="r1",
        task="转账",
        max_steps=3,
        status=RunStatus.RUNNING,
    )
    assert policy.decide(view) == Finish("完成")


def test_client_from_env_reads_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "https://example.invalid/v1")
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.setenv(ENV_MODEL, "m")
    monkeypatch.setenv("AGENTRUN_TIMEOUT", "5")

    client = client_from_env()

    assert client.base_url == "https://example.invalid/v1"
    assert client.api_key == "k"
    assert client.model == "m"
    assert client.timeout == 5.0


def test_client_from_env_reports_missing_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.setenv(ENV_MODEL, "m")

    with pytest.raises(ModelClientError, match="AGENTRUN_BASE_URL"):
        client_from_env()


def test_client_from_env_defaults_timeout_and_reloads_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "https://example.invalid/v1")
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.setenv(ENV_MODEL, "first-model")
    monkeypatch.delenv("AGENTRUN_TIMEOUT", raising=False)

    assert client_from_env().timeout == 30.0
    monkeypatch.setenv(ENV_MODEL, "second-model")
    assert client_from_env().model == "second-model"


@pytest.mark.parametrize("timeout", ["invalid", "0", "-1", "nan", "inf"])
def test_client_from_env_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch, timeout: str
) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "https://example.invalid/v1")
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.setenv(ENV_MODEL, "m")
    monkeypatch.setenv("AGENTRUN_TIMEOUT", timeout)

    with pytest.raises(ModelClientError, match="AGENTRUN_TIMEOUT"):
        client_from_env()
