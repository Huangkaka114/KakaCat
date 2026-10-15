"""核心下载流程：获取播放列表 → 解析 → 下载分片 → 合并 → 返回结果。

本模块不依赖 Tkinter / CustomTkinter，可以独立调用，也可以用 Mock 完整测试。
所有网络访问都通过 ``requests.Session`` 完成，测试时可以注入假 Session。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from .url_utils import get_extension, redact_url, validate_http_url, without_query
from .util import process_optional_params, rebuild_url


__all__ = [
    "download_hls_video",
    "prepare_playlist_url",
    "create_session",
    "request_text",
    "ProgressCallback",
    "HTTP_TIMEOUT",
]

#: 进度回调：接收一个 :class:`~hls_downloader.models.ProgressReport`
ProgressCallback = Callable[[ProgressReport], None]

CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0
#: (连接超时, 读取超时)
HTTP_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)

DOWNLOAD_CHUNK_SIZE = 64 * 1024
#: 默认并发下载的分片数。顺序下载在一千多个分片的片源上会把大量时间花在
#: 等待往返上；4 路并发通常能明显提速，又不会给服务器太大压力。
DEFAULT_WORKERS = 4
MAX_WORKERS = 16
#: 续传索引文件名：记录已经完整下载的分片，供下次任务跳过。
RESUME_INDEX_NAME = ".hls_resume.tsv"
#: 单个请求失败后的默认重试次数。长任务里连接被中途掐断很常见
#: （``IncompleteRead`` / ``Connection broken``），退避 1/2/4/8 秒共 4 次，
#: 单个分片最多多花约 15 秒，远好过让整条任务作废。
DEFAULT_MAX_RETRIES = 4
#: 重试前的退避基数：0.5/1/2/4 秒。连接被掐断这类瞬断通常立刻重连就好，
#: 等太久纯属浪费——重试期间这个工作线程是空转的。
RETRY_BACKOFF_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 8.0

SPEED_WINDOW_SECONDS = 5.0
PROGRESS_EMIT_INTERVAL_SECONDS = 0.1
#: 任务真正完成前对外显示的最大进度。分片下完不等于任务完成——合并仍可能
#: 失败，进度条不该先走满再报错。
MAX_PERCENT_BEFORE_COMPLETION = 99.0

USER_AGENT = "KakaCat/2.0"


def create_session(*, workers: int = DEFAULT_WORKERS) -> requests.Session:
    """创建一个带默认请求头的会话。

    下载与在线播放共用这一个工厂，保证两条路径的请求特征（User-Agent、
    连接池大小）一致；``workers`` 只影响连接池大小。
    """
    session = requests.Session()
    # 必须直接赋值：``requests.Session()`` 初始化时就带了默认的
    # ``python-requests/…``，用 ``setdefault`` 永远覆盖不掉，我们的 UA 会一直
    # 发不出去。
    session.headers["User-Agent"] = USER_AGENT
    _configure_connection_pool(session, workers)
    return session


def prepare_playlist_url(
    playlist_url: str,
    optional_a: str | None = None,
    optional_b: str | None = None,
    *,
    logger: logging.Logger | None = None,
) -> str:
    """校验用户填写的链接，并按可选参数重建真正的播放列表请求 URL。

    下载与在线播放共用这一个入口，保证两条路径对 URL 的处理完全一致：
    先做 :func:`~hls_downloader.url_utils.validate_http_url` 校验，再把参数 A/B
    归一化后交给 :func:`~hls_downloader.util.rebuild_url` 做本地签名。

    Args:
        playlist_url: 用户填写的链接（媒体播放列表地址，或填了参数 B 时的下载链接）。
        optional_a: 参数 A。填写时视为授权密钥，只参与本地签名计算。
        optional_b: 参数 B。填写时视为基础地址，用于从下载链接推导真实地址。
        logger: 可选日志记录器。

    Returns:
        实际应当请求的播放列表 URL。

    Raises:
        InvalidUrlError: URL 为空、协议不支持或格式错误。
    """
    log = logger or logging.getLogger("hls_downloader")

    url = validate_http_url(playlist_url)

    # 预留参数：先归一化，再交给 util 工具包处理。
    options = DownloadOptions(optional_a=optional_a, optional_b=optional_b).normalized()
    processed_a, processed_b = process_optional_params(options.optional_a, options.optional_b)
    # util 返回后再归一化一次：当前是恒等操作，将来 util 若返回带空白或空串的值
    # 也能被正确收敛。
    options = DownloadOptions(optional_a=processed_a, optional_b=processed_b).normalized()

    if options.optional_a:
        # 参数 A 是授权密钥，参数 B 是基础地址：重建播放列表请求 URL。
        # 密钥只参与本地签名计算，因此日志里只给出脱敏后的 URL，
        # 不打印签名与密钥本身。填了 B 时，url 是下载链接，由模板推导真实地址。
        url = rebuild_url(url, options.optional_a, base=options.optional_b)
        log.debug("已按参数 A（和参数 B）重建播放列表请求 URL：%s", redact_url(url))
    elif options.optional_b:
        log.warning(
            "填写了参数 B（基础地址）但没有参数 A（密钥），无法推导真实地址，"
            "本次按原始链接请求。"
        )

    return url


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
    workers: int = DEFAULT_WORKERS,
    resume: bool = True,
    temp_dir: str | os.PathLike[str] | None = None,
    keep_temp_on_failure: bool = False,
    ffmpeg_path: str | None = None,
    logger: logging.Logger | None = None,
) -> DownloadResult:
    """下载用户有权访问的未加密 HLS VOD 视频。

    Args:
        playlist_url: 媒体播放列表（media playlist）URL。
        output_path: 最终视频的输出路径，包含文件名。
        optional_a: 预留参数 A。填写时视为授权密钥：发起请求前会用它按
            ``wsSecret`` / ``wsTime`` 规则重建**播放列表**请求 URL。密钥不写入
            日志，也不会出现在分片、密钥等其它请求中。留空则按原链接请求。
        optional_b: 预留参数 B。填写时视为该服务的基础地址，与参数 A 一起按
            固定模板从 ``playlist_url``（此时是下载链接）推导真实媒体播放列表
            地址；单独填写（没有参数 A）时不起作用。
        progress_callback: 可选进度回调，接收
            :class:`~hls_downloader.models.ProgressReport`。
        cancel_event: 可选取消信号（``threading.Event``）；置位后当前请求会
            尽快中止，并抛出 :class:`~hls_downloader.models.DownloadCancelled`。
        session: 可选的 ``requests.Session``，用于复用连接或注入登录态。
        overwrite: 是否允许覆盖已存在的输出文件，默认 ``False``。
        max_retries: 单个请求失败后的最大重试次数。
        workers: 并发下载的分片数，取值 1~:data:`MAX_WORKERS`；``1`` 表示顺序下载。
        resume: 是否启用断点续传。开启时临时分片目录按链接固定命名，失败或取消
            后保留，再次执行同一任务会跳过已经下好的分片；``False`` 表示每次都
            从头下载，失败时按 ``keep_temp_on_failure`` 决定是否保留临时文件。
        temp_dir: 存放分片临时文件的父目录，默认放在输出文件所在目录下。分片
            可能有几个 GB，放在输出盘上可以避免写满系统盘。
        keep_temp_on_failure: 未开启续传时，失败或取消是否保留临时分片。
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

    # URL 处理（校验 + 可选参数签名）走共用入口：在线播放调用的是同一个函数，
    # 两条路径对同一个链接算出的请求地址因此永远一致。
    url = prepare_playlist_url(playlist_url, optional_a, optional_b, logger=log)
    target = _prepare_output(output_path, overwrite=overwrite)

    cancel = cancel_event if cancel_event is not None else threading.Event()
    owns_session = session is None
    active_session = session or create_session(workers=workers)

    try:
        return _run(
            session=active_session,
            url=url,
            target=target,
            cancel=cancel,
            progress_callback=progress_callback,
            max_retries=max(0, int(max_retries)),
            workers=min(max(1, int(workers)), MAX_WORKERS),
            resume=resume,
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
    workers: int,
    resume: bool,
    temp_dir: str | os.PathLike[str] | None,
    keep_temp_on_failure: bool,
    ffmpeg_path: str | None,
    log: logging.Logger,
) -> DownloadResult:
    started = time.monotonic()
    progress = _ProgressReporter(progress_callback, log, parallel=workers > 1)

    try:
        progress.set_stage(TaskStage.FETCHING_PLAYLIST, "正在获取播放列表…")
        log.info("正在获取播放列表：%s", redact_url(url))
        text = request_text(
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
            TaskStage.DOWNLOADING_SEGMENTS,
            f"开始下载，共 {len(items)} 个分片"
            + (f"（{workers} 路并发）…" if workers > 1 else "…"),
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

        # 续传目录的身份由三样东西决定：去掉查询串的地址（签名里的 wsTime 每次
        # 都变，不能参与）、最终输出路径、以及播放列表内容本身。三者一致才复用
        # 上一次的分片——片源换了（重新编码、换集）会落到新目录，不会把旧内容
        # 当成新内容拼进产物。
        temp_path = _create_temp_dir(
            temp_dir,
            identity=_resume_identity(url, target, text) if resume else None,
            default_parent=target.parent,
        )
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
                resume=resume,
                workers=workers,
            )

            _check_cancel(cancel)
            _log_transfer_summary(
                log=log,
                progress=progress,
                item_count=len(items),
                workers=workers,
                started=started,
            )
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
            if succeeded:
                shutil.rmtree(temp_path, ignore_errors=True)
            elif resume:
                log.warning(
                    "临时分片已保留，重新开始同一任务会从断点继续：%s", temp_path
                )
            elif keep_temp_on_failure:
                log.warning("按设置保留临时分片目录：%s", temp_path)
            else:
                shutil.rmtree(temp_path, ignore_errors=True)

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
    resume: bool,
    workers: int,
) -> list[Path]:
    """下载所有分片，返回与播放列表顺序一致的本地文件路径列表。

    已经完整下载过的分片（见 :class:`_ResumeIndex`）直接跳过；其余的按
    ``workers`` 路并发下载，但结果始终按播放列表顺序摆放，合并顺序不受影响。

    并发时同样是「全部成功才算成功」：任意一个分片最终失败，其余分片会尽快
    停下，整体抛出异常，不会产出残缺或顺序错乱的视频。
    """
    total = len(items)
    key_cache = _KeyCache(session=session, cancel=cancel, max_retries=max_retries, log=log)
    index = _ResumeIndex(temp_path / RESUME_INDEX_NAME, log) if resume else None

    local_paths: list[Path | None] = [None] * total
    pending: list[tuple[int, _DownloadItem, Path]] = []
    resumed = 0

    for position, item in enumerate(items):
        destination = temp_path / item.filename
        size = (
            index.matches(item.filename, without_query(item.url), destination)
            if index is not None
            else None
        )
        if size is None:
            pending.append((position, item, destination))
        else:
            local_paths[position] = destination
            progress.skip_item(size)
            resumed += 1

    if resumed:
        log.info(
            "断点续传：已跳过 %d/%d 个此前下载完成的分片，剩余 %d 个。",
            resumed,
            total,
            len(pending),
        )

    if pending:
        _download_pending(
            session=session,
            pending=pending,
            local_paths=local_paths,
            total=total,
            index=index,
            cancel=cancel,
            max_retries=max_retries,
            progress=progress,
            log=log,
            key_cache=key_cache,
            workers=workers,
        )

    return [path for path in local_paths if path is not None]


