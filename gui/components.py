"""可复用的界面组件：卡片、带标签的输入框、状态面板、日志面板。

这些组件只关心展示与基本交互，不包含任何下载逻辑。
"""

from __future__ import annotations

import time
from typing import Callable

import customtkinter as ctk

from hls_downloader.models import DownloadResult, ProgressReport, TaskStage

from . import theme
from .theme import Fonts

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
    """状态面板中的一个指标（标题 + 数值）。"""

    def __init__(self, master, fonts: Fonts, caption: str, **kwargs):
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        ctk.CTkLabel(
            self, text=caption, font=fonts.hint, text_color=theme.TEXT_DIM, anchor="w"
        ).grid(row=0, column=0, sticky="w")
        self.value = ctk.CTkLabel(
            self, text="—", font=fonts.metric, text_color=theme.TEXT, anchor="w"
        )
        self.value.grid(row=1, column=0, sticky="w", pady=(2, 0))

    def set(self, text: str, color: str = theme.TEXT) -> None:
        self.value.configure(text=text, text_color=color)


class StatusPanel(Card):
    """显示任务阶段、进度、分片数量、速度和预计剩余时间。"""

    def __init__(self, master, fonts: Fonts, **kwargs):
        super().__init__(master, **kwargs)
        self._fonts = fonts
        self.columnconfigure(0, weight=1)

        SectionTitle(self, "下载状态", fonts).grid(
            row=0, column=0, sticky="w", padx=theme.SPACE_LG, pady=(theme.SPACE_MD, theme.SPACE_SM)
        )

        self._stage_label = ctk.CTkLabel(
            self, text=TaskStage.IDLE.label, font=fonts.body_bold, text_color=theme.TEXT, anchor="w"
        )
        self._stage_label.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_LG)

        self._progress_bar = ctk.CTkProgressBar(
            self,
            height=10,
            corner_radius=5,
            fg_color=theme.PROGRESS_TRACK,
            progress_color=theme.ACCENT,
        )
        self._progress_bar.set(0.0)
        self._progress_bar.grid(
            row=2, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_SM, theme.SPACE_XS)
        )

        self._message_label = ctk.CTkLabel(
            self, text="等待开始", font=fonts.hint, text_color=theme.TEXT_DIM, anchor="w"
        )
        self._message_label.grid(row=3, column=0, sticky="ew", padx=theme.SPACE_LG)

        metrics = ctk.CTkFrame(self, fg_color="transparent")
        metrics.grid(
            row=4, column=0, sticky="ew", padx=theme.SPACE_LG, pady=(theme.SPACE_MD, theme.SPACE_LG)
        )
        for column in range(4):
            metrics.columnconfigure(column, weight=1)

        self._percent = _Metric(metrics, fonts, "进度")
        self._percent.grid(row=0, column=0, sticky="w")
        self._segments = _Metric(metrics, fonts, "分片")
        self._segments.grid(row=0, column=1, sticky="w")
        self._speed = _Metric(metrics, fonts, "速度")
        self._speed.grid(row=0, column=2, sticky="w")
        self._eta = _Metric(metrics, fonts, "剩余时间")
        self._eta.grid(row=0, column=3, sticky="w")

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
        self._stage_label.configure(text=TaskStage.FAILED.label, text_color=theme.ERROR)
        self._message_label.configure(text=message.splitlines()[0] if message else "下载失败")
        self._progress_bar.configure(progress_color=theme.ERROR)

    def show_cancelled(self) -> None:
        self._stage_label.configure(text=TaskStage.CANCELLED.label, text_color=theme.WARNING)
        self._message_label.configure(text="任务已取消，临时文件已清理。")
        self._progress_bar.configure(progress_color=theme.WARNING)

    def show_stage(self, stage: TaskStage) -> None:
        self._stage_label.configure(text=stage.label, text_color=theme.WARNING)

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
        self._stage_label.configure(text=stage.label, text_color=color)
        self._message_label.configure(text=message)
        self._progress_bar.configure(progress_color=color)
        self._progress_bar.set(max(0.0, min(1.0, percent)))
        self._percent.set(percent_text or f"{percent * 100:.1f}%", color)
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
