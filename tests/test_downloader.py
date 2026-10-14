"""核心下载函数的单元测试。

全部使用本地假响应，不访问任何真实视频网站。
"""

from __future__ import annotations

import logging
import threading

import pytest
import requests

from hls_downloader import downloader, util
from hls_downloader.models import (
    DownloadCancelled,
    HLSDownloaderError,
    InvalidUrlError,
    MasterPlaylistError,
    MergeError,
    OutputExistsError,
    PlaylistFetchError,
    SegmentDecryptionError,
    SegmentDownloadError,
    TaskStage,
)

from _helpers import FakeResponse, FakeSession, build_media_playlist, build_segment_routes

PLAYLIST_URL = "https://cdn.test/video/index.m3u8"
BASE = "https://cdn.test/video/"
NAMES = ["seg0.ts", "seg1.ts", "seg2.ts"]


def make_session(names: list[str] | None = None, **kwargs) -> FakeSession:
    """构造一个能完整下载的假会话。"""
    names = names if names is not None else NAMES
    routes: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(body=build_media_playlist(names))
    }
    routes.update(build_segment_routes(BASE, names, **kwargs))
    return FakeSession(routes)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """去掉重试等待，保持测试快速且确定。"""
    monkeypatch.setattr(downloader, "RETRY_BACKOFF_SECONDS", 0.0)


# --------------------------------------------------------------------------
# 正常流程
# --------------------------------------------------------------------------


def test_downloads_and_merges_all_segments(tmp_path):
    session = make_session()
    output = tmp_path / "out.ts"

    result = downloader.download_hls_video(PLAYLIST_URL, output, session=session)

    expected = b"".join(session.routes[f"{BASE}{name}"].content for name in NAMES)
    assert output.read_bytes() == expected
    assert result.output_path == str(output)
    assert result.segment_count == 3
    assert result.total_bytes == len(expected)
    assert result.elapsed_seconds >= 0


def test_output_directory_is_created_when_missing(tmp_path):
    session = make_session()
    output = tmp_path / "deep" / "nested" / "out.ts"

    downloader.download_hls_video(PLAYLIST_URL, output, session=session)

    assert output.is_file()


def test_uses_injected_session_without_closing_it(tmp_path):
    session = make_session()

    downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert session.closed is False


def test_internally_created_session_is_closed(tmp_path, monkeypatch):
    session = make_session()
    monkeypatch.setattr(downloader.requests, "Session", lambda: session)

    result = downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts")

    assert result.segment_count == 3
    assert session.closed is True
    assert "User-Agent" in session.headers


def test_internal_session_pool_is_sized_for_the_worker_count(tmp_path, monkeypatch):
    """连接池要按并发数放大。

    requests 默认每主机只池化 10 条连接，并发超过它时多出来的连接用完即弃，
    每个分片都要重做一次 TCP + TLS 握手——这是高并发反而快不起来的原因之一。
    """
    session = make_session()
    monkeypatch.setattr(downloader.requests, "Session", lambda: session)

    downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", workers=8)

    adapter = session.adapters["https://"]
    assert adapter._pool_maxsize >= 8
    assert set(session.adapters) == {"http://", "https://"}


def test_fmp4_init_segment_is_downloaded_first(tmp_path):
    playlist = build_media_playlist(["seg0.m4s"], prefix_lines=['#EXT-X-MAP:URI="init.mp4"'])
    session = FakeSession(
        {
            PLAYLIST_URL: FakeResponse(body=playlist),
            f"{BASE}init.mp4": FakeResponse(body=b"INIT"),
            f"{BASE}seg0.m4s": FakeResponse(body=b"DATA"),
        }
    )

    result = downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.mp4", session=session)

    assert (tmp_path / "out.mp4").read_bytes() == b"INITDATA"
    assert result.segment_count == 2  # 初始化分片 + 1 个媒体分片


def test_streams_segments_without_loading_everything(tmp_path):
    """分片内容以多个 chunk 返回时也必须完整写入。"""
    session = FakeSession(
        {
            PLAYLIST_URL: FakeResponse(body=build_media_playlist(["seg0.ts"])),
            f"{BASE}seg0.ts": FakeResponse(
                chunks=[b"a" * 100, b"b" * 100, b"c" * 100],
            ),
        }
    )

    downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert (tmp_path / "out.ts").read_bytes() == b"a" * 100 + b"b" * 100 + b"c" * 100


# --------------------------------------------------------------------------
# 进度回调
# --------------------------------------------------------------------------


def test_progress_callback_reports_real_stages_and_counts(tmp_path):
    session = make_session()
    reports = []

    downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, progress_callback=reports.append
    )

    stages = [report.stage for report in reports]
    assert stages[0] is TaskStage.FETCHING_PLAYLIST
    assert TaskStage.PARSING_PLAYLIST in stages
    assert TaskStage.DOWNLOADING_SEGMENTS in stages
    assert TaskStage.MERGING in stages
    assert stages[-1] is TaskStage.COMPLETED

    downloads = [report for report in reports if report.stage is TaskStage.DOWNLOADING_SEGMENTS]
    counts = [report.completed_segments for report in downloads]
    assert counts == sorted(counts)  # 进度不会倒退
    assert downloads[-1].completed_segments == 3
    assert downloads[-1].total_segments == 3

    # 分片下完 ≠ 任务完成：合并仍可能失败，因此下载阶段封顶在 99%，
    # 只有真正完成时才显示 100%（进度条是任务级指示器）。
    assert downloads[-1].percent == pytest.approx(downloader.MAX_PERCENT_BEFORE_COMPLETION)
    assert all(report.percent <= 100.0 for report in reports)

    completed = [report for report in reports if report.stage is TaskStage.COMPLETED]
    assert completed, "缺少完成阶段的进度报告"
    assert completed[-1].percent == pytest.approx(100.0)


