"""核心下载流程：获取播放列表 → 解析 → 下载分片 → 合并 → 返回结果。

本模块不依赖 Tkinter / CustomTkinter，可以独立调用，也可以用 Mock 完整测试。
所有网络访问都通过 ``requests.Session`` 完成，测试时可以注入假 Session。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from . import crypto, merger
from .models import (
    DownloadCancelled,
    DownloadOptions,
    DownloadResult,
    EncryptionKey,
    HLSDownloaderError,
    MergeError,
    OutputExistsError,
    PlaylistFetchError,
    PlaylistInfo,
    ProgressReport,
    SegmentDecryptionError,
    SegmentDownloadError,
    SegmentKind,
    TaskStage,
)
from .playlist import parse_playlist
from .url_utils import get_extension, redact_url, validate_http_url

__all__ = ["download_hls_video", "ProgressCallback", "HTTP_TIMEOUT"]

#: 进度回调：接收一个 :class:`~hls_downloader.models.ProgressReport`
ProgressCallback = Callable[[ProgressReport], None]

CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0
#: (连接超时, 读取超时)
HTTP_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)

DOWNLOAD_CHUNK_SIZE = 64 * 1024
DEFAULT_MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.0
MAX_RETRY_DELAY_SECONDS = 8.0

SPEED_WINDOW_SECONDS = 5.0
PROGRESS_EMIT_INTERVAL_SECONDS = 0.1
#: 任务真正完成前对外显示的最大进度。分片下完不等于任务完成——合并仍可能
#: 失败，进度条不该先走满再报错。
MAX_PERCENT_BEFORE_COMPLETION = 99.0

USER_AGENT = "KakaCat-HLSDownloader/1.0"


def download_hls_video(
    playlist_url: str,
    output_path: str | os.PathLike[str],
    optional_a: str | None = None,
    optional_b: str | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    *,
    session: requests.Session | None = None,
    overwrite: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    temp_dir: str | os.PathLike[str] | None = None,
    keep_temp_on_failure: bool = False,
    ffmpeg_path: str | None = None,
    logger: logging.Logger | None = None,
) -> DownloadResult:
    """下载用户有权访问的未加密 HLS VOD 视频。

    Args:
        playlist_url: 媒体播放列表（media playlist）URL。
        output_path: 最终视频的输出路径，包含文件名。
        optional_a: 预留参数 A。当前版本只接收并原样保存，不参与任何请求。
        optional_b: 预留参数 B。当前版本只接收并原样保存，不参与任何请求。
        progress_callback: 可选进度回调，接收
            :class:`~hls_downloader.models.ProgressReport`。
        cancel_event: 可选取消信号（``threading.Event``）；置位后当前请求会
            尽快中止，并抛出 :class:`~hls_downloader.models.DownloadCancelled`。
        session: 可选的 ``requests.Session``，用于复用连接或注入登录态。
        overwrite: 是否允许覆盖已存在的输出文件，默认 ``False``。
        max_retries: 单个请求失败后的最大重试次数。
        temp_dir: 存放分片临时文件的父目录，默认使用系统临时目录。
        keep_temp_on_failure: 失败或取消时是否保留临时分片，便于排查问题。
        ffmpeg_path: FFmpeg 可执行文件路径；需要重新封装时使用。
        logger: 可选日志记录器。

    Returns:
        :class:`~hls_downloader.models.DownloadResult`，其中
        ``output_path`` 是成功生成的最终视频文件路径。

    Raises:
        InvalidUrlError: URL 为空、协议不支持或格式错误。
        OutputExistsError: 输出文件已存在且 ``overwrite`` 为 ``False``。
        PlaylistFetchError: 获取播放列表时的网络错误或 HTTP 错误。
        MasterPlaylistError: 传入的是主播放列表。
        LivePlaylistError: 播放列表是直播或未完成。
        UnsupportedEncryptionError: 播放列表包含加密分片。
        UnsupportedSegmentError: 分片格式当前版本不支持。
        SegmentDownloadError: 某个分片在重试后仍然下载失败。
        MergeError: 合并或封装失败。
        DownloadCancelled: 用户取消了任务。
        HLSDownloaderError: 其他下载器错误（如输出目录不可写）。
    """
    log = logger or logging.getLogger("hls_downloader")

    url = validate_http_url(playlist_url)
    target = _prepare_output(output_path, overwrite=overwrite)

    # 预留参数：只记录“收到”，绝不拼接进 URL、请求头、Cookie 或请求参数。
    options = DownloadOptions(optional_a=optional_a, optional_b=optional_b).normalized()
    if not options.is_empty:
        log.debug("已接收预留参数（当前版本不处理，也不会发送到任何请求中）。")

    cancel = cancel_event if cancel_event is not None else threading.Event()
    owns_session = session is None
    active_session = session or requests.Session()
    if owns_session:
        active_session.headers.setdefault("User-Agent", USER_AGENT)

    try:
        return _run(
            session=active_session,
            url=url,
            target=target,
            cancel=cancel,
            progress_callback=progress_callback,
            max_retries=max(0, int(max_retries)),
            temp_dir=temp_dir,
            keep_temp_on_failure=keep_temp_on_failure,
            ffmpeg_path=ffmpeg_path,
            log=log,
        )
    finally:
        if owns_session:
            active_session.close()


# --------------------------------------------------------------------------
# 内部实现
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _DownloadItem:
    """一个需要下载到本地的文件（分片或 fMP4 初始化分片）。

    ``key`` 不为空时表示该分片使用标准整段 AES-128 加密，``iv`` 是已经按
    RFC 8216 推导好的 16 字节初始向量。
    """

    url: str
    filename: str
    key: EncryptionKey | None = None
    iv: bytes | None = None


class _RetryableSegmentError(Exception):
    """内部使用：表示该分片可以重试。"""


def _run(
    *,
    session: requests.Session,
    url: str,
    target: Path,
    cancel: threading.Event,
    progress_callback: ProgressCallback | None,
    max_retries: int,
    temp_dir: str | os.PathLike[str] | None,
    keep_temp_on_failure: bool,
    ffmpeg_path: str | None,
    log: logging.Logger,
) -> DownloadResult:
    started = time.monotonic()
    progress = _ProgressReporter(progress_callback, log)

    try:
        progress.set_stage(TaskStage.FETCHING_PLAYLIST, "正在获取播放列表…")
        log.info("正在获取播放列表：%s", redact_url(url))
        text = _request_text(
            session, url, cancel=cancel, max_retries=max_retries, log=log, what="获取播放列表"
        )

        _check_cancel(cancel)
        progress.set_stage(TaskStage.PARSING_PLAYLIST, "正在解析播放列表…")
        playlist = parse_playlist(text, url)

        log.info(
            "播放列表解析完成：%d 个分片，类型 %s，总时长约 %.1f 秒。",
            playlist.segment_count,
            playlist.segment_kind.label,
            playlist.total_duration,
        )
        for warning in playlist.warnings:
            log.warning("提示：%s", warning)

        items = _build_items(playlist)
        progress.set_total(len(items))
        progress.set_stage(
            TaskStage.DOWNLOADING_SEGMENTS, f"开始下载，共 {len(items)} 个分片。"
        )

        if playlist.encryption_method and playlist.encryption_method != "NONE":
            log.info("播放列表使用 %s 整段加密，将在下载后逐片解密。", playlist.encryption_method)
            if playlist.init_segment_url:
                # 初始化分片不参与解密（它的 IV 由 #EXT-X-KEY 的显式 IV 决定，
                # 而这条流未必给出）。这种情况可能产出无法播放的文件，必须说明，
                # 不能默默报成功。
                log.warning(
                    "该加密播放列表同时包含 fMP4 初始化分片（#EXT-X-MAP）："
                    "初始化分片不参与解密，若产物无法播放，请改用对应的 TS 版本。"
                )

        temp_path = _create_temp_dir(temp_dir)
        succeeded = False
        try:
            local_paths = _download_all(
                session=session,
                items=items,
                temp_path=temp_path,
                cancel=cancel,
                max_retries=max_retries,
                progress=progress,
                log=log,
            )

            _check_cancel(cancel)
            progress.set_stage(TaskStage.MERGING, "正在合并视频…")
            output_size = _merge_into_place(
                local_paths=local_paths,
                target=target,
                segment_kind=playlist.segment_kind,
                ffmpeg_path=ffmpeg_path,
                cancel_event=cancel,
                log=log,
            )

            elapsed = time.monotonic() - started
            progress.set_stage(TaskStage.COMPLETED, f"下载完成：{target.name}")
            log.info(
                "任务完成：%s（%d 个分片，%d 字节，用时 %.1f 秒）。",
                target.name,
                len(items),
                output_size,
                elapsed,
            )
            succeeded = True
            return DownloadResult(
                output_path=str(target),
                segment_count=len(items),
                total_bytes=output_size,
                elapsed_seconds=elapsed,
                segment_kind=playlist.segment_kind,
                warnings=playlist.warnings,
            )
        finally:
            if succeeded or not keep_temp_on_failure:
                shutil.rmtree(temp_path, ignore_errors=True)
            else:
                log.warning("按设置保留临时分片目录：%s", temp_path)

    except DownloadCancelled:
        progress.set_stage(TaskStage.CANCELLED, "任务已取消。")
        log.warning("任务已取消。")
        raise
    except HLSDownloaderError as exc:
        progress.set_stage(TaskStage.FAILED, exc.message)
        log.error("任务失败：%s", exc.message)
        raise
    except OSError as exc:
        message = f"文件操作失败：{exc}"
        progress.set_stage(TaskStage.FAILED, message)
        log.error("任务失败：%s", message)
        raise HLSDownloaderError(message) from exc


def _download_all(
    *,
    session: requests.Session,
    items: list[_DownloadItem],
    temp_path: Path,
    cancel: threading.Event,
    max_retries: int,
    progress: "_ProgressReporter",
    log: logging.Logger,
) -> list[Path]:
    """按顺序下载所有分片，返回本地文件路径列表。"""
    local_paths: list[Path] = []
    total = len(items)
    key_cache = _KeyCache(session=session, cancel=cancel, max_retries=max_retries, log=log)

    for position, item in enumerate(items, start=1):
        _check_cancel(cancel)
        destination = temp_path / item.filename
        progress.set_message(f"正在下载第 {position}/{total} 个分片…")
        _download_one(
            session=session,
            item=item,
            destination=destination,
            cancel=cancel,
            max_retries=max_retries,
            progress=progress,
            log=log,
            position=position,
            total=total,
            key_cache=key_cache,
        )
        local_paths.append(destination)
        progress.finish_item()

    return local_paths


def _download_one(
    *,
    session: requests.Session,
    item: _DownloadItem,
    destination: Path,
    cancel: threading.Event,
    max_retries: int,
    progress: "_ProgressReporter",
    log: logging.Logger,
    position: int,
    total: int,
    key_cache: "_KeyCache",
) -> int:
    """下载单个分片，失败时按策略重试；重试耗尽后抛出异常。"""
    last_error: Exception | None = None

    # 密钥放在重试循环之外获取：同一个播放列表通常所有分片共用一个密钥，
    # 取一次即可（_KeyCache 内部还有按 URI 的缓存）。
    segment_key = key_cache.get(item.key.uri) if item.key is not None else None

    for attempt in range(max_retries + 1):
        _check_cancel(cancel)
        try:
            response = session.get(
                item.url,
                timeout=HTTP_TIMEOUT,
                stream=True,
                allow_redirects=True,
            )
        except requests.Timeout:
            last_error = _RetryableSegmentError("请求超时")
        except requests.RequestException as exc:
            last_error = _RetryableSegmentError(exc.__class__.__name__)
        else:
            with response:
                status = response.status_code
                if status == 200:
                    try:
                        return _write_stream(
                            response,
                            destination,
                            cancel,
                            progress,
                            key=segment_key,
                            iv=item.iv,
                        )
                    except (requests.RequestException, _RetryableSegmentError) as exc:
                        last_error = exc
                    except OSError as exc:
                        _remove_quietly(destination)
                        raise SegmentDownloadError(
                            f"写入分片文件失败：{exc}", segment_index=position
                        ) from exc
                elif 400 <= status < 500 and status != 429:
                    _remove_quietly(destination)
                    raise SegmentDownloadError(
                        f"下载第 {position}/{total} 个分片失败：服务器返回 HTTP {status}。",
                        segment_index=position,
                    )
                else:
                    last_error = _RetryableSegmentError(f"服务器返回 HTTP {status}")

        _remove_quietly(destination)
        if attempt < max_retries:
            _wait_before_retry(
                attempt=attempt,
                cancel=cancel,
                log=log,
                position=position,
                total=total,
                error=last_error,
            )

    raise SegmentDownloadError(
        f"下载第 {position}/{total} 个分片失败（已重试 {max_retries} 次）：{last_error}",
        segment_index=position,
    )


def _write_stream(
    response: requests.Response,
    destination: Path,
    cancel: threading.Event,
    progress: "_ProgressReporter",
    *,
    key: bytes | None = None,
    iv: bytes | None = None,
) -> int:
    """把响应体写入磁盘。

    未加密时直接流式落盘，不把分片读进内存；加密分片则需要先缓冲**这一个
    分片**的密文，再整体解密——AES-128-CBC 要拿到末尾才能校验并去掉 PKCS7
    填充。缓冲的是单个分片（通常几百 KB），不是整个视频。
    """
    expected = _content_length(response)
    progress.begin_item(expected)

    if key is None:
        written = 0
        try:
            with open(destination, "wb") as handle:
                for chunk in response.iter_content(DOWNLOAD_CHUNK_SIZE):
                    _check_cancel(cancel)
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    progress.add_bytes(len(chunk))
        except DownloadCancelled:
            _remove_quietly(destination)
            raise

        if written == 0:
            _remove_quietly(destination)
            raise _RetryableSegmentError("服务器返回了空的分片内容")

        return written

    # ---- 加密分片 ----
    buffer = bytearray()
    try:
        for chunk in response.iter_content(DOWNLOAD_CHUNK_SIZE):
            _check_cancel(cancel)
            if not chunk:
                continue
            buffer.extend(chunk)
            progress.add_bytes(len(chunk))
    except DownloadCancelled:
        _remove_quietly(destination)
        raise

    if not buffer:
        _remove_quietly(destination)
        raise _RetryableSegmentError("服务器返回了空的分片内容")

    if iv is None:
        _remove_quietly(destination)
        raise SegmentDecryptionError("加密分片缺少初始向量（IV），无法解密。")

    try:
        plain = crypto.decrypt_segment(bytes(buffer), key, iv)
    except SegmentDecryptionError:
        _remove_quietly(destination)
        raise

    try:
        destination.write_bytes(plain)
    except OSError:
        _remove_quietly(destination)
        raise

    return len(plain)


class _KeyCache:
    """按 URI 获取并缓存 AES-128 密钥。

    密钥地址由播放列表自己给出，访问权限与分片一致；这里不猜测、不派生，
    也不会去任何许可证服务器索取密钥。
    """

    def __init__(
        self,
        *,
        session: requests.Session,
        cancel: threading.Event,
        max_retries: int,
        log: logging.Logger,
    ) -> None:
        self._session = session
        self._cancel = cancel
        self._max_retries = max(0, int(max_retries))
        self._log = log
        self._keys: dict[str, bytes] = {}

    def get(self, uri: str) -> bytes:
        """返回 16 字节密钥；同一个 URI 只会真正请求一次。"""
        cached = self._keys.get(uri)
        if cached is not None:
            return cached

        key = self._fetch(uri)
        self._keys[uri] = key
        return key

    def _fetch(self, uri: str) -> bytes:
        last_error: SegmentDecryptionError | None = None

        for attempt in range(self._max_retries + 1):
            _check_cancel(self._cancel)
            try:
                response = self._session.get(
                    uri, timeout=HTTP_TIMEOUT, allow_redirects=True
                )
            except requests.Timeout:
                last_error = SegmentDecryptionError("获取解密密钥超时。")
            except requests.RequestException as exc:
                last_error = SegmentDecryptionError(
                    f"获取解密密钥失败：{exc.__class__.__name__}"
                )
            else:
                with response:
                    status = response.status_code
                    if status == 200:
                        key = response.content
                        if len(key) != crypto.AES_BLOCK_SIZE:
                            raise SegmentDecryptionError(
                                f"解密密钥长度应为 {crypto.AES_BLOCK_SIZE} 字节，"
                                f"实际为 {len(key)} 字节。"
                            )
                        self._log.info("已获取 AES-128 解密密钥：%s", redact_url(uri))
                        return key
                    if 400 <= status < 500 and status != 429:
                        raise SegmentDecryptionError(
                            f"获取解密密钥失败：服务器返回 HTTP {status}。"
                        )
                    last_error = SegmentDecryptionError(
                        f"获取解密密钥失败：服务器返回 HTTP {status}。"
                    )

            if attempt < self._max_retries:
                delay = min(RETRY_BACKOFF_SECONDS * (2**attempt), MAX_RETRY_DELAY_SECONDS)
                self._log.warning("获取解密密钥失败（%s），%.1f 秒后重试。", last_error, delay)
                if self._cancel.wait(delay):
                    raise DownloadCancelled("任务已取消。")

        assert last_error is not None
        raise last_error


def _merge_into_place(
    *,
    local_paths: list[Path],
    target: Path,
    segment_kind: SegmentKind,
    ffmpeg_path: str | None,
    cancel_event: threading.Event | None,
    log: logging.Logger,
) -> int:
    """先合并到同目录的 ``.part`` 文件，成功后原子替换到目标路径。

    这样即使合并失败，也不会在目标路径留下不完整的视频文件，
    更不会破坏用户原有的同名文件。

    合并期间同样会响应 ``cancel_event``：被取消时清理 ``.part`` 文件，
    并向上抛出 :class:`~hls_downloader.models.DownloadCancelled`。
    """
    staging = target.with_name(f"{target.name}.part")
    _remove_quietly(staging)

    try:
        merger.merge_segments(
            local_paths,
            staging,
            segment_kind,
            container_suffix=target.suffix,
            ffmpeg_path=ffmpeg_path,
            cancel_event=cancel_event,
            logger=log,
        )
        if not staging.is_file() or staging.stat().st_size == 0:
            raise MergeError("合并结束后没有生成有效的输出文件。")

        size = staging.stat().st_size
        try:
            os.replace(staging, target)
        except OSError as exc:
            raise MergeError(f"无法把合并结果写入目标路径：{exc}") from exc
    except BaseException:
        _remove_quietly(staging)
        raise

    if not target.is_file() or target.stat().st_size == 0:
        raise MergeError("输出文件校验失败：文件不存在或内容为空。")

    return size


def _request_text(
    session: requests.Session,
    url: str,
    *,
    cancel: threading.Event,
    max_retries: int,
    log: logging.Logger,
    what: str,
) -> str:
    """带有限重试的 GET 请求，返回响应文本。"""
    last_error: PlaylistFetchError | None = None

    for attempt in range(max_retries + 1):
        _check_cancel(cancel)
        try:
            response = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        except requests.Timeout:
            last_error = PlaylistFetchError(f"{what}超时，请检查网络后重试。")
        except requests.RequestException as exc:
            last_error = PlaylistFetchError(f"{what}失败：{exc.__class__.__name__}")
        else:
            with response:
                if response.status_code == 200:
                    if not response.encoding or response.encoding.lower() == "iso-8859-1":
                        response.encoding = "utf-8"
                    return response.text
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise PlaylistFetchError(
                        f"{what}失败：服务器返回 HTTP {response.status_code}。"
                    )
                last_error = PlaylistFetchError(
                    f"{what}失败：服务器返回 HTTP {response.status_code}。"
                )

        if attempt < max_retries:
            delay = min(RETRY_BACKOFF_SECONDS * (2**attempt), MAX_RETRY_DELAY_SECONDS)
            log.warning("%s失败（%s），%.1f 秒后重试。", what, last_error, delay)
            if cancel.wait(delay):
                raise DownloadCancelled("任务已取消。")

    assert last_error is not None
    raise last_error


def _build_items(playlist: PlaylistInfo) -> list[_DownloadItem]:
    """把播放列表转换成待下载文件列表（fMP4 时第一项是初始化分片）。"""
    items: list[_DownloadItem] = []

    if playlist.init_segment_url and playlist.segment_kind is SegmentKind.FMP4:
        # 初始化分片按原样下载：它不参与整段 AAC/H.264 解密，IV 也无从推导。
        items.append(_DownloadItem(url=playlist.init_segment_url, filename="000000-init.mp4"))

    offset = len(items)
    for segment in playlist.segments:
        suffix = get_extension(segment.url) or ".bin"
        key = segment.key if (segment.key and segment.key.is_encrypted) else None
        iv = crypto.build_iv(key, playlist.media_sequence, segment.index) if key else None
        items.append(
            _DownloadItem(
                url=segment.url,
                filename=f"{segment.index + offset:06d}{suffix}",
                key=key,
                iv=iv,
            )
        )

    return items


def _prepare_output(output_path: str | os.PathLike[str], *, overwrite: bool) -> Path:
    """校验输出路径：目录存在且可写、不覆盖已有文件。"""
    raw = str(output_path or "").strip()
    if not raw:
        raise HLSDownloaderError("输出文件路径不能为空。")

    target = Path(raw).expanduser()
    if target.exists() and target.is_dir():
        raise HLSDownloaderError(f"输出路径是一个目录，请指定具体的文件名：{target}")

    if target.exists() and not overwrite:
        raise OutputExistsError(
            f"文件已存在：{target}\n如需覆盖，请勾选“允许覆盖已有文件”或修改输出文件名。"
        )

    parent = target.parent if str(target.parent) else Path(".")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HLSDownloaderError(f"无法创建输出目录 {parent}：{exc}") from exc

    if not os.access(parent, os.W_OK):
        raise HLSDownloaderError(f"输出目录没有写入权限：{parent}")

    return target


def _create_temp_dir(temp_dir: str | os.PathLike[str] | None) -> Path:
    """在指定父目录（默认系统临时目录）下创建一个唯一的临时分片目录。"""
    parent: Path | None = None
    if temp_dir:
        parent = Path(temp_dir).expanduser()
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HLSDownloaderError(f"无法创建临时目录 {parent}：{exc}") from exc

    try:
        return Path(
            tempfile.mkdtemp(
                prefix="hls_segments_",
                dir=str(parent) if parent else None,
            )
        )
    except OSError as exc:
        raise HLSDownloaderError(f"无法创建临时目录：{exc}") from exc


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("任务已取消。")


def _wait_before_retry(
    *,
    attempt: int,
    cancel: threading.Event,
    log: logging.Logger,
    position: int,
    total: int,
    error: Exception | None,
) -> None:
    delay = min(RETRY_BACKOFF_SECONDS * (2**attempt), MAX_RETRY_DELAY_SECONDS)
    log.warning(
        "第 %d/%d 个分片下载失败（%s），%.1f 秒后重试。",
        position,
        total,
        error,
        delay,
    )
    # 用 Event.wait 代替 sleep，取消时能立刻返回。
    if cancel.wait(delay):
        raise DownloadCancelled("任务已取消。")


def _content_length(response: requests.Response) -> int | None:
    value = response.headers.get("Content-Length")
    if not value:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length > 0 else None


def _remove_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


class _ProgressReporter:
    """把真实的下载统计转换成 :class:`ProgressReport`。

    速度来自最近 :data:`SPEED_WINDOW_SECONDS` 秒的实际字节数，
    剩余时间来自已完成分片的平均耗时；数据不足时不会给出估计值。
    """

    def __init__(self, callback: ProgressCallback | None, log: logging.Logger) -> None:
        self._callback = callback
        self._log = log
        self._total = 0
        self._completed = 0
        self._stage = TaskStage.IDLE
        self._message = ""
        self._window: deque[tuple[float, int]] = deque()
        self._window_bytes = 0
        self._total_bytes = 0
        self._item_bytes = 0
        self._item_expected: int | None = None
        self._download_started: float | None = None
        self._last_emit = 0.0
        self._max_percent = 0.0

    def set_total(self, total: int) -> None:
        self._total = max(0, total)

    def set_message(self, message: str) -> None:
        self._message = message

    def set_stage(self, stage: TaskStage, message: str = "") -> None:
        self._stage = stage
        self._message = message
        if stage is TaskStage.DOWNLOADING_SEGMENTS and self._download_started is None:
            self._download_started = time.monotonic()
        self.emit(force=True)

    def begin_item(self, expected_bytes: int | None) -> None:
        self._item_bytes = 0
        self._item_expected = expected_bytes
        if self._download_started is None:
            self._download_started = time.monotonic()

    def add_bytes(self, count: int) -> None:
        now = time.monotonic()
        self._total_bytes += count
        self._item_bytes += count
        self._window.append((now, count))
        self._window_bytes += count
        self._trim_window(now)
        self.emit()

    def finish_item(self) -> None:
        self._completed += 1
        self._item_bytes = 0
        self._item_expected = None
        self.emit(force=True)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def emit(self, *, force: bool = False) -> None:
        if self._callback is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < PROGRESS_EMIT_INTERVAL_SECONDS:
            return
        self._last_emit = now

        percent = self._percent()
        # 完成前封顶在 99%：分片下完不代表任务完成，合并仍可能失败。
        if self._stage is not TaskStage.COMPLETED and percent > MAX_PERCENT_BEFORE_COMPLETION:
            percent = MAX_PERCENT_BEFORE_COMPLETION
        # 分片重试会丢弃上一次尝试写入的数据，_percent() 因而可能回落；
        # 对外呈现的进度必须单调不减，否则界面上的进度条会往回跳。
        if percent > self._max_percent:
            self._max_percent = percent
        else:
            percent = self._max_percent

        try:
            self._callback(
                ProgressReport(
                    stage=self._stage,
                    completed_segments=self._completed,
                    total_segments=self._total,
                    percent=percent,
                    speed_bps=self._speed(),
                    eta_seconds=self._eta(),
                    message=self._message,
                )
            )
        except Exception:  # noqa: BLE001 - 回调异常不应中断下载
            self._log.exception("进度回调执行失败，已忽略该次更新。")

    def _trim_window(self, now: float) -> None:
        while self._window and now - self._window[0][0] > SPEED_WINDOW_SECONDS:
            _, count = self._window.popleft()
            self._window_bytes -= count

    def _percent(self) -> float:
        if self._total <= 0:
            return 0.0
        partial = 0.0
        if self._item_expected:
            partial = min(self._item_bytes / self._item_expected, 1.0)
        return min(100.0, (self._completed + partial) / self._total * 100.0)

    def _speed(self) -> float:
        if not self._window:
            return 0.0
        span = max(self._window[-1][0] - self._window[0][0], 0.5)
        return self._window_bytes / span

    def _eta(self) -> float | None:
        remaining = self._total - self._completed
        if remaining <= 0 or self._completed < 2 or self._download_started is None:
            return None
        elapsed = time.monotonic() - self._download_started
        if elapsed <= 0:
            return None
        return elapsed / self._completed * remaining