def _download_pending(
    *,
    session: requests.Session,
    pending: list[tuple[int, _DownloadItem, Path]],
    local_paths: list[Path | None],
    total: int,
    index: "_ResumeIndex | None",
    cancel: threading.Event,
    max_retries: int,
    progress: "_ProgressReporter",
    log: logging.Logger,
    key_cache: "_KeyCache",
    workers: int,
) -> None:
    """并发下载还没下好的分片；任一失败就停下并抛出对应异常。"""
    scope = _CancelScope(cancel)
    failures: list[BaseException] = []
    failures_lock = threading.Lock()

    def download(position: int, item: _DownloadItem, destination: Path) -> None:
        try:
            progress.set_message(f"正在下载第 {position + 1}/{total} 个分片…")
            written = _download_one(
                session=session,
                item=item,
                destination=destination,
                cancel=scope,
                max_retries=max_retries,
                progress=progress,
                log=log,
                position=position + 1,
                total=total,
                key_cache=key_cache,
            )
        except BaseException as exc:  # noqa: BLE001 - 统一汇总后再抛给调用方
            with failures_lock:
                failures.append(exc)
            scope.stop()  # 其它分片不必再白费力气
            return

        if index is not None:
            index.record(item.filename, without_query(item.url), written)
        local_paths[position] = destination
        progress.finish_item()

    executor = ThreadPoolExecutor(
        max_workers=min(workers, len(pending)), thread_name_prefix="hls-segment"
    )
    try:
        futures = [executor.submit(download, *task) for task in pending]
        for future in as_completed(futures):
            # 异常已在 download 内捕获并汇总，这里只是等它跑完。
            future.result()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    if not failures:
        return

    # 用户取消优先按取消处理；否则抛出真正的下载错误。
    for exc in failures:
        if not isinstance(exc, DownloadCancelled):
            raise exc
    raise DownloadCancelled("任务已取消。")