def test_speed_and_eta_are_not_fabricated(tmp_path):
    """数据不足时不给出速度/剩余时间，给出时必须是有效数字。"""
    session = make_session()
    reports = []

    downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, progress_callback=reports.append
    )

    for report in reports:
        assert report.speed_bps >= 0.0
        if report.eta_seconds is not None:
            assert report.eta_seconds > 0.0


def test_broken_progress_callback_does_not_break_download(tmp_path):
    def broken(_report):
        raise RuntimeError("回调故意抛错")

    session = make_session()

    result = downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, progress_callback=broken
    )

    assert result.segment_count == 3
    assert (tmp_path / "out.ts").is_file()


# --------------------------------------------------------------------------
# 可选参数
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "optional_a,optional_b",
    [(None, None), ("", "   ")],
)
def test_runs_when_optional_parameters_are_empty(tmp_path, optional_a, optional_b):
    session = make_session()

    result = downloader.download_hls_video(
        PLAYLIST_URL,
        tmp_path / "out.ts",
        optional_a=optional_a,
        optional_b=optional_b,
        session=session,
    )

    assert result.segment_count == 3


def test_optional_parameter_b_is_never_sent_anywhere(tmp_path):
    session = make_session()

    downloader.download_hls_video(
        PLAYLIST_URL,
        tmp_path / "out.ts",
        optional_b="SECRET-B",
        session=session,
    )

    for url, kwargs in session.requests:
        assert "SECRET" not in url
        assert "SECRET" not in repr(kwargs)
    assert "SECRET" not in repr(session.headers)


#: 参数 A 的测试密钥：只在本地参与签名，任何请求或日志里都不应出现原文。
SECRET_A = "KEY-9f3c2b"
FIXED_NOW = 1_700_000_000.0


class _FixedClock:
    """把 ``util.time`` 换成固定时钟，让签名里的 wsTime 可预期。"""

    @staticmethod
    def time() -> float:
        return FIXED_NOW


def _signed_playlist_session() -> tuple[str, FakeSession]:
    signed = util.rebuild_url(PLAYLIST_URL, SECRET_A)
    routes: dict[str, object] = {signed: FakeResponse(body=build_media_playlist(NAMES))}
    routes.update(build_segment_routes(BASE, NAMES))
    return signed, FakeSession(routes)


def test_optional_a_signs_the_playlist_request_only(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "time", _FixedClock)
    signed, session = _signed_playlist_session()

    result = downloader.download_hls_video(
        PLAYLIST_URL,
        tmp_path / "out.ts",
        optional_a=SECRET_A,
        session=session,
    )

    assert result.segment_count == 3
    # 播放列表用的是重签名后的 URL，wsTime 取「当前时间 + 有效期」。
    assert session.requested_urls[0] == signed
    assert f"wsTime={int(FIXED_NOW) + util.CLIENT_WS_TTL}" in signed
    # 分片仍按播放列表里给出的原地址请求，不带签名。
    assert sorted(session.requested_urls[1:]) == sorted(f"{BASE}{name}" for name in NAMES)


@pytest.mark.parametrize("optional_a", [None, "", "   "])
def test_without_optional_a_the_url_is_requested_as_given(tmp_path, optional_a):
    session = make_session()

    downloader.download_hls_video(
        PLAYLIST_URL,
        tmp_path / "out.ts",
        optional_a=optional_a,
        session=session,
    )

    assert session.requested_urls[0] == PLAYLIST_URL
    assert "wsSecret" not in PLAYLIST_URL


#: 填写了参数 B 时，「视频 URL」填的是下载链接，真实地址由模板推导。
DOWNLOAD_LINK = "https://example.com/cover/example__1000001/example__1000001_sync.webp"
BASE_B = "https://example"


def _derived_session() -> tuple[str, str, FakeSession]:
    """构造只认推导出的播放列表地址的假会话，返回（地址, 分片基址, 会话）。"""
    derived = util.rebuild_url(DOWNLOAD_LINK, SECRET_A, base=BASE_B)
    derived_base = derived.rsplit("/", 1)[0] + "/"
    routes: dict[str, object] = {derived: FakeResponse(body=build_media_playlist(NAMES))}
    routes.update(build_segment_routes(derived_base, NAMES))
    return derived, derived_base, FakeSession(routes)


def test_optional_b_derives_the_real_playlist_url_from_the_download_link(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "time", _FixedClock)
    derived, derived_base, session = _derived_session()

    result = downloader.download_hls_video(
        DOWNLOAD_LINK,
        tmp_path / "out.ts",
        optional_a=SECRET_A,
        optional_b=BASE_B,
        session=session,
    )

    assert result.segment_count == 3
    assert session.requested_urls[0] == derived
    assert derived.startswith("https://example/mu/example__1000001/hls/1/index.m3u8?wsSecret=")
    assert f"wsTime={int(FIXED_NOW) + util.CLIENT_WS_TTL}" in derived
    # 原下载链接本身不会被请求；分片按推导出的地址相对解析。
    assert DOWNLOAD_LINK not in session.requested_urls
    assert sorted(session.requested_urls[1:]) == sorted(
        f"{derived_base}{name}" for name in NAMES
    )


