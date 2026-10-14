"""M3U8 播放列表解析的单元测试。"""

from __future__ import annotations

import pytest

from hls_downloader.models import (
    InvalidPlaylistError,
    LivePlaylistError,
    MasterPlaylistError,
    SegmentKind,
    UnsupportedEncryptionError,
    UnsupportedSegmentError,
)
from hls_downloader.playlist import parse_playlist

from _helpers import build_media_playlist

BASE_URL = "https://cdn.test/video/index.m3u8"


def test_parses_plain_media_playlist():
    text = build_media_playlist(["seg0.ts", "seg1.ts", "seg2.ts"])

    info = parse_playlist(text, BASE_URL)

    assert info.segment_count == 3
    assert info.has_endlist is True
    assert info.playlist_type == "VOD"
    assert info.version == 3
    assert info.target_duration == 4
    assert info.segment_kind is SegmentKind.TS
    assert [segment.duration for segment in info.segments] == [4.0, 4.0, 4.0]
    assert [segment.index for segment in info.segments] == [0, 1, 2]
    assert info.total_duration == 12.0


def test_extinf_title_is_kept():
    text = (
        "#EXTM3U\n"
        "#EXTINF:5.5,第一个片段\n"
        "seg0.ts\n"
        "#EXT-X-ENDLIST\n"
    )

    info = parse_playlist(text, BASE_URL)

    assert info.segments[0].duration == 5.5
    assert info.segments[0].title == "第一个片段"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("seg0.ts", "https://cdn.test/video/seg0.ts"),
        ("../other/seg1.ts", "https://cdn.test/other/seg1.ts"),
        ("/root/seg2.ts", "https://cdn.test/root/seg2.ts"),
        ("https://third.test/seg3.ts", "https://third.test/seg3.ts"),
    ],
)
def test_relative_segment_urls_are_resolved(name, expected):
    text = build_media_playlist([name])

    info = parse_playlist(text, BASE_URL)

    assert info.segments[0].url == expected


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_empty_playlist_is_rejected(text):
    with pytest.raises(InvalidPlaylistError):
        parse_playlist(text, BASE_URL)


def test_playlist_without_header_is_rejected():
    with pytest.raises(InvalidPlaylistError) as excinfo:
        parse_playlist("#EXTINF:4.0,\nseg0.ts\n#EXT-X-ENDLIST\n", BASE_URL)

    assert "EXTM3U" in excinfo.value.message


def test_playlist_without_segments_is_rejected():
    with pytest.raises(InvalidPlaylistError):
        parse_playlist("#EXTM3U\n#EXT-X-ENDLIST\n", BASE_URL)


def test_master_playlist_is_reported_clearly():
    text = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=1280x720\n"
        "720p/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=640000,RESOLUTION=640x360\n"
        "360p/index.m3u8\n"
    )

    with pytest.raises(MasterPlaylistError) as excinfo:
        parse_playlist(text, BASE_URL)

    assert "主播放列表" in excinfo.value.message


def test_live_playlist_is_rejected():
    text = build_media_playlist(["seg0.ts", "seg1.ts"], endlist=False)

    with pytest.raises(LivePlaylistError) as excinfo:
        parse_playlist(text, BASE_URL)

    assert "ENDLIST" in excinfo.value.message


def test_aes128_playlist_is_accepted_and_carries_the_key():
    """标准的整段 AES-128 加密应当被接受，并把密钥关联到分片。"""
    text = build_media_playlist(
        ["seg0.ts", "seg1.ts"],
        prefix_lines=[
            '#EXT-X-KEY:METHOD=AES-128,URI="https://key.test/k",'
            "IV=0x0123456789ABCDEF0123456789ABCDEF"
        ],
    )

    info = parse_playlist(text, BASE_URL)

    assert info.encryption_method == "AES-128"
    first = info.segments[0]
    assert first.key is not None
    assert first.key.method == "AES-128"
    assert first.key.uri == "https://key.test/k"
    assert first.key.iv == "0x0123456789ABCDEF0123456789ABCDEF"
    # 密钥按 RFC 8216 作用于其后的所有分片
    assert info.segments[1].key == first.key


