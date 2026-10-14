"""统一管理颜色、字体、间距、圆角等视觉常量。

所有界面模块都从这里取值，避免出现散落各处的硬编码颜色，
也方便日后整体调整风格。

整体风格为**蓝黑科技风**：近黑深蓝的层级背景、冷蓝细边框、
电光蓝强调色。所有文字配色均满足 WCAG AA（详见各常量注释）。
"""

from __future__ import annotations

from dataclasses import dataclass

import customtkinter as ctk

# --------------------------------------------------------------------------
# 颜色 —— 蓝黑科技风：近黑深蓝底 + 电光蓝强调
#
# 强调色分两层，用途不同：
#   PRIMARY  深电光蓝。用于按钮、徽标这类**承载白字的填充面**，因此必须
#            满足 WCAG AA（白字 4.63:1）；不要调亮它，调亮就不达标了。
#   ACCENT   更亮的青蓝。用于进度条、章节装饰条、运行中状态这类"发光"
#            元素，不与白字同框，可以更亮更扎眼。
# --------------------------------------------------------------------------

# 背景：近黑深蓝，按层级逐层加深
BG_APP = "#070B13"
BG_PANEL = "#0D1420"
BG_INPUT = "#121C2B"
BG_INPUT_FOCUS = "#172435"
BG_LOG = "#04070D"
BG_HOVER = "#1A2839"

# 边框：冷蓝细线
BORDER = "#1B2A3E"
BORDER_STRONG = "#2C4568"

# 文字
TEXT = "#E6F0FF"
TEXT_SECONDARY = "#93A9C7"
TEXT_DIM = "#64789A"

# 强调色
PRIMARY = "#1F6FEB"
PRIMARY_HOVER = "#3B82F6"
PRIMARY_TEXT = "#FFFFFF"
ACCENT = "#4CC2FF"

# 语义色
SUCCESS = "#2FD69A"
WARNING = "#FFC53D"
ERROR = "#FF6B6B"
NEUTRAL = "#5A6E88"

# 进度条轨道
PROGRESS_TRACK = "#16202E"

# --------------------------------------------------------------------------
# 尺寸
# --------------------------------------------------------------------------

RADIUS_CARD = 12
RADIUS_INPUT = 8
RADIUS_BUTTON = 8

HEIGHT_INPUT = 34
HEIGHT_BUTTON = 38

SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24

#: 窗口宽度小于该值时，中部区域改为单栏布局
NARROW_WIDTH_THRESHOLD = 1000

# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------

#: 优先使用的中文字体，按顺序取第一个系统中存在的
_PREFERRED_FAMILIES = (
    "Microsoft YaHei UI",
    "微软雅黑",
    "Microsoft YaHei",
    "Segoe UI",
    "Noto Sans CJK SC",
    "Arial",
)

_FALLBACK_FAMILY = "Segoe UI"


def _resolve_family() -> str:
    """在系统中挑选一个可用且能正常显示中文的字体。"""
    try:
        import tkinter.font as tkfont

        available = {name.lower() for name in tkfont.families()}
    except Exception:  # pragma: no cover - 只在极端环境下触发
        return _FALLBACK_FAMILY

    for family in _PREFERRED_FAMILIES:
        if family.lower() in available:
            return family
    return _FALLBACK_FAMILY


@dataclass(frozen=True)
class Fonts:
    """界面使用的字体集合。"""

    family: str
    title: ctk.CTkFont
    subtitle: ctk.CTkFont
    section: ctk.CTkFont
    body: ctk.CTkFont
    body_bold: ctk.CTkFont
    small: ctk.CTkFont
    hint: ctk.CTkFont
    metric: ctk.CTkFont
    log: ctk.CTkFont


def create_fonts() -> Fonts:
    """创建字体集合。

    必须在 Tk 根窗口创建之后调用（``CTkFont`` 依赖根窗口）。
    """
    family = _resolve_family()

    def font(size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family=family, size=size, weight=weight)

    return Fonts(
        family=family,
        title=font(20, "bold"),
        subtitle=font(12),
        section=font(14, "bold"),
        body=font(13),
        body_bold=font(13, "bold"),
        small=font(12),
        hint=font(11),
        metric=font(15, "bold"),
        log=font(12),
    )