def test_optional_b_alone_is_ignored_and_warns(tmp_path, caplog):
    session = make_session()

    with caplog.at_level(logging.WARNING, logger="hls_downloader"):
        downloader.download_hls_video(
            PLAYLIST_URL,
            tmp_path / "out.ts",
            optional_b=BASE_B,
            session=session,
        )

    assert session.requested_urls[0] == PLAYLIST_URL
    assert "没有参数 A" in caplog.text


def test_invalid_optional_b_is_rejected_before_any_request(tmp_path):
    session = make_session()

    with pytest.raises(InvalidUrlError):
        downloader.download_hls_video(
            DOWNLOAD_LINK,
            tmp_path / "out.ts",
            optional_a=SECRET_A,
            optional_b="ftp://example.com",
            session=session,
        )

    assert session.requests == []


def test_optional_a_key_never_leaks_into_requests_or_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "time", _FixedClock)
    _signed, session = _signed_playlist_session()

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    task_logger = logging.getLogger("hls_downloader.test.optional_a")
    task_logger.handlers = [_Capture()]
    task_logger.setLevel(logging.DEBUG)
    task_logger.propagate = False

    downloader.download_hls_video(
        PLAYLIST_URL,
        tmp_path / "out.ts",
        optional_a=SECRET_A,
        session=session,
        logger=task_logger,
    )

    for url, kwargs in session.requests:
        assert SECRET_A not in url
        assert SECRET_A not in repr(kwargs)
    assert [text for text in records if SECRET_A in text] == []


# --------------------------------------------------------------------------
# 播放列表错误
# --------------------------------------------------------------------------


def test_playlist_http_error_fails_fast(tmp_path):
    session = FakeSession({PLAYLIST_URL: FakeResponse(status_code=404)})
    output = tmp_path / "out.ts"

    with pytest.raises(PlaylistFetchError) as excinfo:
        downloader.download_hls_video(PLAYLIST_URL, output, session=session, max_retries=3)

    assert "404" in excinfo.value.message
    assert len(session.requests) == 1  # 4xx 不重试
    assert not output.exists()


def test_playlist_timeout_is_reported_and_retried(tmp_path):
    session = FakeSession({PLAYLIST_URL: requests.Timeout()})

    with pytest.raises(PlaylistFetchError) as excinfo:
        downloader.download_hls_video(
            PLAYLIST_URL, tmp_path / "out.ts", session=session, max_retries=1
        )

    assert "超时" in excinfo.value.message
    assert len(session.requests) == 2  # 原始请求 + 1 次重试


def test_playlist_network_error_is_reported(tmp_path):
    session = FakeSession({PLAYLIST_URL: requests.ConnectionError("网络不可达")})

    with pytest.raises(PlaylistFetchError) as excinfo:
        downloader.download_hls_video(
            PLAYLIST_URL, tmp_path / "out.ts", session=session, max_retries=0
        )

    assert "ConnectionError" in excinfo.value.message


def test_playlist_retry_then_success(tmp_path):
    session = FakeSession(
        {
            PLAYLIST_URL: [
                FakeResponse(status_code=503),
                FakeResponse(body=build_media_playlist(["seg0.ts"])),
            ],
            f"{BASE}seg0.ts": FakeResponse(body=b"OK"),
        }
    )

    result = downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, max_retries=2
    )

    assert result.segment_count == 1
    assert (tmp_path / "out.ts").read_bytes() == b"OK"


def test_master_playlist_error_surfaces_to_caller(tmp_path):
    playlist = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nv/index.m3u8\n"
    session = FakeSession({PLAYLIST_URL: FakeResponse(body=playlist)})

    with pytest.raises(MasterPlaylistError):
        downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)


def test_invalid_url_is_rejected_before_any_request(tmp_path):
    session = FakeSession({})

    with pytest.raises(InvalidUrlError):
        downloader.download_hls_video("ftp://cdn.test/index.m3u8", tmp_path / "out.ts", session=session)

    assert session.requests == []


def test_output_path_that_is_a_directory_is_rejected(tmp_path):
    session = make_session()

    with pytest.raises(HLSDownloaderError):
        downloader.download_hls_video(PLAYLIST_URL, tmp_path, session=session)

    assert session.requests == []


# --------------------------------------------------------------------------
# 分片失败与重试
# --------------------------------------------------------------------------


def test_segment_failure_never_reports_success(tmp_path):
    routes: dict[str, object] = {PLAYLIST_URL: FakeResponse(body=build_media_playlist(NAMES))}
    routes.update(build_segment_routes(BASE, NAMES[:2]))
    routes[f"{BASE}{NAMES[2]}"] = FakeResponse(status_code=500)

    session = FakeSession(routes)
    output = tmp_path / "out.ts"
    work = tmp_path / "work"

    with pytest.raises(SegmentDownloadError) as excinfo:
        downloader.download_hls_video(
            PLAYLIST_URL,
            output,
            session=session,
            temp_dir=work,
            max_retries=1,
            resume=False,
        )

    assert excinfo.value.segment_index == 3
    assert not output.exists()
    assert list(work.iterdir()) == []  # 临时文件已清理