def test_relative_key_uri_is_resolved_against_the_playlist():
    text = build_media_playlist(
        ["seg0.ts"], prefix_lines=['#EXT-X-KEY:METHOD=AES-128,URI="keys/k1.bin"']
    )

    info = parse_playlist(text, BASE_URL)

    assert info.segments[0].key.uri == "https://cdn.test/video/keys/k1.bin"


def test_method_none_cancels_a_previous_key():
    """``METHOD=NONE`` 之后的点分片应当恢复为未加密。"""
    text = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://key.test/k"\n'
        "#EXTINF:4.0,\nseg0.ts\n"
        "#EXT-X-KEY:METHOD=NONE\n"
        "#EXTINF:4.0,\nseg1.ts\n"
        "#EXT-X-ENDLIST\n"
    )

    info = parse_playlist(text, BASE_URL)

    assert info.segments[0].key is not None
    assert info.segments[1].key is None


def test_sample_aes_is_rejected_as_unimplemented():
    """SAMPLE-AES 当前版本没有实现，会被拒绝——理由是"没实现"，不是别的。"""
    text = build_media_playlist(
        ["seg0.ts"], prefix_lines=['#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key"']
    )

    with pytest.raises(UnsupportedEncryptionError) as excinfo:
        parse_playlist(text, BASE_URL)

    assert "SAMPLE-AES" in excinfo.value.message
    assert "AES-128" in excinfo.value.message


def test_non_identity_keyformat_is_recorded_but_not_special_cased():
    """非 identity 的 KEYFORMAT 只作记录，不触发任何特殊处理。"""
    text = build_media_playlist(
        ["seg0.ts"],
        prefix_lines=[
            '#EXT-X-KEY:METHOD=AES-128,URI="https://key.test/k",'
            'KEYFORMAT="com.example.something"'
        ],
    )

    info = parse_playlist(text, BASE_URL)

    assert info.encryption_method == "AES-128"
    assert info.segments[0].key.key_format == "com.example.something"
    assert info.segments[0].key.uri == "https://key.test/k"


def test_aes128_without_key_uri_is_rejected():
    """声明了 AES-128 却没给密钥地址，必须报错而不是当成未加密。"""
    text = build_media_playlist(
        ["seg0.ts"], prefix_lines=["#EXT-X-KEY:METHOD=AES-128"]
    )

    with pytest.raises(UnsupportedEncryptionError) as excinfo:
        parse_playlist(text, BASE_URL)

    assert "URI" in excinfo.value.message


def test_session_key_encryption_is_rejected():
    text = (
        "#EXTM3U\n"
        '#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="skd://key"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=1000\n"
        "v/index.m3u8\n"
    )

    with pytest.raises(UnsupportedEncryptionError):
        parse_playlist(text, BASE_URL)


def test_method_none_is_treated_as_unencrypted():
    text = build_media_playlist(
        ["seg0.ts"],
        prefix_lines=["#EXT-X-KEY:METHOD=NONE"],
    )

    info = parse_playlist(text, BASE_URL)

    assert info.encryption_method == "NONE"
    assert info.segment_count == 1


def test_byte_range_playlist_is_rejected():
    text = build_media_playlist(
        ["seg0.ts"],
        prefix_lines=["#EXT-X-BYTERANGE:1024@0"],
    )

    with pytest.raises(UnsupportedSegmentError) as excinfo:
        parse_playlist(text, BASE_URL)

    assert "BYTERANGE" in excinfo.value.message


def test_audio_only_playlist_is_rejected():
    text = build_media_playlist(["seg0.aac", "seg1.aac"])

    with pytest.raises(UnsupportedSegmentError) as excinfo:
        parse_playlist(text, BASE_URL)

    assert "音频" in excinfo.value.message


