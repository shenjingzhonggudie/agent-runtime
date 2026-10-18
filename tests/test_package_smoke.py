"""骨架冒烟测试：只验证包可安装、可导入、版本可读。

刻意不测任何业务行为：第 0 轮还没有契约，测试不应提前固定选择。
"""

import agentrun


def test_package_importable() -> None:
    assert agentrun.__name__ == "agentrun"


def test_version_is_non_empty_string() -> None:
    assert isinstance(agentrun.__version__, str)
    assert agentrun.__version__.strip() != ""