def test_segment_retry_then_success(tmp_path):
    routes: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(body=build_media_playlist([NAMES[0]]))
    }
    routes[f"{BASE}{NAMES[0]}"] = [
        FakeResponse(status_code=503),
        FakeResponse(body=b"OK" * 10, headers={"Content-Length": "20"}),
    ]
    session = FakeSession(routes)

    result = downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, max_retries=2
    )

    assert result.total_bytes == 20
    assert (tmp_path / "out.ts").read_bytes() == b"OK" * 10
    assert len(session.requests) == 3  # 1 次播放列表 + 2 次分片


def test_empty_segment_body_is_a_failure_not_a_success(tmp_path):
    session = FakeSession(
        {
            PLAYLIST_URL: FakeResponse(body=build_media_playlist([NAMES[0]])),
            f"{BASE}{NAMES[0]}": FakeResponse(body=b"", headers={"Content-Length": "0"}),
        }
    )
    output = tmp_path / "out.ts"

    with pytest.raises(SegmentDownloadError):
        downloader.download_hls_video(
            PLAYLIST_URL, output, session=session, max_retries=1
        )

    assert not output.exists()


def test_default_retry_budget_survives_repeated_transient_breaks(tmp_path):
    """默认重试次数要能吞下连续 4 次瞬断（第 5 次成功即任务成功）。"""
    routes: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(body=build_media_playlist([NAMES[0]]))
    }
    routes[f"{BASE}{NAMES[0]}"] = [
        FakeResponse(status_code=503),
        FakeResponse(status_code=503),
        FakeResponse(status_code=503),
        FakeResponse(status_code=503),
        FakeResponse(body=b"OK" * 10, headers={"Content-Length": "20"}),
    ]

    result = downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=FakeSession(routes)
    )

    assert result.segment_count == 1
    assert (tmp_path / "out.ts").read_bytes() == b"OK" * 10


def test_short_body_without_a_transport_error_is_retried(tmp_path):
    """服务器少发字节且不报错时，不能把截断的分片当成完整分片。"""
    routes: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(body=build_media_playlist([NAMES[0]]))
    }
    routes[f"{BASE}{NAMES[0]}"] = [
        FakeResponse(body=b"OK" * 4, headers={"Content-Length": "20"}),
        FakeResponse(body=b"OK" * 10, headers={"Content-Length": "20"}),
    ]
    session = FakeSession(routes)

    result = downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, max_retries=1
    )

    assert (tmp_path / "out.ts").read_bytes() == b"OK" * 10
    assert result.total_bytes == 20
    assert len(session.requests) == 3  # 播放列表 + 2 次分片


def test_persistently_short_body_never_reports_success(tmp_path):
    routes: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(body=build_media_playlist([NAMES[0]]))
    }
    routes[f"{BASE}{NAMES[0]}"] = FakeResponse(
        body=b"OK" * 4, headers={"Content-Length": "20"}
    )
    output = tmp_path / "out.ts"

    with pytest.raises(SegmentDownloadError):
        downloader.download_hls_video(
            PLAYLIST_URL, output, session=FakeSession(routes), max_retries=1
        )

    assert not output.exists()


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------


def test_cancel_before_start(tmp_path):
    session = make_session()
    cancel = threading.Event()
    cancel.set()
    output = tmp_path / "out.ts"

    with pytest.raises(DownloadCancelled):
        downloader.download_hls_video(
            PLAYLIST_URL, output, session=session, cancel_event=cancel
        )

    assert not output.exists()
    assert session.requests == []


def test_cancel_during_segment_download_cleans_up(tmp_path):
    cancel = threading.Event()
    session = FakeSession(
        {
            PLAYLIST_URL: FakeResponse(body=build_media_playlist([NAMES[0]])),
            f"{BASE}{NAMES[0]}": FakeResponse(
                chunks=[b"a" * 32, b"b" * 32, b"c" * 32],
                cancel_event=cancel,
                cancel_after=1,
            ),
        }
    )
    output = tmp_path / "out.ts"
    work = tmp_path / "work"
    reports = []

    with pytest.raises(DownloadCancelled):
        downloader.download_hls_video(
            PLAYLIST_URL,
            output,
            session=session,
            cancel_event=cancel,
            temp_dir=work,
            resume=False,  # 这个用例专门验证「关掉续传时会清理临时文件」
            progress_callback=reports.append,
        )

    assert not output.exists()
    assert list(work.iterdir()) == []
    assert reports[-1].stage is TaskStage.CANCELLED


def test_cancel_between_segments_stops_remaining_downloads(tmp_path):
    cancel = threading.Event()
    routes: dict[str, object] = {PLAYLIST_URL: FakeResponse(body=build_media_playlist(NAMES))}
    routes.update(build_segment_routes(BASE, NAMES))

    # 第一个分片下载完成后立刻取消
    original = routes[f"{BASE}{NAMES[0]}"]
    original._cancel_event = cancel
    original._cancel_after = 99  # 只设置事件，不改动 chunk 行为

    def first_response():
        cancel.set()
        return original

    routes[f"{BASE}{NAMES[0]}"] = first_response
    session = FakeSession(routes)

    with pytest.raises(DownloadCancelled):
        downloader.download_hls_video(
            PLAYLIST_URL,
            tmp_path / "out.ts",
            session=session,
            cancel_event=cancel,
            workers=1,  # 这个用例验证的是顺序下载时的取消语义
        )

    assert f"{BASE}{NAMES[1]}" not in session.requested_urls


