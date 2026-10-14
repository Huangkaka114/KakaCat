"""界面层纯函数的单元测试（不需要图形环境）。"""

from __future__ import annotations

import pytest

from gui.components import (
    format_bytes,
    format_duration,
    format_elapsed,
    format_speed,
)
from gui.main_window import default_filename_from_url, sanitize_filename


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("video.ts", "video.ts"),
        ("我的视频.mp4", "我的视频.mp4"),
        ("a<b>c:d.ts", "a_b_c_d.ts"),
        ("a/b\\c.ts", "a_b_c.ts"),
        ("trailing.  ", "trailing"),
        ("new\nline.ts", "new_line.ts"),
    ],
)
def test_sanitize_filename_cleans_invalid_characters(raw, expected):
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "...", "..."])
def test_sanitize_filename_uses_fallback_when_empty(raw):
    assert sanitize_filename(raw) == "video"


@pytest.mark.parametrize("reserved", ["CON", "nul", "COM1", "LPT9"])
def test_sanitize_filename_avoids_windows_reserved_names(reserved):
    assert sanitize_filename(reserved).startswith("_")


def test_sanitize_filename_truncates_very_long_names():
    assert len(sanitize_filename("x" * 400)) <= 120


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://cdn.test/video/movie123.m3u8", "movie123.ts"),
        ("https://cdn.test/video/index.m3u8", "video.ts"),
        ("https://cdn.test/video/master.m3u8", "video.ts"),
        ("https://cdn.test/video/playlist.m3u8", "video.ts"),
        ("https://cdn.test/video/", "video.ts"),
        ("https://cdn.test/", "video.ts"),
    ],
)
def test_default_filename_from_url(url, expected):
    assert default_filename_from_url(url) == expected


def test_default_filename_from_url_is_sanitized():
    assert "/" not in default_filename_from_url("https://cdn.test/video/a%3Ab.m3u8")


@pytest.mark.parametrize(
    "size,expected",
    [
        (0, "0 B"),
        (-10, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (1024 * 1024 * 1024, "1.0 GB"),
    ],
)
def test_format_bytes(size, expected):
    assert format_bytes(size) == expected


def test_format_speed_shows_unknown_instead_of_fake_zero():
    assert format_speed(0) == "—"
    assert format_speed(-1) == "—"
    assert format_speed(2048) == "2.0 KB/s"


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, "—"),
        (0, "—"),
        (-5, "—"),
        (45, "45 秒"),
        (90, "1 分 30 秒"),
        (3700, "1 小时 1 分"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0.0 秒"), (12.34, "12.3 秒"), (90, "1 分 30 秒")],
)
def test_format_elapsed(seconds, expected):
    assert format_elapsed(seconds) == expected
