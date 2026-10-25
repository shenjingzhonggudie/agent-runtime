"""真实模型客户端的离线契约：本地 HTTP 服务，不访问外网。

覆盖请求形状、鉴权头、四种坏响应、超时，以及与 ``ModelPolicy`` 的拼接。
"""

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

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
    """按 ``server.mode`` 返回不同响应，并记录收到的请求。"""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append(  # type: ignore[attr-defined]
            {"auth": self.headers.get("Authorization"), "body": body, "path": self.path}
        )
        mode = self.server.mode  # type: ignore[attr-defined]

        if mode == "http500":
            self.send_response(500)
            self.end_headers()
            return
        if mode == "timeout":
            time.sleep(0.4)

        if mode == "garbage":
            raw = b"not-json"
        elif mode == "empty":
            raw = json.dumps({"choices": []}).encode()
        elif mode == "badcontent":
            raw = json.dumps({"choices": [{"message": {"content": 123}}]}).encode()
        else:
            raw = json.dumps({"choices": [{"message": {"content": '{"finish": "完成"}'}}]}).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError):
            self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        """测试里不需要访问日志。"""


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    # 避免机器上的 HTTP(S)_PROXY 把 127.0.0.1 也代理走
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.seen = []
    httpd.mode = "ok"
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
    assert seen["body"]["messages"] == [{"role": "user", "content": "你好"}]


@pytest.mark.parametrize("mode", ["http500", "garbage", "empty", "badcontent"])
def test_bad_responses_become_client_errors(server: Any, mode: str) -> None:
    server.mode = mode

    with pytest.raises(ModelClientError) as excinfo:
        client_for(server).complete("你好")

    assert "secret-token" not in str(excinfo.value)


def test_timeout_becomes_client_error(server: Any) -> None:
    server.mode = "timeout"

    with pytest.raises(ModelClientError):
        client_for(server, timeout=0.1).complete("你好")


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
    assert client.timeout == 5.0


def test_client_from_env_reports_missing_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.setenv(ENV_MODEL, "m")

    with pytest.raises(ModelClientError, match="AGENTRUN_BASE_URL"):
        client_from_env()
