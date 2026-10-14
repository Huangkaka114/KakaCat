"""预留参数工具包的单元测试。

验证参数 A/B 的透传与参数 A 的 URL 签名逻辑，全部离线完成，
不依赖任何网络库。
"""

from __future__ import annotations

import os

import pytest

from hls_downloader.models import InvalidUrlError
from hls_downloader.util import (
    CLIENT_WS_TTL,
    build_url,
    md5hex,
    process_optional_a,
    process_optional_b,
    process_optional_params,
    rebuild_url,
    resource_token,
    sign,
)


@pytest.mark.parametrize(
    "optional_a,optional_b",
    [
        (None, None),
        ("x", None),
        (None, "y"),
        ("x", "y"),
        ("", ""),
        ("SECRET-A", "SECRET-B"),
        ("  a  ", ""),
    ],
)
def test_process_optional_params_passes_values_through(optional_a, optional_b):
    assert process_optional_params(optional_a, optional_b) == (optional_a, optional_b)


@pytest.mark.parametrize("value", [None, "", "x", "  带空格的 中文 ", "A" * 500])
def test_process_optional_a_passes_value_through(value):
    assert process_optional_a(value) == value


@pytest.mark.parametrize("value", [None, "", "y", "  带空格的 中文 ", "B" * 500])
def test_process_optional_b_passes_value_through(value):
    assert process_optional_b(value) == value


def test_process_optional_params_returns_a_pair():
    result = process_optional_params("a", "b")
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_md5hex_matches_hashlib():
    assert md5hex("abc") == "900150983cd24fb0d6963f7d28e17f72"


def test_sign_is_md5_of_value_path_and_ws_time():
    assert sign("/x", "v", 1700000000) == md5hex("v/x1700000000")


# --------------------------------------------------------------------------
# 参数 A：授权密钥与请求 URL 重建
# --------------------------------------------------------------------------

NOW = 1700000000.0
EXPIRY = int(NOW) + CLIENT_WS_TTL
SECRET = "SECRET-A"
PLAYLIST_URL = "https://cdn.test/video/index.m3u8"


def test_build_url_contains_secret_and_time_tags():
    url = build_url("cdn.test", "/video/index.m3u8", SECRET, EXPIRY, scheme="https")

    assert url == (
        f"https://cdn.test/video/index.m3u8"
        f"?wsSecret={sign('/video/index.m3u8', SECRET, EXPIRY)}&wsTime={EXPIRY}"
    )


def test_rebuild_url_keeps_scheme_host_and_path():
    rebuilt = rebuild_url(PLAYLIST_URL, SECRET, now=NOW)

    assert rebuilt.startswith("https://cdn.test/video/index.m3u8?wsSecret=")
    assert rebuilt.endswith(f"&wsTime={EXPIRY}")


def test_rebuild_url_replaces_existing_query_string():
    original = f"{PLAYLIST_URL}?wsSecret=stale&wsTime=1&token=leak"

    rebuilt = rebuild_url(original, SECRET, now=NOW)

    assert "stale" not in rebuilt
    assert "token=leak" not in rebuilt
    assert rebuilt.count("?") == 1


def test_rebuild_url_accepts_http_and_strips_key_whitespace():
    rebuilt = rebuild_url("http://cdn.test/a/index.m3u8", f"  {SECRET}  ", now=NOW)

    assert rebuilt.startswith("http://cdn.test/a/index.m3u8?wsSecret=")
    assert rebuilt == build_url("cdn.test", "/a/index.m3u8", SECRET, EXPIRY, scheme="http")


def test_rebuild_url_defaults_path_to_root():
    assert rebuild_url("https://cdn.test", SECRET, now=NOW).startswith(
        "https://cdn.test/?wsSecret="
    )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_rebuild_url_rejects_empty_key(value):
    with pytest.raises(InvalidUrlError):
        rebuild_url(PLAYLIST_URL, value, now=NOW)


@pytest.mark.parametrize("url", ["not-a-url", "/only/path.m3u8"])
def test_rebuild_url_rejects_unparseable_url(url):
    with pytest.raises(InvalidUrlError):
        rebuild_url(url, SECRET, now=NOW)


# --------------------------------------------------------------------------
# 参数 B：从下载链接推导真实播放列表地址
# --------------------------------------------------------------------------