class _CancelScope:
    """把「用户取消」和「同批分片里有人失败」合成一个取消信号。

    分片下载器只认识 ``is_set()`` / ``wait()`` 两个方法，这里两者都提供，
    所以并发时任意一个分片失败，其余分片会像用户点了取消一样尽快收工。
    """

    def __init__(self, user_event: threading.Event) -> None:
        self._user = user_event
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def is_set(self) -> bool:
        return self._user.is_set() or self._stop.is_set()

    def wait(self, timeout: float) -> bool:
        """等待 ``timeout`` 秒；期间被取消则立刻返回 ``True``。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._stop.wait(min(remaining, 0.2)):
                return True


class _ResumeIndex:
    """续传索引：记录已经完整下载的分片，供下次任务跳过。

    每行是 ``文件名 <TAB> 分片地址（不含查询串） <TAB> 字节数``，只追加不重写，
    写完立即返回，所以即使进程被强杀，已经记下的分片下次仍然认得出来。

    只认「文件名 + 地址 + 字节数」三者都对得上的分片：换了片源、重新编码、
    或者文件被截断，都会重新下载，不会把旧内容当成新内容拼进产物。
    """

    def __init__(self, path: Path, log: logging.Logger) -> None:
        self._path = path
        self._log = log
        self._warned = False
        self._done: dict[str, tuple[str, int]] = {}
        # 并发下载时会有多个线程同时调用 record()，不加锁的话两个线程的写入
        # 可能交错，把索引文件写成半行。
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return  # 第一次运行还没有索引文件

        for line in lines:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            name, url, size = parts
            try:
                self._done[name] = (url, int(size))
            except ValueError:
                continue

    @property
    def known_count(self) -> int:
        return len(self._done)

    def matches(self, filename: str, url: str, path: Path) -> int | None:
        """该分片此前是否已完整下载？是则返回字节数，否则返回 ``None``。"""
        record = self._done.get(filename)
        if record is None or record[0] != url or record[1] <= 0:
            return None
        try:
            if not path.is_file() or path.stat().st_size != record[1]:
                return None
        except OSError:
            return None
        return record[1]

    def record(self, filename: str, url: str, size: int) -> None:
        if size <= 0:
            return
        with self._lock:
            self._done[filename] = (url, size)
            try:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{filename}\t{url}\t{size}\n")
            except OSError as exc:
                # 索引写不进去只影响下次续传，不该因此中断正在进行的下载。
                if not self._warned:
                    self._warned = True
                    self._log.warning("无法写入续传索引（%s），本次下载不受影响。", exc)


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
                max_retries=max_retries,
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

        # 少发字节但没报错（服务器提前关闭连接）也要当作失败重试，
        # 不能把截断的分片当成完整分片写进产物。
        if expected is not None and written != expected:
            _remove_quietly(destination)
            raise _RetryableSegmentError(
                f"分片内容不完整：收到 {written} 字节，与 Content-Length（{expected} 字节）不符"
            )

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

    # 密文长度同样要对得上：短读会让 AES-128-CBC 解出错误内容或直接报填充错误。
    if expected is not None and len(buffer) != expected:
        _remove_quietly(destination)
        raise _RetryableSegmentError(
            f"分片内容不完整：收到 {len(buffer)} 字节，"
            f"与 Content-Length（{expected} 字节）不符"
        )

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

    并发下载时会被多个线程同时调用，因此整个获取过程加锁：同一个 URI 只会
    真正请求一次，不会因为并发而重复取密钥。
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
        self._lock = threading.Lock()

    def get(self, uri: str) -> bytes:
        """返回 16 字节密钥；同一个 URI 只会真正请求一次。"""
        with self._lock:
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


def request_text(
    session: requests.Session,
    url: str,
    *,
    cancel: threading.Event,
    max_retries: int,
    log: logging.Logger,
    what: str,
) -> str:
    """带有限重试的 GET 请求，返回响应文本。

    下载与在线播放共用：在线播放的预检必须用与下载完全相同的超时与重试行为，
    否则"下载能取到、在线播放取不到"这类差异会很难排查。
    """
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


def _format_size(num_bytes: int) -> str:
    """把字节数格式化成人能读的大小（核心模块不依赖界面里的格式化函数）。"""
    value = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _log_transfer_summary(
    *,
    log: logging.Logger,
    progress: "_ProgressReporter",
    item_count: int,
    workers: int,
    started: float,
) -> None:
    """输出一段真实传输统计，便于判断慢在哪一环。

    「单分片平均大小」和「平均速度」放在一起看就能区分两种慢：单条连接被服务器
    限速（加并发能改善），还是线路总带宽到顶（加并发没用）。
    """
    elapsed = max(time.monotonic() - started, 0.001)
    total_bytes = progress.total_bytes
    if total_bytes <= 0:
        return

    per_item = total_bytes // item_count if item_count else 0
    log.info(
        "下载阶段完成：%d 个分片共 %s，用时 %.1f 秒，平均 %s/s"
        "（单分片平均 %s，%d 路并发）。",
        item_count,
        _format_size(total_bytes),
        elapsed,
        _format_size(int(total_bytes / elapsed)),
        _format_size(int(per_item)),
        workers,
    )


def _configure_connection_pool(session: requests.Session, workers: int) -> None:
    """按并发数调整连接池大小。

    ``requests`` 默认每个主机只池化 10 条连接。并发数一旦超过它，多出来的连接
    用完就被丢弃，下一个分片得重新做一次 TCP + TLS 握手——分片小而多的片源上，
    这些握手会吃掉相当可观的时间。

    只作用于下载器自己创建的会话；调用方注入的会话保持原样，不动别人的配置。
    """
    pool_size = max(workers, DEFAULT_WORKERS) + 2
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=0,  # 重试由下载器自己控制，避免两层重试叠加放大
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)


def _resume_identity(url: str, target: Path, playlist_text: str) -> str:
    """生成续传目录的身份标识（只有完全一致的任务才会复用分片）。"""
    digest = hashlib.md5(playlist_text.encode("utf-8")).hexdigest()
    return f"{without_query(url)}|{target}|{digest}"


def _create_temp_dir(
    temp_dir: str | os.PathLike[str] | None,
    *,
    identity: str | None = None,
    default_parent: Path | None = None,
) -> Path:
    """在指定父目录下准备分片临时目录。

    显式给了 ``temp_dir`` 就用它；否则退回 ``default_parent``——调用方传的是
    **输出文件所在目录**。分片动辄几个 GB，默认丢进系统临时目录很容易把系统盘
    写满，而输出目录本来就是用户自己挑的、空间够用的地方，其可写性也已经在
    :func:`_prepare_output` 里校验过。

    给了 ``identity`` 时目录名由它决定，同一个任务第二次运行会落回同一个目录，
    从而能跳过已经下好的分片（断点续传）；不给时每次都是全新的随机目录。
    """
    parent: Path | None = None
    if temp_dir:
        parent = Path(temp_dir).expanduser()
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HLSDownloaderError(f"无法创建临时目录 {parent}：{exc}") from exc
    else:
        parent = default_parent

    try:
        if identity is None:
            return Path(
                tempfile.mkdtemp(
                    prefix="hls_segments_",
                    dir=str(parent) if parent else None,
                )
            )

        digest = hashlib.md5(identity.encode("utf-8")).hexdigest()[:12]
        path = (parent or Path(tempfile.gettempdir())) / f"hls_resume_{digest}"
        path.mkdir(parents=True, exist_ok=True)
        return path
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
    max_retries: int,
) -> None:
    delay = min(RETRY_BACKOFF_SECONDS * (2**attempt), MAX_RETRY_DELAY_SECONDS)
    log.warning(
        "第 %d/%d 个分片下载失败（%s），%.1f 秒后进行第 %d/%d 次重试。",
        position,
        total,
        error,
        delay,
        attempt + 1,
        max_retries,
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

    并发下载时会被多个线程同时调用，所有状态都加锁保护；回调在锁外执行，
    避免回调里的阻塞（例如界面排队）拖住其它分片。
    """

    def __init__(
        self,
        callback: ProgressCallback | None,
        log: logging.Logger,
        *,
        parallel: bool = False,
    ) -> None:
        self._callback = callback
        self._log = log
        self._parallel = parallel
        self._lock = threading.Lock()
        self._total = 0
        self._completed = 0
        self._resumed = 0
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
        with self._lock:
            self._total = max(0, total)

    def set_message(self, message: str) -> None:
        with self._lock:
            self._message = message

    def set_stage(self, stage: TaskStage, message: str = "") -> None:
        with self._lock:
            self._stage = stage
            self._message = message
            if stage is TaskStage.DOWNLOADING_SEGMENTS and self._download_started is None:
                self._download_started = time.monotonic()
        self.emit(force=True)

    def begin_item(self, expected_bytes: int | None) -> None:
        with self._lock:
            self._item_bytes = 0
            self._item_expected = expected_bytes
            if self._download_started is None:
                self._download_started = time.monotonic()

    def add_bytes(self, count: int) -> None:
        now = time.monotonic()
        with self._lock:
            self._total_bytes += count
            self._item_bytes += count
            self._window.append((now, count))
            self._window_bytes += count
            self._trim_window(now)
        self.emit()

    def finish_item(self) -> None:
        with self._lock:
            self._completed += 1
            self._item_bytes = 0
            self._item_expected = None
        self.emit(force=True)

    def skip_item(self, size: int) -> None:
        """续传时跳过一个已下好的分片：计入进度，但不计入网速与平均耗时。"""
        with self._lock:
            self._completed += 1
            self._resumed += 1
            self._total_bytes += max(0, size)
        self.emit(force=True)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def emit(self, *, force: bool = False) -> None:
        if self._callback is None:
            return

        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_emit < PROGRESS_EMIT_INTERVAL_SECONDS:
                return
            self._last_emit = now

            percent = self._percent()
            # 完成前封顶在 99%：分片下完不代表任务完成，合并仍可能失败。
            if (
                self._stage is not TaskStage.COMPLETED
                and percent > MAX_PERCENT_BEFORE_COMPLETION
            ):
                percent = MAX_PERCENT_BEFORE_COMPLETION
            # 分片重试会丢弃上一次尝试写入的数据，_percent() 因而可能回落；
            # 对外呈现的进度必须单调不减，否则界面上的进度条会往回跳。
            if percent > self._max_percent:
                self._max_percent = percent
            else:
                percent = self._max_percent

            report = ProgressReport(
                stage=self._stage,
                completed_segments=self._completed,
                total_segments=self._total,
                percent=percent,
                speed_bps=self._speed(),
                eta_seconds=self._eta(),
                message=self._message,
            )

        try:
            self._callback(report)
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
        # 并发时多个分片同时在写，"当前分片进度"没有意义，只按完成个数算。
        if not self._parallel and self._item_expected:
            partial = min(self._item_bytes / self._item_expected, 1.0)
        return min(100.0, (self._completed + partial) / self._total * 100.0)

    def _speed(self) -> float:
        if not self._window:
            return 0.0
        span = max(self._window[-1][0] - self._window[0][0], 0.5)
        return self._window_bytes / span

    def _eta(self) -> float | None:
        remaining = self._total - self._completed
        # 跳过的分片是瞬间完成的，不能算进"平均耗时"，否则剩余时间会被严重低估。
        downloaded = self._completed - self._resumed
        if remaining <= 0 or downloaded < 2 or self._download_started is None:
            return None
        elapsed = time.monotonic() - self._download_started
        if elapsed <= 0:
            return None
        return elapsed / downloaded * remaining
