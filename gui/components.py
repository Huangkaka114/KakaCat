"""可复用的界面组件：卡片、带标签的输入框、状态面板、日志面板。

这些组件只关心展示与基本交互，不包含任何下载逻辑。
"""

from __future__ import annotations

import time
from typing import Callable

import customtkinter as ctk

from hls_downloader.models import DownloadResult, ProgressReport, TaskStage
from hls_downloader.playback import PlaybackInfo
from hls_downloader.player import SPEED_CHOICES

from . import theme
from .theme import Fonts

#: 用 mpv 时的提示：进度条在窗口里，倍速在这块面板上
CONTROL_HINT_MPV = (
    "进度条在播放器窗口里（拖动跳转，空格暂停，f 全屏）；"
    "倍速用下面的按钮，或用播放器窗口里的 [ / ] 快捷键。"
)
#: ffplay 没有窗口内进度条，也没有控制通道
CONTROL_HINT_FFPLAY = (
    "当前播放器（ffplay）窗口里没有进度条与倍速；安装 mpv 后即可在窗口里拖动跳转与调速。"
)

#: 日志区域最多保留的行数，避免长时间运行后占用过多内存
MAX_LOG_LINES = 1500


# --------------------------------------------------------------------------
# 格式化辅助函数
# --------------------------------------------------------------------------


def format_bytes(size: float) -> str:
    """把字节数格式化为易读的字符串。"""
    if size <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{value:.0f} {units[index]}"
    return f"{value:.1f} {units[index]}"


def format_speed(bytes_per_second: float) -> str:
    """格式化下载速度；没有有效数据时返回占位符。"""
    if bytes_per_second <= 0:
        return "—"
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: float | None) -> str:
    """格式化时长；``None`` 或非正值返回占位符。"""
    if seconds is None or seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分 {int(seconds % 60)} 秒"
    return f"{int(seconds // 3600)} 小时 {int(seconds % 3600 // 60)} 分"


def format_elapsed(seconds: float) -> str:
    """格式化耗时（允许 0，用于任务完成后展示）。"""
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    return format_duration(seconds)


def format_clock(seconds: float | None) -> str:
    """把秒数格式化成播放器式的时间（``12:34`` / ``1:02:03``）。

    与 :func:`format_duration` 不同：那个说的是人话（"1 分 30 秒"），这个要在
    进度条旁边跟着秒数跳动，用 ``mm:ss`` 才不晃眼。
    """
    if seconds is None or seconds < 0:
        return "—"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_playback_summary(info: PlaybackInfo) -> str:
    """把在线播放的预检结果浓缩成一行说明。

    主播放列表与直播列表拿不到分片级信息，只说明播放器会怎么处理；普通 VOD
    则给出分片数、总时长和加密情况。
    """
    if info.is_master:
        return "主播放列表 · 播放器将自行选择码率"
    if info.is_live:
        return "直播或尚未结束的列表 · 将持续播放"

    parts = [f"{info.segment_count} 个分片"]
    if info.total_duration > 0:
        parts.append(f"约 {format_duration(info.total_duration)}")
    # 播放列表显式写了 METHOD=NONE 时解析出来是字符串 "NONE"，那同样是未加密
    method = info.encryption_method
    if method and method.upper() != "NONE":
        parts.append(f"加密：{method}")
    else:
        parts.append("未加密")
    return " · ".join(parts)


# --------------------------------------------------------------------------
# 基础组件
# --------------------------------------------------------------------------


class Card(ctk.CTkFrame):
    """统一风格的圆角卡片面板。"""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("fg_color", theme.BG_PANEL)
        kwargs.setdefault("corner_radius", theme.RADIUS_CARD)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", theme.BORDER)
        super().__init__(master, **kwargs)


