"""下载任务的数据模型、状态枚举与异常定义。

本模块不导入任何 GUI 或网络库，可以在没有界面、没有网络的情况下被测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskStage(str, Enum):
    """下载任务所处的阶段。"""

    IDLE = "idle"
    FETCHING_PLAYLIST = "fetching_playlist"
    PARSING_PLAYLIST = "parsing_playlist"
    DOWNLOADING_SEGMENTS = "downloading_segments"
    MERGING = "merging"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        """返回用于界面展示的中文名称。"""
        return _STAGE_LABELS[self]


_STAGE_LABELS: dict["TaskStage", str] = {
    TaskStage.IDLE: "等待开始",
    TaskStage.FETCHING_PLAYLIST: "正在获取播放列表",
    TaskStage.PARSING_PLAYLIST: "正在解析播放列表",
    TaskStage.DOWNLOADING_SEGMENTS: "正在下载分片",
    TaskStage.MERGING: "正在合并视频",
    TaskStage.COMPLETED: "下载完成",
    TaskStage.FAILED: "下载失败",
    TaskStage.CANCELLING: "正在取消",
    TaskStage.CANCELLED: "已取消",
}


class SegmentKind(str, Enum):
    """分片的容器类型，决定后续的合并策略。"""

    TS = "ts"
    FMP4 = "fmp4"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            SegmentKind.TS: "MPEG-TS 分片",
            SegmentKind.FMP4: "fMP4 / CMAF 分片",
            SegmentKind.UNKNOWN: "未知格式分片",
        }[self]


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class HLSDownloaderError(Exception):
    """下载器所有异常的基类。

    ``message`` 始终是可以直接展示给用户的中文描述。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - 简单透传
        return self.message


class InvalidUrlError(HLSDownloaderError):
    """URL 为空、协议不支持或格式明显错误。"""


class PlaylistError(HLSDownloaderError):
    """播放列表相关的所有错误。"""


class InvalidPlaylistError(PlaylistError):
    """播放列表格式无效、为空或不包含任何分片。"""


class MasterPlaylistError(PlaylistError):
    """传入的是主播放列表（master playlist），需要具体的媒体播放列表。"""


class LivePlaylistError(PlaylistError):
    """播放列表看起来是直播或未完成，当前版本只支持点播列表。"""


class UnsupportedEncryptionError(PlaylistError):
    """播放列表包含加密分片，当前版本不支持。"""


class UnsupportedSegmentError(PlaylistError):
    """分片格式当前版本无法处理。"""


class PlaylistFetchError(HLSDownloaderError):
    """获取播放列表时的网络或 HTTP 错误。"""


class SegmentDownloadError(HLSDownloaderError):
    """某个分片下载失败（重试后仍未成功）。"""

    def __init__(self, message: str, *, segment_index: int | None = None) -> None:
        super().__init__(message)
        self.segment_index = segment_index


class MergeError(HLSDownloaderError):
    """分片合并或封装失败。"""


class OutputExistsError(HLSDownloaderError):
    """输出文件已存在，且未允许覆盖。"""


class DownloadCancelled(HLSDownloaderError):
    """用户主动取消任务。"""


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """媒体播放列表中的一个分片。"""

    index: int
    url: str
    duration: float | None = None
    title: str | None = None


@dataclass(frozen=True)
class PlaylistInfo:
    """解析后的媒体播放列表信息。"""

    segments: tuple[Segment, ...]
    source_url: str
    segment_kind: SegmentKind = SegmentKind.UNKNOWN
    target_duration: int | None = None
    version: int | None = None
    media_sequence: int = 0
    playlist_type: str | None = None
    has_endlist: bool = False
    encryption_method: str | None = None
    init_segment_url: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def total_duration(self) -> float:
        """所有分片时长之和；时长未知的分片按 0 计算。"""
        return sum(s.duration or 0.0 for s in self.segments)


@dataclass(frozen=True)
class DownloadOptions:
    """预留的扩展参数。

    当前版本只负责原样保存用户的输入，不会把它们拼接到 URL、请求头、
    Cookie 或请求参数中，也不会推测其业务含义。未来接入真实业务时，
    由 :class:`hls_downloader.interfaces.BusinessAdapter` 的实现负责解释。
    """

    optional_a: str | None = None
    optional_b: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.optional_a or "").strip() and not (self.optional_b or "").strip()

    def normalized(self) -> "DownloadOptions":
        """去除首尾空白，并把空字符串归一化为 ``None``。"""

        def _clean(value: str | None) -> str | None:
            if value is None:
                return None
            text = value.strip()
            return text or None

        return DownloadOptions(optional_a=_clean(self.optional_a), optional_b=_clean(self.optional_b))


@dataclass(frozen=True)
class ProgressReport:
    """一次进度更新。

    由核心下载函数生成，通过 ``progress_callback`` 传递给调用方；
    数值全部来自真实下载统计，不做任何模拟。
    """

    stage: TaskStage
    completed_segments: int = 0
    total_segments: int = 0
    percent: float = 0.0
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    message: str = ""

    @property
    def stage_label(self) -> str:
        return self.stage.label


@dataclass(frozen=True)
class MergeResult:
    """合并阶段的结果。"""

    output_path: str
    total_bytes: int
    strategy: str


@dataclass(frozen=True)
class DownloadResult:
    """任务成功完成后的结果对象。"""

    output_path: str
    segment_count: int
    total_bytes: int
    elapsed_seconds: float
    segment_kind: SegmentKind = SegmentKind.UNKNOWN
    warnings: tuple[str, ...] = ()
