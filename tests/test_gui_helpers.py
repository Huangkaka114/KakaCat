"""界面层纯函数的单元测试（不需要图形环境）。"""

from __future__ import annotations

import pytest

from gui.components import (
    format_bytes,
    format_duration,
    format_elapsed,
    format_playback_summary,
    format_speed,
)
from hls_downloader.playback import PlaybackInfo
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
def test_default_filename_from_url_honours_the_given_suffix(url, expected):
    assert default_filename_from_url(url, ".ts") == expected


@pytest.mark.parametrize(
    "ffmpeg_available,expected_suffix",
    [(True, ".mp4"), (False, ".ts")],
)
def test_default_output_suffix_follows_ffmpeg(monkeypatch, ffmpeg_available, expected_suffix):
    """.mp4 需要 FFmpeg 重新封装，没装时默认值必须是能直接成功的 .ts。"""
    from gui import main_window as mw

    monkeypatch.setattr(
        mw, "find_ffmpeg", lambda: "/usr/bin/ffmpeg" if ffmpeg_available else None
    )

    assert mw.default_output_suffix() == expected_suffix


def test_default_filename_follows_the_default_suffix(monkeypatch):
    """文件名留空后缀时跟着 FFmpeg 走——有 FFmpeg 就是 .mp4。"""
    from gui import main_window as mw

    monkeypatch.setattr(mw, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    assert mw.default_filename_from_url("https://cdn.test/video/movie123.m3u8") == "movie123.mp4"

    monkeypatch.setattr(mw, "find_ffmpeg", lambda: None)
    assert mw.default_filename_from_url("https://cdn.test/video/movie123.m3u8") == "movie123.ts"


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


def test_format_playback_summary_reports_segments_and_duration():
    info = PlaybackInfo(
        url="https://cdn.test/a.m3u8", segment_count=12, total_duration=125.0
    )

    summary = format_playback_summary(info)

    assert "12 个分片" in summary
    assert "2 分 5 秒" in summary
    assert "未加密" in summary


def test_format_playback_summary_reports_the_encryption_method():
    info = PlaybackInfo(
        url="https://cdn.test/a.m3u8",
        segment_count=3,
        total_duration=12.0,
        encryption_method="AES-128",
    )

    assert "AES-128" in format_playback_summary(info)


def test_format_playback_summary_treats_method_none_as_unencrypted():
    """播放列表显式写 METHOD=NONE 时解析结果是字符串 "NONE"，它不是加密。"""
    info = PlaybackInfo(
        url="https://cdn.test/a.m3u8",
        segment_count=3,
        total_duration=12.0,
        encryption_method="NONE",
    )

    summary = format_playback_summary(info)

    assert "NONE" not in summary
    assert "未加密" in summary


def test_format_playback_summary_handles_a_master_playlist():
    summary = format_playback_summary(PlaybackInfo(url="https://cdn.test/a.m3u8", is_master=True))

    assert "主播放列表" in summary


def test_format_playback_summary_handles_a_live_playlist():
    summary = format_playback_summary(PlaybackInfo(url="https://cdn.test/a.m3u8", is_live=True))

    assert "直播" in summary
