"""主窗口：界面布局、用户交互、后台任务调度与线程安全的状态更新。

线程模型：

* GUI 主线程只负责收集输入和刷新界面；
* ``threading.Thread`` 执行 :func:`hls_downloader.download_hls_video`；
* 后台线程通过 ``queue.Queue`` 发送消息，主线程用 ``after`` 轮询消费；
* 后台线程从不直接触碰任何控件。
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from tkinter import BooleanVar, StringVar, filedialog, messagebox
from urllib.parse import urlsplit

import customtkinter as ctk

from hls_downloader import __version__
from hls_downloader.downloader import (
    DEFAULT_WORKERS,
    MAX_WORKERS,
    download_hls_video,
    prepare_playlist_url,
)
from hls_downloader.merger import find_ffmpeg
from hls_downloader.models import (
    DownloadCancelled,
    DownloadResult,
    HLSDownloaderError,
    InvalidUrlError,
    ProgressReport,
    TaskStage,
)
from hls_downloader.player import (
    PlayerError,
    PlayerHandle,
    find_ffplay,
    find_mpv,
    launch_playback,
    stop_playback,
)
from hls_downloader.playback import PlaybackInfo, inspect_playlist
from hls_downloader.url_utils import validate_http_url

from . import theme
from .components import (
    Card,
    LabeledEntry,
    LogPanel,
    PlaybackPanel,
    SectionTitle,
    StatusPanel,
    format_bytes,
    format_elapsed,
    format_playback_summary,
)
from .theme import Fonts

#: 软件名。窗口标题、界面标题栏、日志抬头都用它——只此一处，改名不用满仓库找。
APP_NAME = "KakaCat"

#: 消息队列轮询间隔（毫秒）
POLL_INTERVAL_MS = 100

#: 关闭窗口时等待后台任务收尾的最长时间（秒）。超时后直接关闭，
#: 残留的临时文件由 _cleanup_temp_root 兜底删除。
SHUTDOWN_GRACE_SECONDS = 3.0

MSG_LOG = "log"
MSG_PROGRESS = "progress"
MSG_DONE = "done"
MSG_FAILED = "failed"
MSG_CANCELLED = "cancelled"
MSG_PLAYBACK_READY = "playback_ready"
MSG_PLAYBACK_FAILED = "playback_failed"
MSG_PLAYBACK_CANCELLED = "playback_cancelled"

#: 轮询播放器进程是否已退出的间隔（毫秒）。播放器是独立进程，只有靠轮询
#: 才能知道用户是不是自己把窗口关掉了。
PLAYBACK_POLL_INTERVAL_MS = 500

#: 播放器在这个时间内退出，就认为它根本没播起来（秒）。ffplay 以 0 退出也可能是
#: 失败，因此除了返回码，还要看它活了多久、有没有报错。
PLAYBACK_STARTUP_GRACE_SECONDS = 3.0


def _stretch_to_viewport(card) -> None:
    """让可滚动区域里的卡片横向铺满视口。

    ``customtkinter`` 6.0 把可滚动内容框架的列权重留成了 0，卡片会按自身请求
    宽度摆放，右边空出一条——标题栏、输入框全都被挤在左侧一小块里。
    这里把它所在列的权重补上。放在普通框架里调用也无副作用。
    """
    card.master.grid_columnconfigure(0, weight=1)


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_GENERIC_STEMS = {
    "", "index", "playlist", "master", "manifest", "media", "out", "output", "hls", "video",
}

_logger = logging.getLogger(__name__)


def sanitize_filename(name: str, fallback: str = "video") -> str:
    """清理文件名中的非法字符，避免写入失败或覆盖系统保留名。"""
    cleaned = _INVALID_FILENAME_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip().rstrip(". ")
    if not cleaned:
        return fallback

    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    return cleaned[:120]


def default_output_suffix() -> str:
    """默认的输出容器扩展名。

    优先 ``.mp4``：双击就能播、兼容性最好。但它需要 FFmpeg 把 TS 重新封装
    （``-c copy``，不重新编码），所以**没检测到 FFmpeg 时退回 ``.ts``**——
    否则默认值一上来就是个注定失败的选择。
    """
    return ".mp4" if find_ffmpeg() is not None else ".ts"


def default_filename_from_url(url: str, suffix: str | None = None) -> str:
    """根据播放列表地址给出一个合理的默认文件名。

    ``suffix`` 留空时按 :func:`default_output_suffix` 决定（有 FFmpeg 用
    ``.mp4``，没有用 ``.ts``）。
    """
    suffix = suffix or default_output_suffix()
    try:
        path = urlsplit(url).path
    except ValueError:
        return f"video{suffix}"

    stem = Path(path).stem
    if stem.lower() in _GENERIC_STEMS or len(stem) > 80:
        return f"video{suffix}"

    return sanitize_filename(f"{stem}{suffix}", fallback=f"video{suffix}")


class _QueueLogHandler(logging.Handler):
    """把标准库日志转发到界面消息队列。"""

    def __init__(self, message_queue: "queue.Queue[tuple[str, object]]") -> None:
        super().__init__()
        self._queue = message_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # pragma: no cover - 格式化失败不应影响下载
            return

        if record.levelno >= logging.ERROR:
            level = "error"
        elif record.levelno >= logging.WARNING:
            level = "warn"
        else:
            level = "info"

        self._queue.put((MSG_LOG, (level, text)))


class MainWindow(ctk.CTk):
    """应用主窗口。"""

    def __init__(self) -> None:
        super().__init__()

        self._fonts: Fonts = theme.create_fonts()
        self._message_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel_event: threading.Event | None = None
        self._closing = False
        self._poll_after_id: str | None = None
        #: 关闭续传时，本次下载的临时分片目录（建在输出目录下，任务结束即删）
        self._temp_root: Path | None = None
        self._filename_customized = False
        self._wide = True
        # 在线播放的状态与下载完全独立：在线播放不占用下载的按钮状态机，
        # 下载在跑的时候也可以在线播放。
        self._playback_worker: threading.Thread | None = None
        self._playback_cancel: threading.Event | None = None
        self._playback_process: PlayerHandle | None = None
        self._playback_poll_id: str | None = None
        self._playback_generation = 0
        #: 上次选中的倍速；重新起播放时沿用它
        self._playback_speed = 1.0

        self.title(APP_NAME)
        self.configure(fg_color=theme.BG_APP)
        # 最小尺寸保持较小，小屏设备上由自适应布局与滚动区域兜底
        self.minsize(720, 540)

        self._build_ui()
        self._apply_responsive_layout(wide=True)
        self._center_window()

        self.bind("<Configure>", self._on_configure)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_poll()

        self.log_panel.append(f"{APP_NAME} v{__version__} 已启动。", "success")
        self.log_panel.append(
            "请确认你拥有所下载视频的访问与下载权限。",
            "warn",
        )
        if find_ffmpeg() is None:
            self.log_panel.append(
                "未检测到 FFmpeg：输出 .ts 文件不受影响；如需封装为 MP4，请安装 FFmpeg 并加入 PATH。",
                "warn",
            )
        if find_mpv() is None and find_ffplay() is None:
            self.log_panel.append(
                "未检测到 mpv，也没有 ffplay：在线播放不可用（下载不受影响）。"
                "装 mpv 可获得进度条与倍速；装 FFmpeg 可以得到 ffplay。",
                "warn",
            )
        elif find_mpv() is None:
            self.log_panel.append(
                "未检测到 mpv：在线播放会退回 ffplay，但进度条与倍速不可用。",
                "info",
            )

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)  # 标题栏
        # 表单与日志都用 minsize 保住各自的最低可用高度：只给 weight 的话，
        # 窗口一矮就先饿死表单，日志反而比表单还高。
        self.grid_rowconfigure(1, weight=5, minsize=390)  # 下载设置 + 支持信息
        self.grid_rowconfigure(2, weight=0)  # 全宽状态带
        self.grid_rowconfigure(3, weight=2, minsize=150)  # 运行日志

        self._build_header()
        self._build_body()

        # 状态带横跨整个窗口，放在设置区与日志之间：既占满了中间原本空着
        # 的地方，也让进度成为视线焦点。
        self.status_panel = StatusPanel(self, self._fonts)
        self.status_panel.grid(
            row=2, column=0, sticky="ew",
            padx=theme.SPACE_LG, pady=(0, theme.SPACE_MD),
        )

        self.log_panel = LogPanel(self, self._fonts)
        self.log_panel.grid(
            row=3, column=0, sticky="nsew",
            padx=theme.SPACE_LG, pady=(0, theme.SPACE_LG),
        )

    def _build_header(self) -> None:
        header = Card(self, height=78)
        header.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_LG, pady=theme.SPACE_LG)
        header.grid_propagate(False)
        header.columnconfigure(1, weight=1)

        ctk.CTkLabel(
            header,
            text="HLS",
            font=ctk.CTkFont(family=self._fonts.family, size=14, weight="bold"),
            text_color=theme.PRIMARY_TEXT,
            fg_color=theme.PRIMARY,
            corner_radius=10,
            width=46,
            height=46,
        ).grid(row=0, column=0, rowspan=2, padx=(theme.SPACE_LG, theme.SPACE_MD), pady=theme.SPACE_MD)

        ctk.CTkLabel(
            header, text=APP_NAME, font=self._fonts.title, text_color=theme.TEXT, anchor="w"
        ).grid(row=0, column=1, sticky="sw", pady=(theme.SPACE_MD, 0))

        ctk.CTkLabel(
            header,
            text="输入 M3U8 链接，下载并合并视频",
            font=self._fonts.subtitle,
            text_color=theme.TEXT_SECONDARY,
            anchor="w",
        ).grid(row=1, column=1, sticky="nw", pady=(0, theme.SPACE_MD))

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=2, rowspan=2, sticky="e", padx=(0, theme.SPACE_LG))

        ctk.CTkLabel(
            right,
            text="深色主题",
            font=self._fonts.hint,
            text_color=theme.TEXT_SECONDARY,
            fg_color=theme.BG_HOVER,
            corner_radius=10,
            width=72,
            height=24,
        ).grid(row=0, column=0, sticky="e")
        ctk.CTkLabel(
            right, text=f"v{__version__}", font=self._fonts.hint, text_color=theme.TEXT_DIM
        ).grid(row=1, column=0, sticky="e", pady=(theme.SPACE_XS, 0))

    def _build_body(self) -> None:
        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.grid(
            row=1, column=0, sticky="nsew",
            padx=theme.SPACE_LG, pady=(0, theme.SPACE_MD),
        )
        self._body.rowconfigure(0, weight=1)
        self._body.rowconfigure(1, weight=0)

        # 左栏：表单可滚动，但「开始 / 取消」固定在下方，窗口再矮也不用翻到
        # 卡片底部才点得到。
        self._left_column = ctk.CTkFrame(self._body, fg_color="transparent")
        self._left_column.columnconfigure(0, weight=1)
        self._left_column.rowconfigure(0, weight=1)

        form = ctk.CTkScrollableFrame(
            self._left_column,
            fg_color="transparent",
            corner_radius=0,
            height=372,
            # 滚动条压暗到几乎看不见：默认窗口下表单正好放得下，它只是
            # 窗口变矮时的兜底，不该抢视线。
            scrollbar_fg_color="transparent",
            scrollbar_button_color=theme.BG_PANEL,
            scrollbar_button_hover_color=theme.BORDER_STRONG,
        )
        form.grid(row=0, column=0, sticky="nsew")
        self._build_settings_card(form)
        self._build_action_bar(self._left_column)

        # 右栏也用可滚动容器：窗口拉小时内容不会被裁掉（快捷操作按钮曾经因为
        # 高度不够而直接看不见）。
        self._right_column = ctk.CTkScrollableFrame(
            self._body,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_fg_color="transparent",
            scrollbar_button_color=theme.BG_PANEL,
            scrollbar_button_hover_color=theme.BORDER_STRONG,
        )
        self._build_info_panel(self._right_column)

        # 在线播放放在右栏最下面：它是与下载并列的独立入口，不占用左栏的
        # 下载操作条，也不参与下载的状态机。
        self.playback_panel = PlaybackPanel(
            self._right_column,
            self._fonts,
            on_play=self._on_playback,
            on_stop=self._on_playback_stop,
            on_copy=self._on_copy_playlist_url,
            on_speed=self._on_playback_speed,
        )
        self.playback_panel.grid(row=1, column=0, sticky="ew", pady=(theme.SPACE_MD, 0))
        _stretch_to_viewport(self.playback_panel)

    def _build_settings_card(self, parent) -> None:
        card = Card(parent)
        card.grid(row=0, column=0, sticky="ew")
        _stretch_to_viewport(card)
        card.columnconfigure(0, weight=1)

        SectionTitle(card, "下载设置", self._fonts).grid(
            row=0, column=0, sticky="w",
            padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_MD),
        )

        self.url_entry = LabeledEntry(
            card,
            self._fonts,
            label="视频 URL（M3U8）",
            placeholder="https://example.com/video/index.m3u8",
        )
        self.url_entry.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_LG)
        self.url_entry.entry.bind("<FocusOut>", self._on_url_focus_out)
        self.url_entry.entry.bind("<Return>", lambda _event: self._on_start())

        # 两个可选参数并排：都是短字段，并排既省纵向空间也让「可选」的分量
        # 一目了然。
        optional_row = ctk.CTkFrame(card, fg_color="transparent")
        optional_row.grid(
            row=2, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_MD, 0)
        )
        optional_row.columnconfigure(0, weight=1, uniform="optional")
        optional_row.columnconfigure(1, weight=1, uniform="optional")

        self.optional_a_entry = LabeledEntry(
            optional_row,
            self._fonts,
            label="可选参数 A（授权密钥）",
            placeholder="可留空",
        )
        self.optional_a_entry.grid(row=0, column=0, sticky="ew", padx=(0, theme.SPACE_SM))

        self.optional_b_entry = LabeledEntry(
            optional_row,
            self._fonts,
            label="可选参数 B（基础地址）",
            placeholder="可留空",
        )
        self.optional_b_entry.grid(row=0, column=1, sticky="ew", padx=(theme.SPACE_SM, 0))

        # 保存位置并排：目录与文件名本来就是一回事（存到哪、叫什么），
        # 并排后整张表少一行，默认窗口下不用滚动就能看到开始按钮。
        output_row = ctk.CTkFrame(card, fg_color="transparent")
        output_row.grid(
            row=3, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_MD, 0)
        )
        output_row.columnconfigure(0, weight=3)
        output_row.columnconfigure(1, weight=2)

        default_dir = str(self._default_output_dir())
        self.dir_entry = LabeledEntry(
            output_row,
            self._fonts,
            label="保存目录",
            placeholder="选择保存目录",
            button_text="选择目录",
            button_command=self._choose_directory,
            button_width=88,
        )
        self.dir_entry.set(default_dir)
        self.dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, theme.SPACE_SM))

        self.filename_entry = LabeledEntry(
            output_row,
            self._fonts,
            label="输出文件名",
            placeholder="video.ts",
        )
        self.filename_entry.set("video.ts")
        self.filename_entry.entry.bind("<KeyRelease>", self._on_filename_edited)
        self.filename_entry.grid(row=0, column=1, sticky="ew", padx=(theme.SPACE_SM, 0))

        options = ctk.CTkFrame(card, fg_color="transparent")
        options.grid(
            row=5, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_MD, 0)
        )
        options.columnconfigure(2, weight=1)

        self.overwrite_var = BooleanVar(value=False)
        self.overwrite_check = ctk.CTkCheckBox(
            options,
            text="允许覆盖已有文件",
            variable=self.overwrite_var,
            font=self._fonts.small,
            text_color=theme.TEXT_SECONDARY,
            fg_color=theme.PRIMARY,
            hover_color=theme.PRIMARY_HOVER,
            border_color=theme.BORDER_STRONG,
            corner_radius=4,
            checkbox_width=18,
            checkbox_height=18,
        )
        self.overwrite_check.grid(row=0, column=0, sticky="w")

        self.resume_var = BooleanVar(value=True)
        self.resume_check = ctk.CTkCheckBox(
            options,
            text="断点续传",
            variable=self.resume_var,
            font=self._fonts.small,
            text_color=theme.TEXT_SECONDARY,
            fg_color=theme.PRIMARY,
            hover_color=theme.PRIMARY_HOVER,
            border_color=theme.BORDER_STRONG,
            corner_radius=4,
            checkbox_width=18,
            checkbox_height=18,
        )
        self.resume_check.grid(row=0, column=1, sticky="w", padx=(theme.SPACE_LG, 0))

        ctk.CTkLabel(
            options,
            text="并发分片数",
            font=self._fonts.small,
            text_color=theme.TEXT_SECONDARY,
        ).grid(row=0, column=3, sticky="e", padx=(theme.SPACE_LG, theme.SPACE_SM))
        self.workers_var = StringVar(value=str(DEFAULT_WORKERS))
        self.workers_menu = ctk.CTkOptionMenu(
            options,
            values=[str(count) for count in (1, 2, 4, 8, MAX_WORKERS)],
            variable=self.workers_var,
            width=72,
            height=26,
            font=self._fonts.small,
            dropdown_font=self._fonts.small,
            fg_color=theme.BG_INPUT,
            button_color=theme.BG_INPUT_FOCUS,
            button_hover_color=theme.BG_HOVER,
            text_color=theme.TEXT,
        )
        self.workers_menu.grid(row=0, column=4, sticky="e")

    def _build_action_bar(self, parent) -> None:
        """固定在左栏底部的操作条：开始 / 取消。"""
        bar = Card(parent)
        bar.grid(row=1, column=0, sticky="ew", pady=(theme.SPACE_SM, 0))
        bar.columnconfigure(0, weight=1)

        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_LG, pady=theme.SPACE_LG)
        actions.columnconfigure(0, weight=3)
        actions.columnconfigure(1, weight=2)

        self.start_button = ctk.CTkButton(
            actions,
            text="开始下载",
            font=self._fonts.body_bold,
            height=theme.HEIGHT_BUTTON,
            corner_radius=theme.RADIUS_BUTTON,
            fg_color=theme.PRIMARY,
            hover_color=theme.PRIMARY_HOVER,
            text_color=theme.PRIMARY_TEXT,
            command=self._on_start,
        )
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, theme.SPACE_SM))

        self.cancel_button = ctk.CTkButton(
            actions,
            text="取消下载",
            font=self._fonts.body,
            height=theme.HEIGHT_BUTTON,
            corner_radius=theme.RADIUS_BUTTON,
            fg_color=theme.BG_HOVER,
            hover_color=theme.BORDER_STRONG,
            text_color=theme.TEXT,
            border_width=1,
            border_color=theme.BORDER,
            state="disabled",
            command=self._on_cancel,
        )
        self.cancel_button.grid(row=0, column=1, sticky="ew", padx=(theme.SPACE_SM, 0))

    def _build_info_panel(self, parent) -> None:
        panel = Card(parent)
        panel.grid(row=0, column=0, sticky="ew")
        _stretch_to_viewport(panel)
        panel.columnconfigure(0, weight=1)

        SectionTitle(panel, "支持的播放列表", self._fonts).grid(
            row=0, column=0, sticky="w",
            padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_SM),
        )
        ctk.CTkLabel(
            panel,
            text=(
                "HLS 点播（VOD）媒体播放列表，支持 TS 与 fMP4/CMAF 分片，并支持整段 "
                "AES-128 加密；链接只接受 HTTP/HTTPS。\n"
                "在线播放比下载宽松：主播放列表与直播列表也可以交给播放器试播。"
            ),
            font=self._fonts.small,
            text_color=theme.TEXT_SECONDARY,
            justify="left",
            anchor="w",
            wraplength=380,
        ).grid(row=1, column=0, sticky="ew", padx=theme.SPACE_LG)

        SectionTitle(panel, "当前版本限制", self._fonts).grid(
            row=2, column=0, sticky="w",
            padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_SM),
        )
        ctk.CTkLabel(
            panel,
            text=(
                "• 不支持主播放列表，需要具体码率的媒体列表\n"
                "• 不支持直播或尚未结束的列表\n"
                "• 加密方面只实现整段 AES-128，其余方式会报错\n"
                "• 不支持 #EXT-X-BYTERANGE 分片\n"
                "• 可选参数 A 是授权密钥，参数 B 是服务基础地址；填写参数 B 时，"
                "上面的链接可以填下载链接"
            ),
            font=self._fonts.small,
            text_color=theme.TEXT_SECONDARY,
            justify="left",
            anchor="w",
            wraplength=380,
        ).grid(row=3, column=0, sticky="ew", padx=theme.SPACE_LG)

        SectionTitle(panel, "运行环境", self._fonts).grid(
            row=4, column=0, sticky="w",
            padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_SM),
        )
        ffmpeg = find_ffmpeg()
        ctk.CTkLabel(
            panel,
            text=(
                f"FFmpeg：{'已检测到，可封装 MP4。' if ffmpeg else '未检测到，只能直接拼接 .ts。'}"
            ),
            font=self._fonts.small,
            text_color=theme.SUCCESS if ffmpeg else theme.WARNING,
            justify="left",
            anchor="w",
            wraplength=380,
        ).grid(row=5, column=0, sticky="ew", padx=theme.SPACE_LG)

        mpv = find_mpv()
        ctk.CTkLabel(
            panel,
            text=(
                f"mpv：{'已检测到，在线播放支持进度条与倍速。' if mpv else '未检测到，进度条与倍速不可用。'}"
            ),
            font=self._fonts.small,
            text_color=theme.SUCCESS if mpv else theme.WARNING,
            justify="left",
            anchor="w",
            wraplength=380,
        ).grid(row=6, column=0, sticky="ew", padx=theme.SPACE_LG)

        ffplay = find_ffplay()
        ctk.CTkLabel(
            panel,
            text=(
                f"ffplay：{'已检测到，可在线播放。' if ffplay else '未检测到。'}"
            ),
            font=self._fonts.small,
            text_color=theme.SUCCESS if ffplay else theme.TEXT_DIM,
            justify="left",
            anchor="w",
            wraplength=380,
        ).grid(row=7, column=0, sticky="ew", padx=theme.SPACE_LG)

        SectionTitle(panel, "快捷操作", self._fonts).grid(
            row=8, column=0, sticky="w",
            padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_SM),
        )

        shortcuts = ctk.CTkFrame(panel, fg_color="transparent")
        shortcuts.grid(
            row=9, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(0, theme.SPACE_LG)
        )
        shortcuts.columnconfigure(0, weight=1, uniform="shortcut")
        shortcuts.columnconfigure(1, weight=1, uniform="shortcut")

        ctk.CTkButton(
            shortcuts,
            text="打开输出目录",
            font=self._fonts.small,
            height=32,
            corner_radius=theme.RADIUS_BUTTON,
            fg_color=theme.BG_HOVER,
            hover_color=theme.BORDER_STRONG,
            text_color=theme.TEXT,
            border_width=1,
            border_color=theme.BORDER,
            command=self._open_output_dir,
        ).grid(row=0, column=0, sticky="ew", padx=(0, theme.SPACE_SM))

        ctk.CTkButton(
            shortcuts,
            text="查看运行日志",
            font=self._fonts.small,
            height=32,
            corner_radius=theme.RADIUS_BUTTON,
            fg_color=theme.BG_HOVER,
            hover_color=theme.BORDER_STRONG,
            text_color=theme.TEXT,
            border_width=1,
            border_color=theme.BORDER,
            command=lambda: self.log_panel.scroll_to_end(),
        ).grid(row=0, column=1, sticky="ew", padx=(theme.SPACE_SM, 0))

    def _apply_responsive_layout(self, *, wide: bool) -> None:
        """窗口足够宽时左右双栏，否则改为单栏堆叠。"""
        for column in (0, 1):
            self._body.columnconfigure(column, weight=0, minsize=0)

        self._left_column.grid_forget()
        self._right_column.grid_forget()

        if wide:
            # 右栏必须给权重和最小宽度：只靠 sticky 的话它会缩到可滚动容器的
            # 默认宽度（200px），右栏文字被卡片边缘切掉。
            self._body.columnconfigure(0, weight=3)
            self._body.columnconfigure(1, weight=2, minsize=300)
            self._left_column.grid(row=0, column=0, sticky="nsew", padx=(0, theme.SPACE_MD))
            self._right_column.grid(row=0, column=1, sticky="nsew")
        else:
            self._body.columnconfigure(0, weight=1)
            self._left_column.grid(row=0, column=0, sticky="nsew")
            self._right_column.grid(
                row=1, column=0, sticky="ew", pady=(theme.SPACE_MD, 0)
            )

    def _on_configure(self, event) -> None:
        if event.widget is not self:
            return
        # event.width 是物理像素，而阈值是逻辑像素：不换算的话，在 125%/150%
        # 缩放的屏幕上永远跨不过阈值，窗口已经很窄了还硬撑双栏。
        scaling = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
        wide = event.width / scaling >= theme.NARROW_WIDTH_THRESHOLD
        if wide == self._wide:
            return
        self._wide = wide
        self._apply_responsive_layout(wide=wide)

    def _center_window(self, width: int = 1180, height: int = 940) -> None:
        self.update_idletasks()
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(width, max(720, screen_width - 80))
        height = min(height, max(540, screen_height - 120))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2 - 24)
        self.geometry(f"{width}x{height}+{x}+{y}")

    # ------------------------------------------------------------------
    # 交互回调
    # ------------------------------------------------------------------

    @staticmethod
    def _default_output_dir() -> Path:
        downloads = Path.home() / "Downloads"
        return downloads if downloads.is_dir() else Path.home()

    def _on_url_focus_out(self, _event=None) -> None:
        """URL 输入完成后，如果用户还没改过文件名，就自动填一个默认名。"""
        if self._filename_customized:
            return
        url = self.url_entry.get().strip()
        if url:
            self.filename_entry.set(default_filename_from_url(url, default_output_suffix()))

    def _on_filename_edited(self, _event=None) -> None:
        self._filename_customized = True

    def _choose_directory(self) -> None:
        current = self.dir_entry.get().strip() or str(self._default_output_dir())
        selected = filedialog.askdirectory(initialdir=current, title="选择保存目录", parent=self)
        if selected:
            self.dir_entry.set(str(Path(selected)))

    def _open_output_dir(self) -> None:
        directory = self.dir_entry.get().strip() or str(self._default_output_dir())
        path = Path(directory).expanduser()
        if not path.is_dir():
            messagebox.showwarning("目录不存在", f"目录不存在：{path}", parent=self)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # noqa: S606 - 打开用户自己选择的本地目录
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self)

    def _on_start(self) -> None:
        if self._is_running():
            self.log_panel.append("已有任务正在运行，请先取消或等待完成。", "warn")
            return

        url = self.url_entry.get().strip()
        try:
            validate_http_url(url)
        except InvalidUrlError as exc:
            self.log_panel.append(f"链接无效：{exc.message}", "error")
            messagebox.showwarning("链接无效", exc.message, parent=self)
            self.url_entry.focus_entry()
            return

        output_path = self._resolve_output_path()
        if output_path is None:
            return

        overwrite = bool(self.overwrite_var.get())
        if output_path.exists() and not overwrite:
            if not messagebox.askyesno(
                "文件已存在",
                f"{output_path.name} 已经存在，是否覆盖？",
                parent=self,
            ):
                self.log_panel.append("已取消：输出文件已存在且未允许覆盖。", "warn")
                return
            overwrite = True
            self.overwrite_var.set(True)

        optional_a = self.optional_a_entry.get().strip() or None
        optional_b = self.optional_b_entry.get().strip() or None
        if optional_a:
            self.log_panel.append(
                "已填写参数 A：将为 M3U8 请求生成签名（密钥不会写入日志）。", "info"
            )
        if optional_b and not optional_a:
            self.log_panel.append(
                "只填写了参数 B（基础地址），缺少参数 A（密钥），本次按原始链接请求。", "warn"
            )

        resume = bool(self.resume_var.get())
        try:
            workers = int(self.workers_var.get())
        except ValueError:
            workers = DEFAULT_WORKERS
            self.workers_var.set(str(DEFAULT_WORKERS))
        workers = min(max(1, workers), MAX_WORKERS)

        # 关闭续传时窗口要自己持有临时目录：任务线程是守护线程，被强杀时核心
        # 里的清理来不及执行，得由窗口兜底删除。它建在**输出目录**下而不是
        # 系统临时目录——分片可能有几个 GB，写进系统盘会把它撑爆；建在输出目录
        # 里也保证不会往安装目录之类的地方乱写。
        self._cleanup_temp_root()
        if not resume:
            try:
                self._temp_root = Path(
                    tempfile.mkdtemp(prefix=".hls_session_", dir=str(output_path.parent))
                )
            except OSError as exc:
                messagebox.showerror(
                    "无法创建临时目录", f"{output_path.parent}\n{exc}", parent=self
                )
                return

        self._cancel_event = threading.Event()
        self.status_panel.reset()
        self.log_panel.append(
            f"下载参数：{workers} 路并发，断点续传{'开' if resume else '关'}。", "info"
        )
        self.log_panel.append(f"开始下载：{output_path}", "info")

        self._set_running(True)
        try:
            self._worker = threading.Thread(
                target=self._worker_main,
                args=(url, str(output_path), optional_a, optional_b, overwrite, resume, workers),
                name="hls-download",
                daemon=True,
            )
            self._worker.start()
        except Exception as exc:  # noqa: BLE001 - 线程创建失败不能把界面永久锁死
            self._worker = None
            self._cancel_event = None
            self._set_running(False)
            self.log_panel.append(f"无法启动后台任务：{exc}", "error")
            messagebox.showerror(
                "无法启动任务", f"后台线程创建失败：\n{exc}", parent=self
            )

    def _resolve_output_path(self) -> Path | None:
        """校验保存目录与文件名，返回最终输出路径；不合法时返回 ``None``。"""
        directory = self.dir_entry.get().strip()
        if not directory:
            directory = str(self._default_output_dir())
            self.dir_entry.set(directory)

        dir_path = Path(directory).expanduser()
        if dir_path.exists() and not dir_path.is_dir():
            messagebox.showerror("目录无效", f"保存路径不是目录：{dir_path}", parent=self)
            return None

        if not dir_path.exists():
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("无法创建目录", f"{dir_path}\n{exc}", parent=self)
                return None

        if not os.access(dir_path, os.W_OK):
            messagebox.showerror("目录不可写", f"没有写入权限：{dir_path}", parent=self)
            return None

        filename = sanitize_filename(self.filename_entry.get(), fallback="video")
        if not Path(filename).suffix:
            filename += default_output_suffix()
        self.filename_entry.set(filename)

        return dir_path / filename

    def _on_cancel(self) -> None:
        if self._cancel_event is None or not self._is_running():
            return
        self._cancel_event.set()
        self.cancel_button.configure(state="disabled", text="正在取消…")
        self.status_panel.show_stage(TaskStage.CANCELLING)
        self.log_panel.append("已发送取消请求，正在停止任务…", "warn")

    # ------------------------------------------------------------------
    # 在线播放
    # ------------------------------------------------------------------

    def _on_playback(self) -> None:
        """启动在线播放：只播放，不下载。

        在线播放与下载互不阻塞——它有自己的线程、取消信号和子进程，不参与
        :meth:`_set_running` 的按钮状态机，因此下载进行中照样可以在线播放。
        """
        if self._is_playing():
            self.log_panel.append("播放器已在播放，请先停止在线播放。", "warn")
            return

        url = self.url_entry.get().strip()
        try:
            validate_http_url(url)
        except InvalidUrlError as exc:
            self.log_panel.append(f"链接无效：{exc.message}", "error")
            messagebox.showwarning("链接无效", exc.message, parent=self)
            self.url_entry.focus_entry()
            return

        # 先做一次代价极低的可用性检查，省掉一次注定失败的请求
        if find_mpv() is None and find_ffplay() is None:
            message = (
                "未检测到可用的播放器，无法在线播放。请安装 mpv（有进度条与倍速），"
                "或安装 FFmpeg（其中带 ffplay，只提供基本播放）。"
            )
            self.playback_panel.show_failed(message)
            self.log_panel.append(message, "error")
            messagebox.showerror("无法在线播放", message, parent=self)
            return

        optional_a = self.optional_a_entry.get().strip() or None
        optional_b = self.optional_b_entry.get().strip() or None

        # 每次在线播放都有一个代号：上一轮迟到的消息据此丢弃，避免"停止后立刻
        # 重播"时旧消息把新状态覆盖掉。
        self._playback_generation += 1
        generation = self._playback_generation
        self._playback_cancel = threading.Event()
        # 事件对象作为参数传给线程，不要让它回头去读 self._playback_cancel：
        # 属性可能已被下一轮播放（或停止）换掉，那样这次请求就取消不掉了。
        cancel = self._playback_cancel
        self.playback_panel.show_inspecting()
        self.log_panel.append("正在获取播放列表用于在线播放（只取列表，不下载分片）…", "info")

        try:
            self._playback_worker = threading.Thread(
                target=self._playback_worker_main,
                args=(url, optional_a, optional_b, generation, cancel),
                name="hls-playback",
                daemon=True,
            )
            self._playback_worker.start()
        except Exception as exc:  # noqa: BLE001 - 线程创建失败不能锁死在线播放
            self._playback_worker = None
            self._playback_cancel = None
            message = f"无法启动在线播放：{exc}"
            self.playback_panel.show_failed(message)
            self.log_panel.append(message, "error")

    def _on_playback_stop(self) -> None:
        """停止播放：取消预检，或结束播放器进程。"""
        if self._playback_cancel is not None:
            self._playback_cancel.set()
        self._playback_generation += 1  # 让在途的在线播放消息作废

        if self._playback_poll_id is not None:
            try:
                self.after_cancel(self._playback_poll_id)
            except Exception:  # pragma: no cover - 解释器正在退出
                pass
            self._playback_poll_id = None

        process = self._playback_process
        self._playback_process = None
        self._playback_worker = None

        if process is not None and process.poll() is None:
            # 结束进程可能要先 terminate 再 kill，放到子线程里做，
            # 免得点一下「停止播放」界面就卡住。
            threading.Thread(
                target=stop_playback,
                args=(process,),
                name="hls-playback-stop",
                daemon=True,
            ).start()
            self.log_panel.append("已停止播放。", "info")
        else:
            self.log_panel.append("已取消在线播放。", "info")

        self.playback_panel.show_stopped()

    def _on_copy_playlist_url(self) -> None:
        """把按可选参数签名后的播放列表地址复制到剪贴板。

        给「在手机上播」用：算出来的地址是**自包含**的（签名就在 query 里），
        有效期内任何支持 HLS 的播放器都能直接打开，不需要装这个程序。
        """
        raw = self.url_entry.get().strip()
        optional_a = self.optional_a_entry.get().strip() or None
        optional_b = self.optional_b_entry.get().strip() or None

        try:
            # 与下载、在线播放共用同一个入口：三者算出的地址必然一致
            signed = prepare_playlist_url(raw, optional_a, optional_b)
        except HLSDownloaderError as exc:
            self.log_panel.append(f"无法生成播放地址：{exc.message}", "error")
            messagebox.showwarning("无法生成播放地址", exc.message, parent=self)
            return

        if optional_b and not optional_a:
            self.log_panel.append(
                "只填写了参数 B（基础地址），缺少参数 A（密钥），复制的是原始链接。",
                "warn",
            )

        try:
            self.clipboard_clear()
            self.clipboard_append(signed)
            # Tk 的剪贴板是延迟提交的，不 update 的话程序一退出内容就没了
            self.update()
        except Exception as exc:  # noqa: BLE001 - 剪贴板被别的程序占着是常事
            self.log_panel.append(f"复制失败：{exc.__class__.__name__}：{exc}", "error")
            return

        self.log_panel.append(
            "已复制播放地址到剪贴板（含签名，2 小时内有效）。"
            "可直接粘到手机上的 VLC / mpv 等播放器播放；请勿外传。",
            "success",
        )

    def _on_playback_speed(self, speed: float) -> None:
        """面板上的倍速按钮：实时改，不用重启播放器。

        mpv 的窗口里**没有**倍速控件（它的 osc.lua 编译在 exe 里，只提供可拖的
        进度条），所以这个按钮只能由我们给。进度条则相反——留在窗口里。
        """
        self._playback_speed = speed
        process = self._playback_process
        if process is None or not process.supports_control:
            return
        if process.set_speed(speed):
            self.log_panel.append(f"播放速度已设为 {speed:g}×。", "info")
        else:
            self.log_panel.append("设置倍速失败：播放器没有响应。", "warn")

    def _is_playing(self) -> bool:
        """在线播放是否正在进行（预检中或播放中）。"""
        process = self._playback_process
        if process is not None and process.poll() is None:
            return True
        return self._playback_worker is not None and self._playback_worker.is_alive()

    def _playback_worker_main(
        self,
        url: str,
        optional_a: str | None,
        optional_b: str | None,
        generation: int,
        cancel_event: threading.Event,
    ) -> None:
        """后台线程入口：只做预检并通过队列汇报，不碰任何控件、不启动进程。"""
        try:
            info = inspect_playlist(
                url,
                optional_a,
                optional_b,
                cancel_event=cancel_event,
            )
        except DownloadCancelled:
            self._message_queue.put((MSG_PLAYBACK_CANCELLED, (generation, None)))
        except HLSDownloaderError as exc:
            self._message_queue.put((MSG_PLAYBACK_FAILED, (generation, exc.message)))
        except Exception as exc:  # noqa: BLE001 - 任何异常都不该让在线播放卡死
            self._message_queue.put(
                (MSG_PLAYBACK_FAILED, (generation, f"在线播放失败：{exc.__class__.__name__}：{exc}"))
            )
        else:
            self._message_queue.put((MSG_PLAYBACK_READY, (generation, info)))

    def _apply_playback_result(self, kind: str, generation: int, payload: object) -> None:
        """在主线程处理预检结果；过期消息直接丢弃。"""
        if generation != self._playback_generation:
            return

        self._playback_worker = None
        self._playback_cancel = None

        if kind == MSG_PLAYBACK_READY:
            if not isinstance(payload, PlaybackInfo):
                # 内部约定被破坏。不用 assert（python -O 下会失效），宁可当作
                # 失败处理，也不要让面板永远停在「正在获取播放列表…」。
                self.playback_panel.show_failed("内部错误：播放信息无效，详见日志。")
                self.log_panel.append(
                    f"在线播放消息载荷类型异常：{type(payload).__name__}", "error"
                )
                return
            self._start_playback_process(payload)
        elif kind == MSG_PLAYBACK_FAILED:
            message = str(payload)
            self.playback_panel.show_failed(message)
            self.log_panel.append(f"在线播放失败：{message}", "error")
        else:  # MSG_PLAYBACK_CANCELLED
            self.playback_panel.show_stopped()
            self.log_panel.append("已取消在线播放。", "warn")

    def _start_playback_process(self, info: PlaybackInfo) -> None:
        """启动播放器进程。只在 GUI 主线程里碰子进程。"""
        try:
            # 沿用上次选中的倍速，免得每次都从 1× 重新调
            process = launch_playback(info.url, speed=self._playback_speed)
        except PlayerError as exc:
            self.playback_panel.show_failed(exc.message)
            self.log_panel.append(f"在线播放失败：{exc.message}", "error")
            return

        self._playback_process = process
        self.playback_panel.show_playing(
            info, player_name=process.name, controlled=process.supports_control
        )
        for warning in info.warnings:
            self.log_panel.append(f"提示：{warning}", "warn")
        self.log_panel.append(
            f"已启动播放器 {process.name}（{format_playback_summary(info)}）。"
            "播放器自行在线取流，程序不保存任何分片。",
            "info",
        )
        if not process.supports_control:
            self.log_panel.append(
                "该播放器没有控制通道：进度条与倍速不可用。安装 mpv 后可启用。",
                "warn",
            )
        self._poll_playback_process()

    def _poll_playback_process(self) -> None:
        """轮询播放器进程：用户自己关掉播放器窗口后要把面板恢复回来。"""
        self._playback_poll_id = None
        process = self._playback_process
        if process is None or self._closing:
            return

        code = process.poll()
        if code is None:
            # 位置由播放器推来（mpv 的 IPC 是异步事件，只能靠轮询取快照）。
            # 不支持的播放器这两个方法返回 None，面板保持原样。
            self.playback_panel.update_progress(process.position(), process.duration())
            try:
                self._playback_poll_id = self.after(
                    PLAYBACK_POLL_INTERVAL_MS, self._poll_playback_process
                )
            except Exception:  # pragma: no cover - 解释器正在退出
                self._playback_poll_id = None
            return

        self._playback_process = None
        # 用户点「停止播放」的路径会先取消轮询，所以走到这里一定是播放器自己
        # 退出的。
        self._report_playback_exit(code, process)

    def _report_playback_exit(self, code: int, process) -> None:
        """播放器自己退出后，把结果讲清楚。

        不能只看返回码：实测 ffplay 遇到 404、不支持的编码这类致命错误时
        **同样以 0 退出**。真正的信号是它有没有报错（mpv 由 ``end-file`` 原因与
        错误日志给出，ffplay 只能看 stderr），以及是不是刚起来就退了。
        """
        hint = process.failure_hint() if hasattr(process, "failure_hint") else ""
        elapsed = getattr(process, "elapsed_seconds", None)
        elapsed = elapsed() if callable(elapsed) else None
        ended_with_error = False
        checker = getattr(process, "ended_with_error", None)
        if callable(checker):
            ended_with_error = bool(checker())

        if not ended_with_error and code == 0 and not hint:
            self.playback_panel.show_stopped()
            self.log_panel.append("播放器已结束播放。", "info")
            return

        if not ended_with_error and elapsed is not None and elapsed < PLAYBACK_STARTUP_GRACE_SECONDS:
            # 没报错但刚起来就退了：多半是启动就失败
            ended_with_error = True

        if elapsed is not None and elapsed < PLAYBACK_STARTUP_GRACE_SECONDS:
            self.playback_panel.show_failed("播放器未能开始播放，原因见日志。")
            self.log_panel.append(
                f"播放器启动后 {elapsed:.1f} 秒即退出（返回码 {code}），它没能开始播放。",
                "error",
            )
            level = "error"
        else:
            played = f"，已播放 {format_elapsed(elapsed)}" if elapsed else ""
            self.playback_panel.show_stopped()
            self.log_panel.append(
                f"播放器已结束{played}（返回码 {code}），但期间有错误。", "warn"
            )
            level = "warn"

        if hint:
            self.log_panel.append(f"播放器输出：\n{hint}", level)
        else:
            self.log_panel.append(
                "播放器没有给出错误详情。若怀疑是链接问题，可以用同一个链接试一次"
                "「开始下载」：下载能取到播放列表和分片、而播放器播不了，通常说明"
                "播放器不支持该内容的编码或容器。",
                "warn",
            )

    def _stop_playback_on_close(self) -> None:
        """关窗时结束在线播放。

        播放器是独立进程，不主动结束的话窗口关掉它还会留在桌面上。这里不走
        :func:`~hls_downloader.player.stop_playback` 的等待逻辑：窗口马上就
        销毁了，直接请求终止即可。
        """
        if self._playback_cancel is not None:
            self._playback_cancel.set()
        self._playback_generation += 1

        if self._playback_poll_id is not None:
            try:
                self.after_cancel(self._playback_poll_id)
            except Exception:  # pragma: no cover - 解释器正在退出
                pass
            self._playback_poll_id = None

        process = self._playback_process
        self._playback_process = None
        if process is None:
            return

        # 先断 IPC 通道，否则读线程会一直挂在管道上
        close = getattr(process, "close", None)
        if callable(close):
            try:
                close()
            except OSError:  # pragma: no cover - 通道可能已经断了
                pass

        if process.poll() is None:
            try:
                process.terminate()
            except OSError:  # pragma: no cover - 进程可能刚好自己退了
                pass

    # ------------------------------------------------------------------
    # 后台任务与消息处理
    # ------------------------------------------------------------------

    def _is_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(
            state="normal" if running else "disabled",
            text="取消下载",
        )
        for widget in (
            self.url_entry,
            self.optional_a_entry,
            self.optional_b_entry,
            self.dir_entry,
            self.filename_entry,
        ):
            widget.set_enabled(not running)
        self.overwrite_check.configure(state="disabled" if running else "normal")
        self.resume_check.configure(state="disabled" if running else "normal")
        self.workers_menu.configure(state="disabled" if running else "normal")

    def _worker_main(
        self,
        url: str,
        output_path: str,
        optional_a: str | None,
        optional_b: str | None,
        overwrite: bool,
        resume: bool,
        workers: int,
    ) -> None:
        """后台线程入口：只调用核心函数并通过队列汇报结果。"""
        handler = _QueueLogHandler(self._message_queue)
        handler.setFormatter(logging.Formatter("%(message)s"))
        task_logger = logging.getLogger("hls_downloader.task")
        task_logger.setLevel(logging.INFO)
        task_logger.propagate = False
        task_logger.handlers = [handler]

        try:
            result = download_hls_video(
                playlist_url=url,
                output_path=output_path,
                optional_a=optional_a,
                optional_b=optional_b,
                progress_callback=self._emit_progress,
                cancel_event=self._cancel_event,
                overwrite=overwrite,
                workers=workers,
                resume=resume,
                # 开启续传时不能把分片放进「关闭窗口就删除」的会话目录，
                # 否则重启程序后续不上；交给核心放到输出目录下的固定位置。
                temp_dir=str(self._temp_root) if self._temp_root else None,
                logger=task_logger,
            )
        except DownloadCancelled:
            self._message_queue.put((MSG_CANCELLED, None))
        except HLSDownloaderError as exc:
            self._message_queue.put((MSG_FAILED, exc.message))
        except Exception as exc:  # noqa: BLE001 - 兜底，避免界面永久卡死
            self._message_queue.put(
                (MSG_FAILED, f"发生未预期的错误（{exc.__class__.__name__}）：{exc}")
            )
        else:
            self._message_queue.put((MSG_DONE, result))
        finally:
            task_logger.handlers = []

    def _emit_progress(self, report: ProgressReport) -> None:
        if self._closing:
            return
        self._message_queue.put((MSG_PROGRESS, report))

    def _poll_queue(self) -> None:
        """消费后台线程发来的消息。

        单条消息处理失败**绝不能**中断轮询：一旦停止重新排程，队列就再没有
        消费者，任务结束消息也没人处理——按钮会永久停在禁用状态，界面彻底
        卡死（CLAUDE.md 第八节第 6 条）。因此这里逐条兜底，并把重新排程放进
        ``finally``。
        """
        self._poll_after_id = None
        try:
            while True:
                try:
                    kind, payload = self._message_queue.get_nowait()
                except queue.Empty:
                    break

                try:
                    self._handle_message(kind, payload)
                except Exception as exc:  # noqa: BLE001 - 单条消息失败不应拖垮轮询
                    _logger.exception("处理界面消息失败，已跳过该条消息。")
                    self._report_handler_error(exc)
        finally:
            self._schedule_poll()

    def _schedule_poll(self) -> None:
        """安排下一次轮询；窗口已关闭时不再排队。"""
        if self._closing:
            return
        try:
            self._poll_after_id = self.after(POLL_INTERVAL_MS, self._poll_queue)
        except Exception:  # pragma: no cover - 解释器正在退出
            self._poll_after_id = None

    def _report_handler_error(self, exc: BaseException) -> None:
        """把消息处理异常提示给用户；该函数自身不得再抛异常。"""
        try:
            self.log_panel.append(
                f"界面消息处理异常（已跳过该条消息）：{exc.__class__.__name__}：{exc}",
                "error",
            )
        except Exception:  # pragma: no cover - 控件可能已销毁
            pass

    def _handle_message(self, kind: str, payload: object) -> None:
        if kind == MSG_LOG:
            level, text = payload  # type: ignore[misc]
            self.log_panel.append(str(text), str(level))
        elif kind == MSG_PROGRESS:
            self.status_panel.update_from_report(payload)  # type: ignore[arg-type]
        elif kind == MSG_DONE:
            result = payload
            assert isinstance(result, DownloadResult)
            self._set_running(False)
            self._cleanup_temp_root()
            self.status_panel.show_completed(result)
            self.log_panel.append(
                "下载完成："
                f"{result.output_path}（{format_bytes(result.total_bytes)}，"
                f"用时 {format_elapsed(result.elapsed_seconds)}）",
                "success",
            )
            for warning in result.warnings:
                self.log_panel.append(f"提示：{warning}", "warn")
        elif kind == MSG_FAILED:
            message = str(payload)
            self._set_running(False)
            self._cleanup_temp_root()
            self.status_panel.show_failed(message)
            self.log_panel.append(f"下载失败：{message}", "error")
            messagebox.showerror("下载失败", message, parent=self)
        elif kind == MSG_CANCELLED:
            self._set_running(False)
            self._cleanup_temp_root()
            self.status_panel.show_cancelled()
            self.log_panel.append("任务已取消，临时文件已清理。", "warn")
        elif kind in (MSG_PLAYBACK_READY, MSG_PLAYBACK_FAILED, MSG_PLAYBACK_CANCELLED):
            generation, playback_payload = payload  # type: ignore[misc]
            self._apply_playback_result(kind, int(generation), playback_payload)

    # ------------------------------------------------------------------
    # 关闭处理
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if self._is_running():
            if not messagebox.askyesno(
                "确认退出",
                "下载任务仍在进行中，确定要退出并取消任务吗？",
                parent=self,
            ):
                return
            # 用户已经确认退出：在线播放没必要再陪着等下载线程收尾
            self._stop_playback_on_close()
            if self._cancel_event is not None:
                self._cancel_event.set()
            self.log_panel.append("正在取消任务并退出…", "warn")
            # 不要在 GUI 线程上 join：任务正卡在网络读取时，join 会让窗口
            # 假死数秒（Windows 会显示"未响应"）。改为轮询等待，界面保持响应。
            self._await_worker_then_close(time.monotonic() + SHUTDOWN_GRACE_SECONDS)
            return

        self._finish_close()

    def _await_worker_then_close(self, deadline: float) -> None:
        """等待后台线程收尾，但不阻塞界面。"""
        worker = self._worker
        if worker is None or not worker.is_alive() or time.monotonic() >= deadline:
            self._finish_close()
            return
        try:
            self.after(50, lambda: self._await_worker_then_close(deadline))
        except Exception:  # pragma: no cover - 解释器正在退出
            self._finish_close()

    def _finish_close(self) -> None:
        """真正关闭窗口。

        可重复调用：宽限期里用户再点一次关闭会启动第二条等待链，两条链都会
        走到这里，而对已销毁的窗口再调一次 ``destroy()`` 会抛 ``TclError``。
        因此用 ``_closing`` 做幂等保护，只有第一次生效。
        """
        if self._closing:
            return
        self._closing = True

        # 取消尚未触发的轮询，避免回调打到已销毁的控件
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:  # pragma: no cover - 解释器可能已开始退出
                pass
            self._poll_after_id = None

        self._stop_playback_on_close()
        self._cleanup_temp_root()
        self.destroy()

    def _cleanup_temp_root(self) -> None:
        """删除本窗口持有的临时根目录。

        join 超时后进程退出会直接杀掉守护线程，核心里的 ``finally`` 清理来不及
        执行；这里做兜底，保证关闭后不留下临时分片或 ``.part`` 文件。

        Windows 上若任务线程恰好还持有某个文件的句柄，删除会失败；因此有限次
        重试，而不是删一次就算了。
        """
        root = self._temp_root
        self._temp_root = None
        if root is None:
            return

        for _ in range(3):
            shutil.rmtree(root, ignore_errors=True)
            if not root.exists():
                return
            time.sleep(0.1)