# --------------------------------------------------------------------------
# 输出文件保护与合并失败
# --------------------------------------------------------------------------


def test_existing_output_file_is_protected(tmp_path):
    session = make_session()
    output = tmp_path / "out.ts"
    output.write_bytes(b"OLD")

    with pytest.raises(OutputExistsError) as excinfo:
        downloader.download_hls_video(PLAYLIST_URL, output, session=session)

    assert "已存在" in excinfo.value.message
    assert output.read_bytes() == b"OLD"
    assert session.requests == []


def test_overwrite_true_replaces_existing_file(tmp_path):
    session = make_session()
    output = tmp_path / "out.ts"
    output.write_bytes(b"OLD")

    downloader.download_hls_video(PLAYLIST_URL, output, session=session, overwrite=True)

    assert output.read_bytes() != b"OLD"


def test_merge_failure_leaves_existing_file_untouched(tmp_path, monkeypatch):
    session = make_session()
    output = tmp_path / "out.ts"
    output.write_bytes(b"OLD-CONTENT")

    def boom(*args, **kwargs):
        raise MergeError("合并失败（测试注入）")

    monkeypatch.setattr(downloader.merger, "merge_segments", boom)

    with pytest.raises(MergeError):
        downloader.download_hls_video(
            PLAYLIST_URL, output, session=session, overwrite=True
        )

    assert output.read_bytes() == b"OLD-CONTENT"
    assert not (tmp_path / "out.ts.part").exists()


def test_merge_failure_reports_failed_stage(tmp_path, monkeypatch):
    session = make_session()
    reports = []

    def boom(*args, **kwargs):
        raise MergeError("合并失败（测试注入）")

    monkeypatch.setattr(downloader.merger, "merge_segments", boom)

    with pytest.raises(MergeError):
        downloader.download_hls_video(
            PLAYLIST_URL,
            tmp_path / "out.ts",
            session=session,
            progress_callback=reports.append,
        )

    assert reports[-1].stage is TaskStage.FAILED
    assert "合并失败" in reports[-1].message
    assert not (tmp_path / "out.ts").exists()


# --------------------------------------------------------------------------
# 临时文件管理
# --------------------------------------------------------------------------


def test_temp_files_are_cleaned_after_success(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    session = make_session()

    downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, temp_dir=work
    )

    assert list(work.iterdir()) == []


def test_temp_files_are_cleaned_after_failure(tmp_path):
    session = FakeSession({PLAYLIST_URL: FakeResponse(status_code=403)})
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(PlaylistFetchError):
        downloader.download_hls_video(
            PLAYLIST_URL, tmp_path / "out.ts", session=session, temp_dir=work
        )

    assert list(work.iterdir()) == []


def test_resume_skips_already_downloaded_segments(tmp_path):
    """第一次在第 3 个分片失败，第二次应当只请求剩下的那一个。"""
    work = tmp_path / "work"
    output = tmp_path / "out.ts"

    broken = make_session(**{})
    broken.routes[f"{BASE}{NAMES[2]}"] = FakeResponse(status_code=500)

    with pytest.raises(SegmentDownloadError):
        downloader.download_hls_video(
            PLAYLIST_URL,
            output,
            session=broken,
            temp_dir=work,
            max_retries=0,
            workers=1,
        )

    assert not output.exists()
    assert list(work.iterdir()), "续传时应保留临时分片目录"

    retry_session = make_session()
    reports = []
    result = downloader.download_hls_video(
        PLAYLIST_URL,
        output,
        session=retry_session,
        temp_dir=work,
        workers=1,
        progress_callback=reports.append,
    )

    assert retry_session.requested_urls[1:] == [f"{BASE}{NAMES[2]}"]
    expected = b"".join(retry_session.routes[f"{BASE}{name}"].content for name in NAMES)
    assert output.read_bytes() == expected
    assert result.segment_count == 3
    # 跳过的分片也要计入进度，否则进度条永远到不了 100%。
    assert reports[-1].percent == pytest.approx(100.0)
    assert list(work.iterdir()) == [], "成功后应当清理续传目录"


def test_resume_redownloads_a_truncated_leftover(tmp_path):
    """上次被强杀留下的半截分片（大小对不上）必须重新下载。"""
    work = tmp_path / "work"
    output = tmp_path / "out.ts"

    broken = make_session()
    broken.routes[f"{BASE}{NAMES[2]}"] = FakeResponse(status_code=500)
    with pytest.raises(SegmentDownloadError):
        downloader.download_hls_video(
            PLAYLIST_URL, output, session=broken, temp_dir=work, max_retries=0, workers=1
        )

    temp_dir = next(work.iterdir())
    victim = temp_dir / "000001.ts"
    victim.write_bytes(victim.read_bytes()[:4])

    retry_session = make_session()
    downloader.download_hls_video(
        PLAYLIST_URL, output, session=retry_session, temp_dir=work, workers=1
    )

    assert f"{BASE}{NAMES[1]}" in retry_session.requested_urls, "半截分片没有被重新下载"
    expected = b"".join(retry_session.routes[f"{BASE}{name}"].content for name in NAMES)
    assert output.read_bytes() == expected