class SectionTitle(ctk.CTkFrame):
    """带蓝色竖条的分区标题。"""

    def __init__(self, master, text: str, fonts: Fonts, **kwargs):
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        bar = ctk.CTkFrame(
            self, width=3, height=16, corner_radius=2, fg_color=theme.ACCENT
        )
        bar.grid(row=0, column=0, sticky="ns", padx=(0, theme.SPACE_SM))
        bar.grid_propagate(False)
        ctk.CTkLabel(self, text=text, font=fonts.section, text_color=theme.TEXT).grid(
            row=0, column=1, sticky="w"
        )


class LabeledEntry(ctk.CTkFrame):
    """标签 + 输入框（可选尾部按钮、底部提示文字）。"""

    def __init__(
        self,
        master,
        fonts: Fonts,
        label: str,
        placeholder: str = "",
        hint: str | None = None,
        button_text: str | None = None,
        button_command: Callable[[], None] | None = None,
        button_width: int = 96,
        **kwargs,
    ):
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        self.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text=label, font=fonts.body, text_color=theme.TEXT, anchor="w"
        ).grid(row=0, column=0, columnspan=2, sticky="ew")

        self.entry = ctk.CTkEntry(
            self,
            height=theme.HEIGHT_INPUT,
            corner_radius=theme.RADIUS_INPUT,
            font=fonts.body,
            fg_color=theme.BG_INPUT,
            border_color=theme.BORDER,
            border_width=1,
            text_color=theme.TEXT,
            placeholder_text=placeholder,
            placeholder_text_color=theme.TEXT_DIM,
        )
        self.entry.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        self.button = None
        if button_text:
            self.button = ctk.CTkButton(
                self,
                text=button_text,
                command=button_command,
                width=button_width,
                height=theme.HEIGHT_INPUT,
                corner_radius=theme.RADIUS_INPUT,
                font=fonts.small,
                fg_color=theme.BG_HOVER,
                hover_color=theme.BORDER_STRONG,
                text_color=theme.TEXT,
                border_width=1,
                border_color=theme.BORDER,
            )
            self.button.grid(row=1, column=1, sticky="e", padx=(theme.SPACE_SM, 0), pady=(6, 0))

        if hint:
            ctk.CTkLabel(
                self,
                text=hint,
                font=fonts.hint,
                text_color=theme.TEXT_DIM,
                anchor="w",
                justify="left",
            ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    def get(self) -> str:
        return self.entry.get()

    def set(self, value: str) -> None:
        self.entry.delete(0, "end")
        if value:
            self.entry.insert(0, value)

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state)
        if self.button is not None:
            self.button.configure(state=state)

    def focus_entry(self) -> None:
        self.entry.focus_set()


# --------------------------------------------------------------------------
# 状态与进度面板
# --------------------------------------------------------------------------


class _Metric(ctk.CTkFrame):
    """状态面板中的一个指标（标题 + 数值）。

    ``tile=True`` 时带底色与圆角，成为一块独立的小磁贴；用于横跨整窗的状态带。
    """

    def __init__(
        self, master, fonts: Fonts, caption: str, *, tile: bool = False, **kwargs
    ):
        kwargs.setdefault("fg_color", theme.BG_INPUT if tile else "transparent")
        if tile:
            kwargs.setdefault("corner_radius", theme.RADIUS_INPUT)
        super().__init__(master, **kwargs)

        pad = theme.SPACE_SM if tile else 0
        self.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self, text=caption, font=fonts.hint, text_color=theme.TEXT_DIM, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=pad, pady=(pad, 0))
        self.value = ctk.CTkLabel(
            self, text="—", font=fonts.metric, text_color=theme.TEXT, anchor="w"
        )
        self.value.grid(row=1, column=0, sticky="ew", padx=pad, pady=(2, pad))

    def set(self, text: str, color: str = theme.TEXT) -> None:
        self.value.configure(text=text, text_color=color)


def _badge_text(text: str) -> str:
    """给徽标文字补两侧留白。

    ``CTkLabel`` 没有内边距选项，胶囊底色会紧贴文字，因此用空格把视觉留白
    补出来。
    """
    return f"   {text}   "


