"""在线播放预检的单元测试（全部离线，使用本地假会话）。"""

from __future__ import annotations

import threading

import pytest
import requests

from hls_downloader import downloader
from hls_downloader.models import (
    DownloadCancelled,
    InvalidPlaylistError,
    InvalidUrlError,
    PlaylistFetchError,
)
from hls_downloader.playback import inspect_playlist

from _helpers import FakeResponse, FakeSession, build_media_playlist, build_segment_routes

PLAYLIST_URL = "https://cdn.test/video/index.m3u8"
SEGMENTS = ["seg0.ts", "seg1.ts", "seg2.ts"]
SECRET_A = "unit-test-secret"
BASE_B = "https://cdn.test:8443/prefix/"
DOWNLOAD_LINK = "https://cdn.test/mu/abc123/hls/1/index.m3u8"
SIGNED_URL = "https://cdn.test/signed/index.m3u8"

MASTER_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
    "video/360/index.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\n"
    "video/720/index.m3u8\n"
)


def _session_for(
    text: str,
    url: str = PLAYLIST_URL,
    *,
    extra: dict[str, object] | None = None,
) -> FakeSession:
    routes: dict[str, object] = {url: FakeResponse(body=text, url=url)}
    routes.update(extra or {})
    return FakeSession(routes)


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_playback_returns_segment_info():
    session = _session_for(build_media_playlist(SEGMENTS, durations=[4.0, 5.0, 6.0]))

    info = inspect_playlist(PLAYLIST_URL, session=session)

    assert info.url == PLAYLIST_URL
    assert info.segment_count == 3
    assert info.total_duration == pytest.approx(15.0)
    assert info.is_master is False
    assert info.is_live is False
    assert info.encryption_method is None
    assert info.warnings == ()


def test_playback_does_not_request_segments_or_keys():
    """「只播放不下载」：预检除播放列表外，一个分片、一个密钥都不许取。"""
    playlist = build_media_playlist(
        SEGMENTS,
        prefix_lines=['#EXT-X-KEY:METHOD=AES-128,URI="key.bin"'],
    )
    session = _session_for(
        playlist,
        extra=build_segment_routes("https://cdn.test/video/", SEGMENTS)
        | {"https://cdn.test/video/key.bin": FakeResponse(body=b"k" * 16)},
    )

    info = inspect_playlist(PLAYLIST_URL, session=session)

    assert info.encryption_method == "AES-128"
    assert session.requested_urls == [PLAYLIST_URL]


def test_playback_keeps_playlist_warnings():
    playlist = build_media_playlist(SEGMENTS, durations=None)
    # 去掉 #EXTINF 时长，解析器会给出提示
    playlist = playlist.replace("#EXTINF:4.0,\n", "")
    session = _session_for(playlist)

    info = inspect_playlist(PLAYLIST_URL, session=session)

    assert info.warnings


# --------------------------------------------------------------------------
# URL 处理必须和下载一致
# --------------------------------------------------------------------------


def test_playback_signs_the_url_through_the_shared_entry_point(monkeypatch):
    """在线播放必须走下载那套签名逻辑，而不是自己重写一遍。"""
    calls: list[tuple] = []

    def recorder(url, key, *, base=None, now=None):
        calls.append((url, key, base))
        return SIGNED_URL

    monkeypatch.setattr(downloader, "rebuild_url", recorder)
    session = _session_for(build_media_playlist(SEGMENTS), url=SIGNED_URL)

    info = inspect_playlist(PLAYLIST_URL, SECRET_A, BASE_B, session=session)

    assert calls == [(PLAYLIST_URL, SECRET_A, BASE_B)]
    assert info.url == SIGNED_URL
    assert session.requested_urls == [SIGNED_URL]


def test_playback_and_download_request_the_same_url(monkeypatch, tmp_path):
    """同一个链接 + 同一组可选参数，两条路径算出的请求地址必须一致。"""
    monkeypatch.setattr(downloader, "rebuild_url", lambda *a, **k: SIGNED_URL)

    playback_session = _session_for(build_media_playlist(SEGMENTS), url=SIGNED_URL)
    inspect_playlist(PLAYLIST_URL, SECRET_A, BASE_B, session=playback_session)

    routes: dict[str, object] = {
        SIGNED_URL: FakeResponse(body=build_media_playlist(SEGMENTS), url=SIGNED_URL)
    }
    # 分片地址相对播放列表解析，而播放列表是签名后的 SIGNED_URL
    routes.update(build_segment_routes("https://cdn.test/signed/", SEGMENTS))
    download_session = FakeSession(routes)
    downloader.download_hls_video(
        PLAYLIST_URL,
        tmp_path / "out.ts",
        SECRET_A,
        BASE_B,
        session=download_session,
        resume=False,
    )

    assert playback_session.requested_urls[0] == download_session.requested_urls[0]


def test_playback_without_optional_params_requests_the_link_as_is():
    session = _session_for(build_media_playlist(SEGMENTS))

    inspect_playlist(PLAYLIST_URL, session=session)

    assert session.requested_urls == [PLAYLIST_URL]