def test_resume_discards_segments_when_the_playlist_changed(tmp_path):
    """片源换了（播放列表内容不同）时不能复用旧分片。"""
    work = tmp_path / "work"
    output = tmp_path / "out.ts"

    broken = make_session()
    broken.routes[f"{BASE}{NAMES[2]}"] = FakeResponse(status_code=500)
    with pytest.raises(SegmentDownloadError):
        downloader.download_hls_video(
            PLAYLIST_URL, output, session=broken, temp_dir=work, max_retries=0, workers=1
        )

    # 同一个地址、同一批分片名，但播放列表内容变了（时长不同）。
    changed: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(
            body=build_media_playlist(NAMES, durations=[4.0, 4.0, 9.0])
        )
    }
    changed.update(build_segment_routes(BASE, NAMES))
    retry_session = FakeSession(changed)

    downloader.download_hls_video(
        PLAYLIST_URL, output, session=retry_session, temp_dir=work, workers=1
    )

    assert sorted(retry_session.requested_urls[1:]) == sorted(
        f"{BASE}{name}" for name in NAMES
    ), "播放列表变了却复用了旧分片"


def test_resume_disabled_always_starts_from_scratch(tmp_path):
    work = tmp_path / "work"
    output = tmp_path / "out.ts"

    broken = make_session()
    broken.routes[f"{BASE}{NAMES[2]}"] = FakeResponse(status_code=500)
    with pytest.raises(SegmentDownloadError):
        downloader.download_hls_video(
            PLAYLIST_URL,
            output,
            session=broken,
            temp_dir=work,
            max_retries=0,
            workers=1,
            resume=False,
        )

    assert list(work.iterdir()) == [], "关掉续传后失败时应当清理临时文件"


def test_concurrent_downloads_keep_playlist_order(tmp_path):
    """多路并发时，合并顺序仍必须严格按播放列表来。"""
    names = [f"s{index}.ts" for index in range(12)]
    routes: dict[str, object] = {PLAYLIST_URL: FakeResponse(body=build_media_playlist(names))}
    for index, name in enumerate(names):
        routes[f"{BASE}{name}"] = FakeResponse(body=f"[{index:02d}]".encode())

    session = FakeSession(routes)
    result = downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, workers=6
    )

    expected = b"".join(f"[{index:02d}]".encode() for index in range(12))
    assert (tmp_path / "out.ts").read_bytes() == expected
    assert result.segment_count == 12


def test_concurrent_failure_never_reports_success(tmp_path):
    names = [f"s{index}.ts" for index in range(8)]
    routes: dict[str, object] = {PLAYLIST_URL: FakeResponse(body=build_media_playlist(names))}
    for index, name in enumerate(names):
        routes[f"{BASE}{name}"] = FakeResponse(body=f"[{index:02d}]".encode())
    routes[f"{BASE}s3.ts"] = FakeResponse(status_code=500)

    work = tmp_path / "work"
    output = tmp_path / "out.ts"

    with pytest.raises(SegmentDownloadError):
        downloader.download_hls_video(
            PLAYLIST_URL,
            output,
            session=FakeSession(routes),
            temp_dir=work,
            workers=4,
            max_retries=0,
            resume=False,
        )

    assert not output.exists()
    assert list(work.iterdir()) == []


def test_keep_temp_on_failure_option(tmp_path):
    """失败且显式要求保留时，临时分片目录应当保留下来。"""
    work = tmp_path / "work"
    routes: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(body=build_media_playlist(NAMES))
    }
    routes.update(build_segment_routes(BASE, NAMES[:1]))
    routes[f"{BASE}{NAMES[1]}"] = FakeResponse(status_code=500)
    session = FakeSession(routes)

    with pytest.raises(SegmentDownloadError):
        downloader.download_hls_video(
            PLAYLIST_URL,
            tmp_path / "out.ts",
            session=session,
            temp_dir=work,
            keep_temp_on_failure=True,
            max_retries=0,
        )

    kept = list(work.rglob("*"))
    assert kept, "显式要求保留时临时分片不应被清理"
    assert any(path.name.startswith("00000") for path in kept)


def test_missing_temp_parent_directory_is_created(tmp_path):
    """临时目录的父目录不存在时应当自动创建，而不是直接报错。"""
    work = tmp_path / "not-yet" / "work"
    session = make_session()

    downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, temp_dir=work
    )

    assert work.is_dir()
    assert list(work.iterdir()) == []


def test_segments_are_written_in_playlist_order(tmp_path):
    names = ["a.ts", "b.ts", "c.ts", "d.ts"]
    routes: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(body=build_media_playlist(names))
    }
    for index, name in enumerate(names):
        routes[f"{BASE}{name}"] = FakeResponse(body=str(index).encode())

    session = FakeSession(routes)

    downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert (tmp_path / "out.ts").read_bytes() == b"0123"


# --------------------------------------------------------------------------
# 回归测试：合并阶段的取消与重试时的进度
# --------------------------------------------------------------------------