class StatusPanel(Card):
    """显示任务阶段、进度、分片数量、速度和预计剩余时间。

    布局按「横跨整个窗口的状态带」设计：左上角是分区标题与阶段徽标，右上角是
    大号百分比，下面依次是加粗的进度条、提示文字和四块指标磁贴。
    """

    def __init__(self, master, fonts: Fonts, **kwargs):
        super().__init__(master, **kwargs)
        self._fonts = fonts
        self.columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(
            row=0, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_SM, theme.SPACE_SM)
        )
        header.columnconfigure(2, weight=1)

        SectionTitle(header, "下载状态", fonts).grid(row=0, column=0, sticky="w")

        self._stage_label = ctk.CTkLabel(
            header,
            text=_badge_text(TaskStage.IDLE.label),
            font=fonts.hint,
            text_color=theme.ACCENT,
            fg_color=theme.BG_HOVER,
            corner_radius=11,
            height=24,
        )
        self._stage_label.grid(row=0, column=1, sticky="w", padx=(theme.SPACE_MD, 0))

        self._percent_display = ctk.CTkLabel(
            header, text="0.0%", font=fonts.display, text_color=theme.ACCENT, anchor="e"
        )
        self._percent_display.grid(row=0, column=3, sticky="e")

        self._progress_bar = ctk.CTkProgressBar(
            self,
            height=14,
            corner_radius=7,
            fg_color=theme.PROGRESS_TRACK,
            progress_color=theme.ACCENT,
        )
        self._progress_bar.set(0.0)
        self._progress_bar.grid(
            row=1, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(0, theme.SPACE_SM)
        )

        self._message_label = ctk.CTkLabel(
            self, text="等待开始", font=fonts.hint, text_color=theme.TEXT_DIM, anchor="w"
        )
        self._message_label.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_LG)

        metrics = ctk.CTkFrame(self, fg_color="transparent")
        metrics.grid(
            row=3, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_SM, theme.SPACE_MD)
        )
        for column in range(4):
            metrics.columnconfigure(column, weight=1, uniform="metric")

        self._percent = _Metric(metrics, fonts, "进度", tile=True)
        self._percent.grid(row=0, column=0, sticky="ew", padx=(0, theme.SPACE_SM))
        self._segments = _Metric(metrics, fonts, "分片", tile=True)
        self._segments.grid(row=0, column=1, sticky="ew", padx=theme.SPACE_SM)
        self._speed = _Metric(metrics, fonts, "速度", tile=True)
        self._speed.grid(row=0, column=2, sticky="ew", padx=theme.SPACE_SM)
        self._eta = _Metric(metrics, fonts, "剩余时间", tile=True)
        self._eta.grid(row=0, column=3, sticky="ew", padx=(theme.SPACE_SM, 0))

    # -- 状态更新 ---------------------------------------------------------

    def reset(self) -> None:
        """回到“等待开始”状态。"""
        self._apply(
            stage=TaskStage.IDLE,
            color=theme.TEXT,
            message="等待开始",
            percent=0.0,
            segments="0 / 0",
            speed="—",
            eta="—",
        )

    def update_from_report(self, report: ProgressReport) -> None:
        """按真实的进度报告刷新界面。"""
        color = {
            TaskStage.COMPLETED: theme.SUCCESS,
            TaskStage.FAILED: theme.ERROR,
            TaskStage.CANCELLED: theme.WARNING,
            TaskStage.CANCELLING: theme.WARNING,
        }.get(report.stage, theme.ACCENT)

        segments = (
            f"{report.completed_segments} / {report.total_segments}"
            if report.total_segments
            else "0 / 0"
        )
        self._apply(
            stage=report.stage,
            color=color,
            message=report.message or report.stage_label,
            percent=report.percent / 100.0,
            segments=segments,
            speed=format_speed(report.speed_bps),
            eta=format_duration(report.eta_seconds),
            percent_text=f"{report.percent:.1f}%",
        )

    def show_completed(self, result: DownloadResult) -> None:
        detail = (
            f"已生成 {result.output_path}（{format_bytes(result.total_bytes)}，"
            f"用时 {format_elapsed(result.elapsed_seconds)}）"
        )
        self._apply(
            stage=TaskStage.COMPLETED,
            color=theme.SUCCESS,
            message=detail,
            percent=1.0,
            segments=f"{result.segment_count} / {result.segment_count}",
            speed="—",
            eta="—",
            percent_text="100%",
        )

    def show_failed(self, message: str) -> None:
        self._set_stage(TaskStage.FAILED, theme.ERROR)
        self._message_label.configure(text=message.splitlines()[0] if message else "下载失败")
        self._progress_bar.configure(progress_color=theme.ERROR)

    def show_cancelled(self) -> None:
        self._set_stage(TaskStage.CANCELLED, theme.WARNING)
        self._message_label.configure(text="任务已取消，临时文件已清理。")
        self._progress_bar.configure(progress_color=theme.WARNING)

    def show_stage(self, stage: TaskStage) -> None:
        self._set_stage(stage, theme.WARNING)

    def _set_stage(self, stage: TaskStage, color: str) -> None:
        self._stage_label.configure(text=_badge_text(stage.label), text_color=color)

    # -- 内部实现 ---------------------------------------------------------

    def _apply(
        self,
        *,
        stage: TaskStage,
        color: str,
        message: str,
        percent: float,
        segments: str,
        speed: str,
        eta: str,
        percent_text: str | None = None,
    ) -> None:
        self._set_stage(stage, color)
        self._message_label.configure(text=message)
        self._progress_bar.configure(progress_color=color)
        self._progress_bar.set(max(0.0, min(1.0, percent)))
        text = percent_text or f"{percent * 100:.1f}%"
        self._percent.set(text, color)
        self._percent_display.configure(text=text, text_color=color)
        self._segments.set(segments)
        self._speed.set(speed)
        self._eta.set(eta)


