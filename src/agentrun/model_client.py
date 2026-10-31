"""真实模型客户端：通过 LangChain 调用最小 OpenAI 兼容协议。

它只做一件事：把一段提示发出去，把文本取回来。不做重试、不解析动作、不写日志、不
打印密钥。任何 HTTP / 网络 / 结构问题都归为 ``ModelClientError``，由 ``ModelPolicy``
转成 ``PolicyError``——对引擎而言只是「这次没拿到决策」。

自动测试使用本地 HTTP 服务验证协议与错误处理；真实服务联调由演示入口显式执行，
不在测试期间读取本机凭据或发起计费请求。

依赖方向：
    model_client → langchain_openai.ChatOpenAI
"""

import math
import os
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

ENV_BASE_URL = "AGENTRUN_BASE_URL"
ENV_API_KEY = "AGENTRUN_API_KEY"
ENV_MODEL = "AGENTRUN_MODEL"
ENV_CONFIG = "AGENTRUN_CONFIG"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "agentrun.local.toml"


class ModelClientError(Exception):
    """没有拿到可用的模型输出。

    抛出它表示这次调用失败：网络、鉴权、超时、返回结构不符。它不表示模型说了什么，
    也不携带提示或密钥——错误信息会被写进日志，不能把凭据带进去。

    Args:
        message: 失败原因。会进入事件日志与终端输出，不能含凭据、提示或响应正文。
        retryable: 这次失败是否值得再试一次。按异常种类与状态码判定：超时、连接
            失败、限流与 5xx 属于瞬时故障；401 / 403 / 404 与参数错误属于确定性
            错误，重试只会得到同样的结果。默认 ``False``。

    Attributes:
        retryable: 与构造参数一致。``ModelPolicy`` 原样透传进 ``PolicyError``，
            引擎据此决定是暂停还是判死。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


RETRYABLE_STATUS: frozenset[int] = frozenset({408, 409, 425, 429})
"""值得重试的 HTTP 状态码：请求超时、冲突、过早、限流。

5xx 不在这里逐个列举，由 ``_is_retryable_error`` 按区间判定——枚举容易漏。
"""


def _status_code(error: BaseException) -> int | None:
    """从不认识的第三方异常里读出 HTTP 状态码。

    Args:
        error: 客户端或 SDK 抛出的异常；类型不受控制。

    Returns:
        int | None: 状态码；异常没有这个属性、或属性不是整数时为 None。

    Notes:
        只读属性，不 ``str(error)``：SDK 的异常文本可能带着服务端回显的请求正文、
        密钥或响应原文，拼进日志就再也擦不掉。读不到一律当确定性失败——判错方向
        也要保守，不拿重试去赌一个可能永远不恢复的端点。
    """
    for name in ("status_code", "status", "http_status"):
        value = getattr(error, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _causes(error: BaseException) -> Iterator[BaseException]:
    """沿异常链逐层给出异常，遇到环就停。

    Args:
        error: 最外层的异常。

    Yields:
        BaseException: 自身，以及它的 ``__cause__`` / ``__context__``，由外向内。

    Notes:
        SDK 会把底层网络异常包一层再抛出（``APITimeoutError`` 之类），只看最外层会
        把超时误判成「未知错误」。链上的 ``__context__`` 也要走：本模块用 ``from None``
        抛错是为了不把原文带进日志，但那不会斩断 ``__context__``，分类仍然需要它。
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_retryable_error(error: BaseException) -> bool:
    """判断这次调用失败是否值得重试。

    Args:
        error: 客户端或 SDK 抛出的异常。

    Returns:
        bool: 网络层故障、超时、限流与 5xx 为 True；其余为 False。

    Notes:
        判据只有两条：异常种类与状态码，不看异常文本。文本不稳定，分类必须由可枚举
        的事实决定，否则同一类故障会在不同机器上得到不同结论。

        沿异常链找证据：LangChain 与 openai SDK 抛的是自己的类型，真正的
        ``httpx.TimeoutException`` 藏在 ``__cause__`` 里。只看最外层，超时会被判成
        不可重试——这类误判不会报错，只会让本该自愈的一次抖动变成任务失败。
    """
    for item in _causes(error):
        if isinstance(item, (httpx.TransportError, TimeoutError, ConnectionError)):
            return True
        status = _status_code(item)
        if status is not None:
            return status >= 500 or status in RETRYABLE_STATUS
    return False