class _TruncatedResponse(FakeResponse):
    """模拟传输中途断开：先真正送出部分数据，再抛出连接异常。

    与直接返回错误码不同，这种响应会让下载器先写入一部分分片数据，
    从而产生"已经上报过的部分进度"。
    """

    def iter_content(self, chunk_size: int = 1):
        for chunk in self._chunks or []:
            yield chunk
        raise requests.ConnectionError("连接被中断")


def test_progress_never_goes_backwards_when_a_segment_is_retried(tmp_path, monkeypatch):
    """重试会丢弃上一次尝试写入的分片数据，但对外呈现的进度不得倒退。"""
    # 让每次写入都上报一次进度，避免被节流掩盖住问题
    monkeypatch.setattr(downloader, "PROGRESS_EMIT_INTERVAL_SECONDS", 0.0)

    names = ["seg0.ts", "seg1.ts"]
    attempts = {"count": 0}

    def flaky_segment():
        attempts["count"] += 1
        if attempts["count"] == 1:
            # 第一次：只送出 100/200 字节就断开
            return _TruncatedResponse(
                chunks=[b"X" * 100], headers={"Content-Length": "200"}
            )
        # 第二次：正常送达，且分多次写入，制造出中间态进度
        return FakeResponse(
            chunks=[b"Y" * 50] * 4, headers={"Content-Length": "200"}
        )

    routes: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(body=build_media_playlist(names)),
        f"{BASE}seg0.ts": FakeResponse(
            body=b"X" * 200, headers={"Content-Length": "200"}
        ),
        f"{BASE}seg1.ts": flaky_segment,
    }

    percents: list[float] = []
    result = downloader.download_hls_video(
        PLAYLIST_URL,
        tmp_path / "out.ts",
        session=FakeSession(routes),
        max_retries=2,
        progress_callback=lambda report: percents.append(report.percent),
    )

    assert attempts["count"] == 2, "预期第二个分片恰好重试一次"
    assert result.segment_count == 2
    assert percents == sorted(percents), f"进度出现倒退：{percents}"
    assert percents[-1] == pytest.approx(100.0)


def test_cancel_requested_during_merge_stops_the_task(tmp_path):
    """进入合并阶段后收到取消信号，必须中止任务且不得报告成功。"""
    session = make_session()
    cancel = threading.Event()
    reached_merge = {"value": False}

    def on_progress(report):
        # 合并阶段一开始就把取消信号置位
        if report.stage is TaskStage.MERGING:
            reached_merge["value"] = True
            cancel.set()

    with pytest.raises(DownloadCancelled):
        downloader.download_hls_video(
            PLAYLIST_URL,
            tmp_path / "out.ts",
            session=session,
            cancel_event=cancel,
            progress_callback=on_progress,
        )

    assert reached_merge["value"], "没有进入合并阶段，测试前提不成立"
    assert not (tmp_path / "out.ts").exists()
    assert not list(tmp_path.glob("*.part")), "取消后不应残留 .part 临时文件"


# --------------------------------------------------------------------------
# 整段 AES-128 解密
# --------------------------------------------------------------------------

_AES_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
_OTHER_KEY = bytes.fromhex("ffeeddccbbaa99887766554433221100")
_KEY_URL = "https://key.test/k"
_EXPLICIT_IV_HEX = "0x" + "ab" * 16