def test_playback_with_only_optional_b_keeps_the_original_link():
    session = _session_for(build_media_playlist(SEGMENTS))

    inspect_playlist(PLAYLIST_URL, None, BASE_B, session=session)

    assert session.requested_urls == [PLAYLIST_URL]


def test_playback_signs_the_download_link_when_both_params_are_given(monkeypatch):
    calls: list[tuple] = []

    def recorder(url, key, *, base=None, now=None):
        calls.append((url, key, base))
        return SIGNED_URL

    monkeypatch.setattr(downloader, "rebuild_url", recorder)
    session = _session_for(build_media_playlist(SEGMENTS), url=SIGNED_URL)

    inspect_playlist(DOWNLOAD_LINK, SECRET_A, BASE_B, session=session)

    assert calls == [(DOWNLOAD_LINK, SECRET_A, BASE_B)]


def test_playback_rejects_invalid_url():
    with pytest.raises(InvalidUrlError):
        inspect_playlist("ftp://cdn.test/index.m3u8", session=FakeSession())

    with pytest.raises(InvalidUrlError):
        inspect_playlist("", session=FakeSession())


# --------------------------------------------------------------------------
# 在线播放比下载宽松：这些情况只提示，不拒绝
# --------------------------------------------------------------------------


def test_playback_allows_a_master_playlist_with_a_hint():
    session = _session_for(MASTER_PLAYLIST)

    info = inspect_playlist(PLAYLIST_URL, session=session)

    assert info.is_master is True
    assert info.segment_count == 0
    assert any("主播放列表" in warning for warning in info.warnings)


def test_playback_allows_a_live_playlist_with_a_hint():
    session = _session_for(build_media_playlist(SEGMENTS, endlist=False))

    info = inspect_playlist(PLAYLIST_URL, session=session)

    assert info.is_live is True
    assert any("直播" in warning for warning in info.warnings)


def test_playback_allows_an_unsupported_encryption_method_with_a_hint():
    playlist = build_media_playlist(
        SEGMENTS,
        prefix_lines=['#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"'],
    )
    session = _session_for(playlist)

    info = inspect_playlist(PLAYLIST_URL, session=session)

    assert not info.is_master
    assert any("加密" in warning for warning in info.warnings)


def test_playback_allows_byte_range_segments_with_a_hint():
    playlist = build_media_playlist(
        SEGMENTS,
        prefix_lines=["#EXT-X-BYTERANGE:1024@0"],
    )
    session = _session_for(playlist)

    info = inspect_playlist(PLAYLIST_URL, session=session)

    assert any("分片类型" in warning for warning in info.warnings)


# --------------------------------------------------------------------------
# 真正无法继续的情况：照旧报错
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "这不是播放列表",
        "#EXTM3U\n",
    ],
)
def test_playback_rejects_a_malformed_playlist(text):
    session = _session_for(text)

    with pytest.raises(InvalidPlaylistError):
        inspect_playlist(PLAYLIST_URL, session=session)


def test_playback_reports_a_client_error_without_retrying():
    session = _session_for("", extra={PLAYLIST_URL: FakeResponse(status_code=403)})

    with pytest.raises(PlaylistFetchError) as excinfo:
        inspect_playlist(PLAYLIST_URL, session=session)

    assert "403" in str(excinfo.value)
    assert len(session.requests) == 1


def test_playback_retries_a_server_error():
    session = FakeSession(
        {PLAYLIST_URL: [FakeResponse(status_code=500), FakeResponse(status_code=500)]}
    )

    with pytest.raises(PlaylistFetchError):
        inspect_playlist(PLAYLIST_URL, session=session, max_retries=1)

    assert len(session.requests) == 2


def test_playback_retries_a_timeout_with_the_same_policy_as_download():
    session = FakeSession(
        {
            PLAYLIST_URL: [
                requests.Timeout(),
                FakeResponse(body=build_media_playlist(SEGMENTS), url=PLAYLIST_URL),
            ]
        }
    )

    info = inspect_playlist(PLAYLIST_URL, session=session, max_retries=1)

    assert info.segment_count == 3
    assert len(session.requests) == 2


def test_playback_reports_an_unreachable_server():
    session = FakeSession({PLAYLIST_URL: requests.ConnectionError("boom")})

    with pytest.raises(PlaylistFetchError) as excinfo:
        inspect_playlist(PLAYLIST_URL, session=session, max_retries=0)

    assert "ConnectionError" in str(excinfo.value)


def test_playback_can_be_cancelled_before_requesting():
    cancel = threading.Event()
    cancel.set()
    session = _session_for(build_media_playlist(SEGMENTS))

    with pytest.raises(DownloadCancelled):
        inspect_playlist(PLAYLIST_URL, session=session, cancel_event=cancel)

    assert session.requests == []


def test_playback_does_not_close_a_session_it_does_not_own():
    session = _session_for(build_media_playlist(SEGMENTS))

    inspect_playlist(PLAYLIST_URL, session=session)

    assert session.closed is False