# --------------------------------------------------------------------------
# 日志面板
# --------------------------------------------------------------------------


class LogPanel(Card):
    """可滚动的运行日志区域。"""

    def __init__(self, master, fonts: Fonts, **kwargs):
        super().__init__(master, **kwargs)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._line_count = 0

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(
            row=0, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_MD, theme.SPACE_SM)
        )
        header.columnconfigure(0, weight=1)

        SectionTitle(header, "运行日志", fonts).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            header,
            text="清空日志",
            width=88,
            height=28,
            corner_radius=theme.RADIUS_BUTTON,
            font=fonts.small,
            fg_color=theme.BG_HOVER,
            hover_color=theme.BORDER_STRONG,
            text_color=theme.TEXT,
            command=self.clear,
        ).grid(row=0, column=1, sticky="e")

        self.textbox = ctk.CTkTextbox(
            self,
            height=150,
            font=fonts.log,
            fg_color=theme.BG_LOG,
            border_width=1,
            border_color=theme.BORDER,
            corner_radius=theme.RADIUS_INPUT,
            text_color=theme.TEXT_SECONDARY,
            wrap="word",
            activate_scrollbars=True,
        )
        self.textbox.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=theme.SPACE_LG,
            pady=(0, theme.SPACE_LG),
        )

        self.textbox.tag_config("ts", foreground=theme.TEXT_DIM)
        self.textbox.tag_config("info", foreground=theme.TEXT_SECONDARY)
        self.textbox.tag_config("success", foreground=theme.SUCCESS)
        self.textbox.tag_config("warn", foreground=theme.WARNING)
        self.textbox.tag_config("error", foreground=theme.ERROR)
        self.textbox.configure(state="disabled")

    def append(self, message: str, level: str = "info") -> None:
        """追加一条日志。``level`` 取 ``info``/``success``/``warn``/``error``。

        消息本身可能含换行（错误提示常是多行），所以裁剪必须按**实际写入的
        物理行数**进行，而不是按消息条数——否则行数上限会被突破数倍。
        """
        if level not in ("info", "success", "warn", "error"):
            level = "info"

        self.textbox.configure(state="normal")
        self.textbox.insert("end", f"[{time.strftime('%H:%M:%S')}] ", "ts")
        self.textbox.insert("end", message.rstrip() + "\n", level)
        self._trim_to_limit()

        self._scroll_to_end()
        self.textbox.configure(state="disabled")

    def _trim_to_limit(self) -> None:
        """按文本框的实际行数裁剪到上限。"""
        self._line_count = self._count_lines()
        if self._line_count > MAX_LOG_LINES:
            excess = self._line_count - MAX_LOG_LINES
            self.textbox.delete("1.0", f"{excess + 1}.0")
            self._line_count = self._count_lines()

    def _count_lines(self) -> int:
        """行数交给 Tk 自己数（``end-1c`` 的行号就是物理行数），避免手数出错。"""
        try:
            return int(str(self.textbox.index("end-1c")).split(".")[0])
        except Exception:  # pragma: no cover - 控件可能已销毁
            return self._line_count

    def clear(self) -> None:
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self._line_count = 0
        self.textbox.configure(state="disabled")

    def scroll_to_end(self) -> None:
        """滚动到最新一行（供“查看运行日志”快捷操作用）。"""
        self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        try:
            self.textbox.see("end")
        except AttributeError:  # pragma: no cover - 兼容不同 CTk 版本
            self.textbox.yview_moveto(1.0)