def test_fmp4_playlist_is_detected_with_init_segment():
    text = build_media_playlist(
        ["seg0.m4s", "seg1.m4s"],
        prefix_lines=['#EXT-X-MAP:URI="init.mp4"'],
    )

    info = parse_playlist(text, BASE_URL)

    assert info.segment_kind is SegmentKind.FMP4
    assert info.init_segment_url == "https://cdn.test/video/init.mp4"


def test_fmp4_without_map_warns():
    text = build_media_playlist(["seg0.m4s"])

    info = parse_playlist(text, BASE_URL)

    assert info.segment_kind is SegmentKind.FMP4
    assert info.init_segment_url is None
    assert any("EXT-X-MAP" in warning for warning in info.warnings)


def test_extensionless_segments_are_unknown_with_warning():
    text = build_media_playlist(["segment-1", "segment-2"])

    info = parse_playlist(text, BASE_URL)

    assert info.segment_kind is SegmentKind.UNKNOWN
    assert any("无法从分片地址判断" in warning for warning in info.warnings)


def test_quoted_attribute_containing_comma_is_parsed():
    text = build_media_playlist(
        ["seg0.m4s"],
        prefix_lines=['#EXT-X-MAP:URI="https://cdn.test/video/init,part1.mp4"'],
    )

    info = parse_playlist(text, BASE_URL)

    assert info.init_segment_url == "https://cdn.test/video/init,part1.mp4"


def test_discontinuity_is_reported_as_warning():
    text = build_media_playlist(
        ["seg0.ts", "seg1.ts"],
        prefix_lines=["#EXT-X-DISCONTINUITY"],
    )

    info = parse_playlist(text, BASE_URL)

    assert any("DISCONTINUITY" in warning for warning in info.warnings)


def test_crlf_and_bom_are_tolerated():
    text = "﻿" + build_media_playlist(["seg0.ts"]).replace("\n", "\r\n")

    info = parse_playlist(text, BASE_URL)

    assert info.segment_count == 1


# --------------------------------------------------------------------------
# 非视频分片：必须明确拒绝，不能当成"未知格式"下载后报成功
# --------------------------------------------------------------------------


def test_subtitle_only_playlist_is_rejected():
    """全 .vtt 的字幕列表产出的是文本，不是视频，必须明确拒绝。

    修复前它会被归为"未知格式"，照常下载并报告成功——也就是伪装成功。
    """
    text = build_media_playlist(["sub0.vtt", "sub1.vtt"])

    with pytest.raises(UnsupportedSegmentError) as excinfo:
        parse_playlist(text, BASE_URL)

    assert "视频" in excinfo.value.message


@pytest.mark.parametrize(
    "name",
    ["a.vtt", "a.webvtt", "a.srt", "a.ttml", "a.dfxp", "a.jpg", "a.png", "a.key", "a.html"],
)
def test_non_video_segment_extensions_are_rejected(name):
    text = build_media_playlist([name])

    with pytest.raises(UnsupportedSegmentError):
        parse_playlist(text, BASE_URL)


def test_video_playlist_with_a_stray_subtitle_segment_is_rejected():
    """混进字幕分片的视频列表同样应当拒绝：产物必然是坏的，不能报成功。"""
    text = build_media_playlist(["seg0.ts", "sub0.vtt"])

    with pytest.raises(UnsupportedSegmentError):
        parse_playlist(text, BASE_URL)


def test_extensionless_segments_are_still_accepted():
    """没有扩展名的分片仍然按"未知格式"接受——不能因为修这个 bug 而误伤。

    有些源确实用不带扩展名的分片地址，它们有可能是合法视频。
    """
    text = build_media_playlist(["segment0", "segment1"])

    info = parse_playlist(text, BASE_URL)

    assert info.segment_count == 2
    assert info.segment_kind is SegmentKind.UNKNOWN
