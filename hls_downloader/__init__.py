"""KakaCat（HLS 视频下载器）的核心包。

本包只包含与界面无关的下载逻辑，可以被 GUI、脚本或自动化测试独立调用：

    from hls_downloader import download_hls_video

    result = download_hls_video(
        playlist_url="https://example.com/video/index.m3u8",
        output_path="output.ts",
    )

当前版本支持普通的 HLS VOD 媒体播放列表，以及标准的整段 AES-128 加密
（密钥地址由播放列表自身给出，访问权限与分片相同）。
"""

from .downloader import download_hls_video
from .models import (
    DownloadCancelled,
    DownloadOptions,
    DownloadResult,
    EncryptionKey,
    HLSDownloaderError,
    InvalidPlaylistError,
    InvalidUrlError,
    LivePlaylistError,
    MasterPlaylistError,
    MergeError,
    OutputExistsError,
    PlaylistError,
    PlaylistFetchError,
    PlaylistInfo,
    ProgressReport,
    Segment,
    SegmentDecryptionError,
    SegmentDownloadError,
    SegmentKind,
    TaskStage,
    UnsupportedEncryptionError,
    UnsupportedSegmentError,
)
from .playlist import parse_playlist
from .url_utils import redact_url, resolve_url, validate_http_url

__version__ = "2.0.0"

__all__ = [
    "__version__",
    "download_hls_video",
    "parse_playlist",
    "validate_http_url",
    "resolve_url",
    "redact_url",
    "DownloadCancelled",
    "DownloadOptions",
    "DownloadResult",
    "EncryptionKey",
    "HLSDownloaderError",
    "InvalidPlaylistError",
    "InvalidUrlError",
    "LivePlaylistError",
    "MasterPlaylistError",
    "MergeError",
    "OutputExistsError",
    "PlaylistError",
    "PlaylistFetchError",
    "PlaylistInfo",
    "ProgressReport",
    "Segment",
    "SegmentDecryptionError",
    "SegmentDownloadError",
    "SegmentKind",
    "TaskStage",
    "UnsupportedEncryptionError",
    "UnsupportedSegmentError",
]
