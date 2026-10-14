"""预留业务接口的单元测试。

验证当前版本确实不会执行任何业务逻辑，也不会发起网络请求。
"""

from __future__ import annotations

import pytest

from hls_downloader.interfaces import (
    BusinessAdapter,
    BusinessParams,
    NullBusinessAdapter,
    get_default_adapter,
)


def test_default_adapter_refuses_to_execute():
    adapter = get_default_adapter()
    with pytest.raises(NotImplementedError):
        adapter.execute("a", "b")


def test_null_adapter_is_a_business_adapter():
    assert isinstance(NullBusinessAdapter(), BusinessAdapter)


def test_adapter_cannot_be_instantiated_without_implementation():
    with pytest.raises(TypeError):
        BusinessAdapter()  # 抽象方法未实现


def test_interfaces_module_has_no_network_dependency():
    """业务接口模块不依赖任何网络库，从结构上保证不会发起请求。"""
    import hls_downloader.interfaces as interfaces

    assert not hasattr(interfaces, "requests")
    assert not hasattr(interfaces, "urllib")


@pytest.mark.parametrize(
    "optional_a,optional_b,expected",
    [
        (None, None, True),
        ("", "   ", True),
        ("x", None, False),
        (None, "y", False),
    ],
)
def test_business_params_is_empty(optional_a, optional_b, expected):
    assert BusinessParams(optional_a, optional_b).is_empty is expected


def test_adapter_name_is_descriptive():
    assert "未接入" in get_default_adapter().name
