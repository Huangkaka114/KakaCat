"""在线播放：启动播放器之前的那一步预检。

「在线播放」和「下载」是两条互不相干的路径。在线播放只把播放列表地址交给外部播放器，
由播放器自己在线上取流播放；程序不建临时目录、不写分片、不产出文件，也不与
下载任务共享任何状态——下载在跑的时候照样可以在线播放，反之亦然。

本模块负责启动播放器之前的准备工作：

* 用与下载**完全相同**的入口处理 URL（校验 + 可选参数签名），见
  :func:`hls_downloader.downloader.prepare_playlist_url`；
* 用与下载**完全相同**的超时与重试拉一次播放列表文本，见
  :func:`hls_downloader.downloader.request_text`；
* 解析它，好把「链接打不开」「这是主播放列表」这类问题用人话讲清楚，
  而不是把播放器的报错甩给用户。

预检比下载宽松：主播放列表、直播列表，以及当前版本不支持下载的加密方式或
分片类型，都只记为提示并放行——播放能力本来就强于下载，没必要跟着一起拒绝。
真正无法继续的情况（链接无效、取不到播放列表、播放列表格式本身错误）才抛异常。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import requests

from .downloader import (
    DEFAULT_MAX_RETRIES,
    create_session,
    prepare_playlist_url,
    request_text,
)
from .models import (
    LivePlaylistError,
    MasterPlaylistError,
    UnsupportedEncryptionError,
    UnsupportedSegmentError,
)
from .playlist import parse_playlist
from .url_utils import redact_url


@dataclass(frozen=True)
class PlaybackInfo:
    """一次在线播放的预检的结果。

    ``url`` 是真正交给播放器的地址（已完成校验并按可选参数签名）；其余字段
    用于在界面上说明这次在线播放将要播放什么，缺失时取中性默认值。
    """

    url: str
    segment_count: int = 0
    total_duration: float = 0.0
    encryption_method: str | None = None
    #: 链接指向主播放列表：播放器会自行挑选码率，下载则需要具体的媒体播放列表
    is_master: bool = False
    #: 链接指向直播或尚未结束的列表：可以在线播放，但不能下载
    is_live: bool = False
    #: 需要提醒用户、但不影响继续播放的说明
    warnings: tuple[str, ...] = field(default_factory=tuple)


def inspect_playlist(
    playlist_url: str,
    optional_a: str | None = None,
    optional_b: str | None = None,
    *,
    session: requests.Session | None = None,
    cancel_event: threading.Event | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    logger: logging.Logger | None = None,
) -> PlaybackInfo:
    """拉取并解析播放列表，返回在线播放所需的信息。

    只发一次请求取播放列表文本，不会请求任何分片或解密密钥。

    Args:
        playlist_url: 用户填写的链接，处理方式与 :func:`download_hls_video` 一致。
        optional_a: 参数 A（授权密钥），与下载同一套签名规则。
        optional_b: 参数 B（基础地址），与下载同一套推导规则。
        session: 可选的 ``requests.Session``；留空时自建一个。
        cancel_event: 可选取消信号。
        max_retries: 单个请求失败后的最大重试次数。
        logger: 可选日志记录器。

    Returns:
        :class:`PlaybackInfo`。

    Raises:
        InvalidUrlError: URL 为空、协议不支持或格式错误。
        PlaylistFetchError: 获取播放列表时的网络错误或 HTTP 错误。
        InvalidPlaylistError: 播放列表格式无效、为空或不含任何分片。
        DownloadCancelled: 用户取消了在线播放。
    """
    log = logger or logging.getLogger("hls_downloader")

    # 与下载共用同一个入口：同一个链接算出的请求地址必然一致。
    url = prepare_playlist_url(playlist_url, optional_a, optional_b, logger=log)

    cancel = cancel_event if cancel_event is not None else threading.Event()
    owns_session = session is None
    active_session = session or create_session(workers=1)

    try:
        log.info("正在获取播放列表用于在线播放：%s", redact_url(url))
        text = request_text(
            active_session,
            url,
            cancel=cancel,
            max_retries=max(0, int(max_retries)),
            log=log,
            what="获取播放列表",
        )
    finally:
        if owns_session:
            active_session.close()

    return _classify(text, url, log)


def _classify(text: str, url: str, log: logging.Logger) -> PlaybackInfo:
    """解析播放列表；下载会拒绝的情况在在线播放中降级为提示。"""
    try:
        playlist = parse_playlist(text, url)
    except MasterPlaylistError:
        log.info("链接指向主播放列表，在线播放会交给播放器自行选择码率。")
        return PlaybackInfo(
            url=url,
            is_master=True,
            warnings=(
                "这是主播放列表：在线播放会交由播放器自行挑选码率，"
                "下载则需要你提供具体的媒体播放列表。",
            ),
        )
    except LivePlaylistError:
        log.info("链接指向直播或尚未结束的列表，在线播放可以直接播放。")
        return PlaybackInfo(
            url=url,
            is_live=True,
            warnings=("这是直播或尚未结束的列表：可以在线播放，但当前版本不支持下载直播。",),
        )
    except UnsupportedEncryptionError as exc:
        log.info("播放列表的加密方式下载不支持，在线播放仍会交给播放器尝试。")
        return PlaybackInfo(
            url=url,
            warnings=(f"下载不支持这种加密方式（{exc.message}），在线播放仍会交给播放器尝试。",),
        )
    except UnsupportedSegmentError as exc:
        log.info("播放列表的分片类型下载不支持，在线播放仍会交给播放器尝试。")
        return PlaybackInfo(
            url=url,
            warnings=(f"下载不支持这种分片类型（{exc.message}），在线播放仍会交给播放器尝试。",),
        )

    return PlaybackInfo(
        url=url,
        segment_count=playlist.segment_count,
        total_duration=playlist.total_duration,
        encryption_method=playlist.encryption_method,
        warnings=tuple(playlist.warnings),
    )
