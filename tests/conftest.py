"""测试公共夹具。

这里只放「跨文件复用、且表达一种运行前提」的东西。单个文件自己的替身应当留在
那一个文件里——过早搬到公共位置，会让读者以为它是生产代码的一部分。
"""

import pytest

from agentrun import engine as engine_module


class NoHeartbeat:
    """心跳替身：什么都不做。

    Notes:
        用来构造「心跳缺席」的场景——线程没起来、卡住，或者已经死掉。只有在那种时候，
        租约才会在工具调用期间过期，也才轮得到别人接管。真实运行里心跳默认开着，
        所以「能否被接管」必须靠关掉它来验证，而不是靠等待或多睡一会儿。
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        """忽略所有参数，与真实心跳的构造签名对齐。"""

    def start(self) -> None:
        """不启动任何东西。"""

    def stop(self) -> None:
        """没有需要停止的东西。"""


@pytest.fixture()
def no_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """把引擎的后台心跳换成空实现。

    Args:
        monkeypatch: pytest 提供，测试结束自动还原。

    Returns:
        None: 仅作为「已启用」的标记出现，调用方不需要使用返回值。
    """
    monkeypatch.setattr(engine_module, "_Heartbeat", NoHeartbeat)