@dataclass(frozen=True, slots=True)
class HttpModelClient:
    """OpenAI 兼容的 ``/chat/completions`` 客户端。

    Attributes:
        base_url: 服务根地址，例如 ``https://api.example.com/v1``；不要再带路径。
        api_key: 鉴权令牌，放在 ``Authorization: Bearer`` 头里。
        model: 模型名，原样写进请求体。
        timeout: 单次调用的超时秒数；超时归为 ``ModelClientError``。
        reasoning_effort: 服务支持的推理设置；未配置时不发送该参数。

    Notes:
        配置温度为 0；SDK 或服务可能对部分模型限制该参数，不能保证回答确定。
        不重试：重试属于策略层或更外层的决定，客户端只报告这一次的失败。
        每次调用独立构造模型与同步 HTTP 客户端，请求结束或异常时关闭自有连接。
    """

    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float = 30.0
    reasoning_effort: str | None = None

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
        try:
            options: dict[str, Any] = {}
            if self.reasoning_effort is not None:
                options["reasoning_effort"] = self.reasoning_effort
            # 显式持有本次请求的连接，避免关闭 LangChain 缓存的共享客户端。
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as http_client:
                model = ChatOpenAI(
                    model=self.model,
                    api_key=SecretStr(self.api_key),
                    base_url=self.base_url.rstrip("/"),
                    timeout=self.timeout,
                    temperature=0,
                    max_retries=0,
                    use_responses_api=False,
                    streaming=False,
                    cache=False,
                    http_client=http_client,
                    **options,
                )
                content = model.invoke([("human", prompt)]).content
        except Exception as exc:
            # SDK 异常可能带有服务端回显的请求正文、密钥或响应原文。
            # 不拼接异常文本，也不保留会被 CLI 打印出来的异常链。
            raise ModelClientError(
                "模型调用失败：请检查连接、服务响应和客户端配置",
                retryable=_is_retryable_error(exc),
            ) from None

        if not isinstance(content, str) or not content.strip():
            # 结构与约定不符，但这是服务端偶发会自愈的一类失败：空回复重来一次
            # 通常就正常了，记成确定性错误会让一次抖动变成任务的终局。
            raise ModelClientError("模型服务返回的 content 不是非空字符串", retryable=True)
        return content


def _local_configuration() -> dict[str, Any]:
    """读取本地配置；错误不包含文件正文、凭据或解析器的上下文。"""
    selected = os.environ.get(ENV_CONFIG, "").strip()
    try:
        path = Path(selected).expanduser() if selected else DEFAULT_CONFIG_PATH
        with path.open("rb") as stream:
            configuration = tomllib.load(stream)
    except FileNotFoundError:
        if not selected:
            return {}
        raise ModelClientError("AGENTRUN_CONFIG 指定的配置文件不存在") from None
    except OSError, ValueError, RuntimeError:
        raise ModelClientError("无法读取模型配置：请检查文件权限和 TOML 格式") from None

    model = configuration.get("model", {})
    if not isinstance(model, dict):
        raise ModelClientError("模型配置的 [model] 必须是一个表")
    return model


def client_from_env() -> HttpModelClient:
    """从本地配置和环境变量构造客户端，逐字段优先使用环境变量。

    Returns:
        HttpModelClient: 默认读取项目根目录 ``agentrun.local.toml`` 的 ``[model]``；
        ``AGENTRUN_CONFIG`` 可指定其他文件。环境变量 ``AGENTRUN_BASE_URL`` /
        ``AGENTRUN_API_KEY`` / ``AGENTRUN_MODEL`` / ``AGENTRUN_TIMEOUT`` /
        ``AGENTRUN_REASONING_EFFORT`` 分别覆盖同名字段。超时默认 30 秒。

    Raises:
        ModelClientError: 配置文件无法读取、字段类型错误或必需配置缺失。

    Notes:
        每次调用重新读取配置，不修改环境变量。默认文件不存在时仍可只用环境变量；
        显式指定的文件不存在则报错。配置错误在第一次决策前暴露。
    """
    configuration = _local_configuration()

    def value(field_name: str, environment_name: str, default: Any = "") -> Any:
        return os.environ.get(environment_name, configuration.get(field_name, default))

    def text(field_name: str, environment_name: str) -> str:
        raw = value(field_name, environment_name)
        if not isinstance(raw, str):
            raise ModelClientError(f"模型配置 {field_name} / {environment_name} 必须是字符串")
        return raw.strip()

    base_url = text("base_url", ENV_BASE_URL)
    api_key = text("api_key", ENV_API_KEY)
    model = text("model", ENV_MODEL)
    missing = [
        name
        for name, value in ((ENV_BASE_URL, base_url), (ENV_API_KEY, api_key), (ENV_MODEL, model))
        if not value
    ]
    if missing:
        raise ModelClientError(f"缺少模型配置或环境变量：{', '.join(missing)}")

    raw_timeout = value("timeout", "AGENTRUN_TIMEOUT", 30.0)
    try:
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (str, int, float)):
            raise ValueError
        timeout = float(raw_timeout)
    except ValueError, OverflowError:
        raise ModelClientError("AGENTRUN_TIMEOUT 必须是有限的正数") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ModelClientError("AGENTRUN_TIMEOUT 必须是有限的正数")
    reasoning_effort = text("reasoning_effort", "AGENTRUN_REASONING_EFFORT") or None
    return HttpModelClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
    )
