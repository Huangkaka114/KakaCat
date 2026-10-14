"""HLS 视频下载器核心包。

本包只包含与界面无关的下载逻辑，可以被 GUI、脚本或自动化测试独立调用：

    from hls_downloader import download_hls_video

    result = download_hls_video(
        playlist_url="https://example.com/video/index.m3u8",
        output_path="output.ts",
    )

当前版本仅支持用户有权访问的、未加密的 HLS VOD 媒体播放列表。
不包含任何绕过付费墙、破解 DRM 或规避访问控制的逻辑。
"""

from .downloader import download_hls_video
from .models import (
    DownloadCancelled,
    DownloadOptions,
    DownloadResult,
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
    SegmentDownloadError,
    SegmentKind,
    TaskStage,
    UnsupportedEncryptionError,
    UnsupportedSegmentError,
)
from .playlist import parse_playlist
from .url_utils import redact_url, resolve_url, validate_http_url

__version__ = "1.0.0"

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
    "SegmentDownloadError",
    "SegmentKind",
    "TaskStage",
    "UnsupportedEncryptionError",
    "UnsupportedSegmentError",
]
