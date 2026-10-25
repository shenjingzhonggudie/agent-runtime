"""真实模型客户端：标准库 HTTP，最小 OpenAI 兼容协议。

它只做一件事：把一段提示发出去，把文本取回来。不做重试、不解析动作、不写日志、不
打印密钥。任何 HTTP / 网络 / 结构问题都归为 ``ModelClientError``，由 ``ModelPolicy``
转成 ``PolicyError``——对引擎而言只是「这次没拿到决策」。

**未联真实服务验证**：仓库与 CI 都没有模型服务凭据，测试只覆盖本地 HTTP 服务与协议
解析。接真实服务前不要假定兼容性。

依赖方向：
    model_client → 无（只用标准库）
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

ENV_BASE_URL = "AGENTRUN_BASE_URL"
ENV_API_KEY = "AGENTRUN_API_KEY"
ENV_MODEL = "AGENTRUN_MODEL"


class ModelClientError(Exception):
    """没有拿到可用的模型输出。

    抛出它表示这次调用失败：网络、鉴权、超时、返回结构不符。它不表示模型说了什么，
    也不携带提示或密钥——错误信息会被写进日志，不能把凭据带进去。
    """


@dataclass(frozen=True, slots=True)
class HttpModelClient:
    """OpenAI 兼容的 ``/chat/completions`` 客户端。

    Attributes:
        base_url: 服务根地址，例如 ``https://api.example.com/v1``；不要再带路径。
        api_key: 鉴权令牌，放在 ``Authorization: Bearer`` 头里。
        model: 模型名，原样写进请求体。
        timeout: 单次调用的超时秒数；超时归为 ``ModelClientError``。

    Notes:
        温度固定为 0：续跑要求「同一提示倾向同一决策」，虽然服务端不保证。
        不重试：重试属于策略层或更外层的决定，客户端只报告这一次的失败。
    """

    base_url: str
    api_key: str
    model: str
    timeout: float = 30.0

    def complete(self, prompt: str) -> str:
        """发送提示并返回模型文本。

        Args:
            prompt: 由 ``build_prompt`` 渲染的完整提示。

        Returns:
            str: ``choices[0].message.content`` 的原文，不做修剪。

        Raises:
            ModelClientError: 网络失败、HTTP 非 2xx、响应不是 JSON、或结构不含
                非空字符串内容。

        Notes:
            错误信息里不出现 ``api_key`` 与 ``prompt``：前者是凭据，后者可能含业务
            数据；两者都会进入事件日志。
        """
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise ModelClientError(f"模型服务返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelClientError(f"模型服务不可用：{exc}") from exc

        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ModelClientError("模型服务返回的不是 JSON") from exc

        content = _extract_content(data)
        if not isinstance(content, str) or not content.strip():
            raise ModelClientError("模型服务返回的 content 不是非空字符串")
        return content


def _extract_content(data: Any) -> Any:
    """从响应里取出 ``choices[0].message.content``；结构不符返回 ``None``。"""
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    return message.get("content")


def client_from_env() -> HttpModelClient:
    """从环境变量构造客户端。

    Returns:
        HttpModelClient: 使用 ``AGENTRUN_BASE_URL`` / ``AGENTRUN_API_KEY`` /
        ``AGENTRUN_MODEL``；超时取 ``AGENTRUN_TIMEOUT``，默认 30 秒。

    Raises:
        ModelClientError: 三个必需变量有缺失或为空。

    Notes:
        每次调用重新读取环境变量。配置错误在构造时暴露，而不是等到第一次决策。
    """
    base_url = os.environ.get(ENV_BASE_URL, "").strip()
    api_key = os.environ.get(ENV_API_KEY, "").strip()
    model = os.environ.get(ENV_MODEL, "").strip()
    missing = [
        name
        for name, value in ((ENV_BASE_URL, base_url), (ENV_API_KEY, api_key), (ENV_MODEL, model))
        if not value
    ]
    if missing:
        raise ModelClientError(f"缺少环境变量：{', '.join(missing)}")

    raw_timeout = os.environ.get("AGENTRUN_TIMEOUT", "30").strip()
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ModelClientError(f"AGENTRUN_TIMEOUT 不是数字：{raw_timeout!r}") from exc
    return HttpModelClient(base_url=base_url, api_key=api_key, model=model, timeout=timeout)