def _encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    """按 HLS 的方式加密（测试自用）：PKCS7 填充 + AES-128-CBC。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from hls_downloader.crypto import AES_BLOCK_SIZE

    pad = AES_BLOCK_SIZE - (len(plain) % AES_BLOCK_SIZE)
    padded = plain + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _encrypted_session(
    names: list[str],
    *,
    media_sequence: int = 0,
    key: bytes = _AES_KEY,
    key_body: bytes | None = None,
    key_status: int = 200,
    explicit_iv: str | None = None,
) -> tuple[FakeSession, list[bytes]]:
    """构造一个整段 AES-128 加密的假会话，返回 (会话, 各分片明文)。"""
    plains = [f"plain-segment-{index}-".encode() * 4 + bytes([index]) for index in range(len(names))]

    tag = f'#EXT-X-KEY:METHOD=AES-128,URI="{_KEY_URL}"'
    if explicit_iv:
        tag += f",IV={explicit_iv}"
    text = build_media_playlist(names, prefix_lines=[tag])
    text = text.replace("#EXT-X-MEDIA-SEQUENCE:0", f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}")

    routes: dict[str, object] = {
        PLAYLIST_URL: FakeResponse(body=text),
        _KEY_URL: FakeResponse(
            status_code=key_status, body=(_AES_KEY if key_body is None else key_body)
        ),
    }
    iv_bytes = bytes.fromhex(_EXPLICIT_IV_HEX[2:]) if explicit_iv else None
    for index, (name, plain) in enumerate(zip(names, plains)):
        iv = iv_bytes or (media_sequence + index).to_bytes(16, "big")
        routes[f"{BASE}{name}"] = FakeResponse(body=_encrypt(plain, key, iv))

    return FakeSession(routes), plains


def test_aes128_playlist_is_downloaded_and_decrypted(tmp_path):
    session, plains = _encrypted_session(NAMES)

    result = downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert (tmp_path / "out.ts").read_bytes() == b"".join(plains)
    assert result.segment_count == 3


def test_short_ciphertext_is_retried_instead_of_decrypted(tmp_path):
    """密文被截断时不能硬解：长度对不上就该重试，而不是产出错误内容。"""
    session, plains = _encrypted_session(NAMES[:1])
    route = f"{BASE}{NAMES[0]}"
    cipher = session.routes[route].content
    session.routes[route] = [
        FakeResponse(body=cipher[:-16], headers={"Content-Length": str(len(cipher))}),
        FakeResponse(body=cipher, headers={"Content-Length": str(len(cipher))}),
    ]

    downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.ts", session=session, max_retries=1
    )

    assert (tmp_path / "out.ts").read_bytes() == b"".join(plains)


def test_key_is_requested_only_once_for_the_whole_playlist(tmp_path):
    session, _ = _encrypted_session(NAMES)

    downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert session.requested_urls.count(_KEY_URL) == 1


def test_explicit_iv_from_playlist_is_honoured(tmp_path):
    """播放列表显式给出 IV 时，必须用它而不是媒体序号。"""
    session, plains = _encrypted_session(NAMES, explicit_iv=_EXPLICIT_IV_HEX)

    downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert (tmp_path / "out.ts").read_bytes() == b"".join(plains)


def test_default_iv_follows_the_media_sequence(tmp_path):
    """未给出 IV 时，媒体序号必须参与推导，否则解出来就是垃圾。"""
    session, plains = _encrypted_session(NAMES, media_sequence=42)

    downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert (tmp_path / "out.ts").read_bytes() == b"".join(plains)


def test_wrong_key_fails_loudly_and_leaves_no_output(tmp_path):
    """密钥不对时必须失败，绝不能产出"看起来成功"的垃圾文件。"""
    session, _ = _encrypted_session(NAMES, key=_OTHER_KEY)

    with pytest.raises(SegmentDecryptionError):
        downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert not (tmp_path / "out.ts").exists()


def test_missing_key_file_reports_clearly(tmp_path):
    session, _ = _encrypted_session(NAMES, key_status=404)

    with pytest.raises(SegmentDecryptionError) as excinfo:
        downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert "404" in excinfo.value.message
    assert not (tmp_path / "out.ts").exists()


def test_key_of_wrong_length_is_rejected(tmp_path):
    session, _ = _encrypted_session(NAMES, key_body=b"short")

    with pytest.raises(SegmentDecryptionError) as excinfo:
        downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert "16" in excinfo.value.message


def test_unencrypted_playlist_never_requests_a_key(tmp_path):
    session = make_session()

    downloader.download_hls_video(PLAYLIST_URL, tmp_path / "out.ts", session=session)

    assert _KEY_URL not in session.requested_urls


def test_cancel_during_key_fetch_stops_the_task(tmp_path):
    """取消信号要能中止密钥获取阶段。"""
    session, _ = _encrypted_session(NAMES)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(DownloadCancelled):
        downloader.download_hls_video(
            PLAYLIST_URL, tmp_path / "out.ts", session=session, cancel_event=cancel
        )

    assert not (tmp_path / "out.ts").exists()


# --------------------------------------------------------------------------
# 进度语义
# --------------------------------------------------------------------------


def test_encrypted_fmp4_playlist_warns_about_the_init_segment(tmp_path):
    """AES-128 + fMP4 初始化分片：初始化分片不参与解密，必须明确提示。

    这类组合可能产出无法播放的文件，不能默默报成功。
    """
    import logging

    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    logger = logging.getLogger("test.encrypted_init")
    logger.setLevel(logging.DEBUG)
    logger.handlers = [_Capture()]
    logger.propagate = False

    playlist = build_media_playlist(
        ["seg0.m4s"],
        prefix_lines=[
            '#EXT-X-MAP:URI="init.mp4"',
            f'#EXT-X-KEY:METHOD=AES-128,URI="{_KEY_URL}"',
        ],
    )
    session = FakeSession(
        {
            PLAYLIST_URL: FakeResponse(body=playlist),
            _KEY_URL: FakeResponse(body=_AES_KEY),
            f"{BASE}init.mp4": FakeResponse(body=b"INIT"),
            f"{BASE}seg0.m4s": FakeResponse(
                body=_encrypt(b"DATA", _AES_KEY, (0).to_bytes(16, "big"))
            ),
        }
    )

    downloader.download_hls_video(
        PLAYLIST_URL, tmp_path / "out.mp4", session=session, logger=logger
    )

    assert any("初始化分片" in message for message in messages), messages


def test_progress_does_not_reach_100_before_completion(tmp_path):
    """分片下完 ≠ 任务完成：合并仍可能失败，进度条不该先走满。"""
    session = make_session()
    reports: list[tuple[TaskStage, float]] = []

    downloader.download_hls_video(
        PLAYLIST_URL,
        tmp_path / "out.ts",
        session=session,
        progress_callback=lambda report: reports.append((report.stage, report.percent)),
    )

    merging = [percent for stage, percent in reports if stage is TaskStage.MERGING]
    assert merging, "没有进入合并阶段，测试前提不成立"
    assert all(percent < 100.0 for percent in merging), f"合并阶段就显示 100%：{merging}"

    completed = [percent for stage, percent in reports if stage is TaskStage.COMPLETED]
    assert completed, "没有收到完成阶段的进度"
    assert completed[-1] == pytest.approx(100.0), "真正完成时必须显示 100%"