#: 服务方的实例比对：密钥从环境变量读，**不入库**。
#:
#: 仓库里不留任何真实授权密钥（CLAUDE.md 第十节），所以这条用例默认跳过；
#: 想对着真实服务复验一次时，本地跑：
#:
#:     KAKACAT_TEST_KEY=<你的密钥> python -m pytest tests/test_util.py -k worked_example
#:
#: 路径模板、签名规则和 wsTime 口径都已被下面那些占位密钥的用例覆盖，
#: 这条只负责确认「离线推出来的结果和服务方给出的实例逐字节一致」。
EXAMPLE_KEY = os.environ.get("KAKACAT_TEST_KEY", "")
EXAMPLE_BASE = "https://example"
EXAMPLE_LINK = "https://example.com/cover/example__1000001/example__1000001_sync.webp"
EXAMPLE_WS_TIME = 1791990982
#: 服务方给出的实例里，该密钥在 path=/mu/example__1000001/hls/1/index.m3u8、
#: wsTime=1791990982 时的签名。
EXAMPLE_SIGNATURE = "d20a9146fc5071e2802789e0eb7afaed"


@pytest.mark.skipif(not EXAMPLE_KEY, reason="未提供 KAKACAT_TEST_KEY，跳过实例比对")
def test_rebuild_url_matches_the_worked_example():
    rebuilt = rebuild_url(
        EXAMPLE_LINK,
        EXAMPLE_KEY,
        base=EXAMPLE_BASE,
        now=EXAMPLE_WS_TIME - CLIENT_WS_TTL,
    )

    assert rebuilt == (
        f"{EXAMPLE_BASE}/mu/example__1000001/hls/1/index.m3u8"
        f"?wsSecret={EXAMPLE_SIGNATURE}&wsTime={EXAMPLE_WS_TIME}"
    )


def test_resource_token_prefers_the_segment_with_double_underscore():
    token = "e7c7680e259136acbc348a062c__1284466"
    link = f"https://example.com/cover/{token}/{token}_sync.webp"

    assert resource_token(link) == token


@pytest.mark.parametrize(
    "link,expected",
    [
        ("https://example.com/movie__7/poster.webp", "movie__7"),
        ("https://example.com/movie__7/a/b/c__9_thumb.jpg", "movie__7"),
        ("https://example.com/cover/plain/poster.webp", "plain"),
        ("https://example.com/poster.webp", "poster"),
    ],
)
def test_resource_token_fallbacks(link, expected):
    assert resource_token(link) == expected


@pytest.mark.parametrize("link", ["https://example.com", "https://example.com/"])
def test_resource_token_needs_a_path(link):
    with pytest.raises(InvalidUrlError):
        resource_token(link)


def test_rebuild_url_with_base_builds_the_templated_path():
    token = "e7c7680e259136acbc348a062c__1284466"
    link = f"https://example.com/cover/{token}/{token}_sync.webp"

    rebuilt = rebuild_url(link, SECRET, base="https://cdn.test:8443/prefix/", now=NOW)

    expected_path = f"/prefix/mu/{token}/hls/1/index.m3u8"
    assert rebuilt == build_url("cdn.test:8443", expected_path, SECRET, EXPIRY, scheme="https")


def test_rebuild_url_with_baseless_scheme_assumes_https():
    assert rebuild_url(EXAMPLE_LINK, SECRET, base="cdn.test", now=NOW).startswith(
        "https://cdn.test/mu/"
    )


@pytest.mark.parametrize("base", ["", "   ", None])
def test_rebuild_url_without_base_keeps_the_original_url(base):
    rebuilt = rebuild_url(EXAMPLE_LINK, SECRET, base=base, now=NOW)

    assert rebuilt.startswith(f"{EXAMPLE_LINK}?wsSecret=")


@pytest.mark.parametrize("base", ["ftp://cdn.test", "https://", "http:///nohost"])
def test_rebuild_url_rejects_invalid_base(base):
    with pytest.raises(InvalidUrlError):
        rebuild_url(EXAMPLE_LINK, SECRET, base=base, now=NOW)


def test_rebuild_url_with_base_requires_a_download_link_path():
    with pytest.raises(InvalidUrlError):
        rebuild_url("https://example.com", SECRET, base=EXAMPLE_BASE, now=NOW)


def test_util_module_has_no_network_dependency():
    """工具包不依赖任何网络库，从结构上保证占位实现不会发起请求。"""
    import hls_downloader.util as util

    assert not hasattr(util, "requests")
    assert not hasattr(util, "urllib")
