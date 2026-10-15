"""URL 校验、相对地址解析与日志脱敏的单元测试。"""

from __future__ import annotations

import pytest

from hls_downloader.models import InvalidUrlError
from hls_downloader.url_utils import (
    get_extension,
    redact_url,
    resolve_url,
    validate_http_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.test/video/index.m3u8",
        "http://cdn.test/video/index.m3u8",
        "  https://cdn.test/video/index.m3u8  ",
        "https://cdn.test:8443/a/b/index.m3u8?token=abc",
    ],
)
def test_validate_http_url_accepts_reasonable_urls(url):
    assert validate_http_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "ftp://cdn.test/index.m3u8",
        "file:///tmp/index.m3u8",
        "cdn.test/index.m3u8",
        "https://",
        "https://cdn.test/a b.m3u8",
    ],
)
def test_validate_http_url_rejects_invalid_input(url):
    with pytest.raises(InvalidUrlError):
        validate_http_url(url)


def test_validate_http_url_error_message_is_actionable():
    with pytest.raises(InvalidUrlError) as excinfo:
        validate_http_url("ftp://cdn.test/index.m3u8")
    assert "http" in excinfo.value.message.lower()


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("seg0.ts", "https://cdn.test/video/seg0.ts"),
        ("./seg1.ts", "https://cdn.test/video/seg1.ts"),
        ("../other/seg2.ts", "https://cdn.test/other/seg2.ts"),
        ("/root/seg3.ts", "https://cdn.test/root/seg3.ts"),
        ("//other.test/seg4.ts", "https://other.test/seg4.ts"),
        (
            "https://third.test/seg5.ts",
            "https://third.test/seg5.ts",
        ),
        ("  seg6.ts  ", "https://cdn.test/video/seg6.ts"),
    ],
)
def test_resolve_url_handles_relative_and_absolute(reference, expected):
    assert resolve_url("https://cdn.test/video/index.m3u8", reference) == expected


def test_resolve_url_rejects_empty_reference():
    with pytest.raises(InvalidUrlError):
        resolve_url("https://cdn.test/video/index.m3u8", "   ")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://cdn.test/a/seg0.ts", ".ts"),
        ("https://cdn.test/a/SEG1.M4S", ".m4s"),
        ("https://cdn.test/a/seg?token=1", ""),
        ("https://cdn.test/a/", ""),
    ],
)
def test_get_extension(url, expected):
    assert get_extension(url) == expected


def test_redact_url_hides_query_and_middle_path():
    redacted = redact_url("https://cdn.test/secret/path/index.m3u8?token=SUPERSECRET&sig=abc")

    assert "SUPERSECRET" not in redacted
    assert "abc" not in redacted
    assert "secret" not in redacted
    assert redacted == "https://cdn.test/…/index.m3u8?…"


def test_redact_url_keeps_host_and_filename():
    assert redact_url("https://cdn.test/index.m3u8") == "https://cdn.test/index.m3u8"


def test_redact_url_drops_credentials_in_the_userinfo():
    """有的服务把令牌放在 ``user:password@`` 里，那部分不能进日志。"""
    redacted = redact_url("https://token:s3cret@cdn.test/video/index.m3u8?wsSecret=x")

    assert "s3cret" not in redacted
    assert "token" not in redacted
    assert redacted.startswith("https://cdn.test/")


def test_redact_url_keeps_the_port_when_dropping_credentials():
    assert redact_url("https://u:p@cdn.test:8443/a/b.m3u8") == "https://cdn.test:8443/…/b.m3u8"


def test_redact_url_handles_unparseable_input():
    assert redact_url("") == "<空>"
    assert "无效" in redact_url("not-a-url")