# --------------------------------------------------------------------------
# 在线播放面板
# --------------------------------------------------------------------------


class PlaybackPanel(Card):
    """在线播放面板：只播放，不下载。

    面板自己不碰网络，也不碰播放器进程——它只把「播放 / 停止」转交给主窗口的
    回调，并展示当前状态。播放器进程的生命周期完全由 :class:`~gui.main_window.MainWindow`
    管理，这样线程与子进程的规则只在一个地方。
    """

    def __init__(
        self,
        master,
        fonts: Fonts,
        *,
        on_play: Callable[[], None],
        on_stop: Callable[[], None],
        on_copy: Callable[[], None],
        on_speed: Callable[[float], None],
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self._fonts = fonts
        self._duration: float | None = None
        self._on_speed = on_speed
        #: 当前倍速在 SPEED_CHOICES 里的下标
        self._speed_index = SPEED_CHOICES.index(1.0)
        #: 播放器有没有把位置/时长报给我们的通道（mpv 有，ffplay 没有）
        self._has_control_channel = False
        self.columnconfigure(0, weight=1)

        SectionTitle(self, "在线播放", fonts).grid(
            row=0,
            column=0,
            sticky="w",
            padx=theme.SPACE_LG,
            pady=(theme.SPACE_LG, theme.SPACE_SM),
        )

        ctk.CTkLabel(
            self,
            text=(
                "只播放，不下载：不写临时文件，也不占用保存目录。"
                "使用上面填写的链接与可选参数，可与下载同时进行。"
            ),
            font=fonts.small,
            text_color=theme.TEXT_SECONDARY,
            justify="left",
            anchor="w",
            wraplength=380,
        ).grid(row=1, column=0, sticky="ew", padx=theme.SPACE_LG)

        self._info_label = ctk.CTkLabel(
            self,
            text="尚未播放。",
            font=fonts.hint,
            text_color=theme.TEXT_DIM,
            justify="left",
            anchor="w",
            wraplength=380,
        )
        self._info_label.grid(
            row=2, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_SM, 0)
        )

        # 播放控制在**播放器窗口里**做（mpv 的 OSC 进度条与快捷键），这里只说明
        # 去哪儿操作，不再另做一套控件——同一个功能放两个地方只会让人犯迷糊。
        self._hint_label = ctk.CTkLabel(
            self,
            text="",
            font=fonts.hint,
            text_color=theme.TEXT_DIM,
            justify="left",
            anchor="w",
            wraplength=380,
        )
        self._hint_label.grid(row=3, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_SM, 0))

        # 只读的播放进度文字：mpv 会把位置推给我们，属于状态展示而非控制
        self._time_label = ctk.CTkLabel(
            self,
            text="",
            font=fonts.hint,
            text_color=theme.TEXT_DIM,
            anchor="w",
        )
        self._time_label.grid(row=4, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(2, 0))

        # 倍速只能由我们提供按钮：mpv 窗口里**没有**倍速控件（它的 osc.lua 是编译
        # 进 exe 的，只有一个可拖的进度条），只有 [ / ] 快捷键。进度条留在播放器
        # 窗口里，倍速在这里给一组按钮。
        speed_row = ctk.CTkFrame(self, fg_color="transparent")
        speed_row.grid(row=5, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_SM, 0))
        speed_row.columnconfigure(4, weight=1)

        ctk.CTkLabel(
            speed_row, text="倍速", font=fonts.hint, text_color=theme.TEXT_DIM
        ).grid(row=0, column=0, sticky="w", padx=(0, theme.SPACE_SM))

        self._slower_button = self._speed_button(speed_row, "−", -1)
        self._slower_button.grid(row=0, column=1, sticky="w")

        self._speed_label = ctk.CTkLabel(
            speed_row, text="1×", font=fonts.body_bold, text_color=theme.TEXT, width=52
        )
        self._speed_label.grid(row=0, column=2, sticky="w", padx=theme.SPACE_SM)

        self._faster_button = self._speed_button(speed_row, "＋", 1)
        self._faster_button.grid(row=0, column=3, sticky="w")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(
            row=6,
            column=0,
            sticky="ew",
            padx=theme.SPACE_LG,
            pady=(theme.SPACE_MD, theme.SPACE_LG),
        )
        buttons.columnconfigure(0, weight=1, uniform="playback")
        buttons.columnconfigure(1, weight=1, uniform="playback")

        self.play_button = ctk.CTkButton(
            buttons,
            text="播放",
            font=fonts.body_bold,
            height=theme.HEIGHT_BUTTON,
            corner_radius=theme.RADIUS_BUTTON,
            fg_color=theme.PRIMARY,
            hover_color=theme.PRIMARY_HOVER,
            text_color=theme.PRIMARY_TEXT,
            command=on_play,
        )
        self.play_button.grid(row=0, column=0, sticky="ew", padx=(0, theme.SPACE_SM))

        self.stop_button = ctk.CTkButton(
            buttons,
            text="停止播放",
            font=fonts.body,
            height=theme.HEIGHT_BUTTON,
            corner_radius=theme.RADIUS_BUTTON,
            fg_color=theme.BG_HOVER,
            hover_color=theme.BORDER_STRONG,
            text_color=theme.TEXT,
            border_width=1,
            border_color=theme.BORDER,
            state="disabled",
            command=on_stop,
        )
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(theme.SPACE_SM, 0))

        # 复制签名后的播放列表地址：给「在别的设备上播」用。算出来的地址是
        # 自包含的，手机上的 HLS 播放器直接就能打开。
        self.copy_button = ctk.CTkButton(
            self,
            text="复制播放地址（含签名）",
            font=fonts.small,
            height=32,
            corner_radius=theme.RADIUS_BUTTON,
            fg_color=theme.BG_HOVER,
            hover_color=theme.BORDER_STRONG,
            text_color=theme.TEXT,
            border_width=1,
            border_color=theme.BORDER,
            command=on_copy,
        )
        self.copy_button.grid(
            row=7, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_SM, theme.SPACE_LG)
        )

    # -- 状态更新 ---------------------------------------------------------

    def reset(self) -> None:
        """回到「尚未播放」状态。"""
        self._apply(
            text="尚未播放。",
            color=theme.TEXT_DIM,
            play_state="normal",
            stop_state="disabled",
            controlled=False,
        )

    def show_inspecting(self) -> None:
        """正在获取并解析播放列表。"""
        self._apply(
            text="正在获取播放列表…",
            color=theme.ACCENT,
            play_state="disabled",
            stop_state="normal",
            controlled=False,
        )

    def show_playing(self, info: PlaybackInfo, *, player_name: str, controlled: bool) -> None:
        """播放器已启动。

        ``controlled`` 表示播放器会不会把位置报给我们。它只决定这行进度文字
        有没有内容，以及提示该往哪儿去操作播放——控制本身在播放器窗口里。
        """
        self._apply(
            text=f"正在播放（{player_name}）：{format_playback_summary(info)}",
            color=theme.SUCCESS,
            play_state="disabled",
            stop_state="normal",
            controlled=controlled,
        )

    def show_failed(self, message: str) -> None:
        """在线播放失败：只展示第一行，完整信息在日志里。"""
        first_line = message.splitlines()[0] if message else "在线播放失败。"
        self._apply(
            text=first_line,
            color=theme.ERROR,
            play_state="normal",
            stop_state="disabled",
            controlled=False,
        )

    def show_stopped(self) -> None:
        """播放已结束或被停止。"""
        self._apply(
            text="播放已结束。",
            color=theme.TEXT_DIM,
            play_state="normal",
            stop_state="disabled",
            controlled=False,
        )

    # -- 播放进度（只读） --------------------------------------------------

    def update_progress(self, position: float | None, duration: float | None) -> None:
        """按播放器推来的位置刷新这行只读进度文字。

        播放器没有控制通道时什么也不做——ffplay 压根报不出位置。
        """
        if not self._has_control_channel:
            return
        if duration is not None and duration > 0:
            self._duration = duration
        if position is None:
            return

        total = self._duration
        self._time_label.configure(
            text=f"已播放 {format_clock(position)} / {format_clock(total)}",
            text_color=theme.TEXT_SECONDARY,
        )

    # -- 倍速 -------------------------------------------------------------

    def speed(self) -> float:
        """当前选中的倍速。主窗口重新起播放时沿用它。"""
        return SPEED_CHOICES[self._speed_index]

    def _speed_button(self, master, text: str, step: int):
        return ctk.CTkButton(
            master,
            text=text,
            width=34,
            height=26,
            font=self._fonts.body_bold,
            corner_radius=theme.RADIUS_INPUT,
            fg_color=theme.BG_INPUT,
            hover_color=theme.BG_HOVER,
            text_color=theme.TEXT,
            border_width=1,
            border_color=theme.BORDER,
            state="disabled",
            command=lambda: self._step_speed(step),
        )

    def _step_speed(self, step: int) -> None:
        """按档位加减倍速；已经在两端就不动。"""
        if not self._has_control_channel:
            return
        index = min(max(0, self._speed_index + step), len(SPEED_CHOICES) - 1)
        if index != self._speed_index:
            self._speed_index = index
            self._update_speed_label()
            self._on_speed(SPEED_CHOICES[index])
        self._refresh_speed_buttons()

    def _update_speed_label(self) -> None:
        self._speed_label.configure(text=f"{SPEED_CHOICES[self._speed_index]:g}×")

    def _refresh_speed_buttons(self) -> None:
        """到顶/到底时把对应的按钮灰掉，免得点了没反应。"""
        usable = self._has_control_channel
        self._slower_button.configure(
            state="normal" if usable and self._speed_index > 0 else "disabled"
        )
        self._faster_button.configure(
            state="normal"
            if usable and self._speed_index < len(SPEED_CHOICES) - 1
            else "disabled"
        )

    # -- 内部实现 ---------------------------------------------------------

    def _apply(
        self,
        *,
        text: str,
        color: str,
        play_state: str,
        stop_state: str,
        controlled: bool,
    ) -> None:
        self._info_label.configure(text=text, text_color=color)
        self.play_button.configure(state=play_state)
        self.stop_button.configure(state=stop_state)
        self._has_control_channel = controlled
        self._duration = None
        self._time_label.configure(text="")
        self._hint_label.configure(
            text=CONTROL_HINT_MPV if controlled else CONTROL_HINT_FFPLAY,
            text_color=theme.TEXT_DIM if controlled else theme.WARNING,
        )
        self._refresh_speed_buttons()

