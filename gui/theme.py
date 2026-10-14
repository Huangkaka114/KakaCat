"""统一管理颜色、字体、间距、圆角等视觉常量。

所有界面模块都从这里取值，避免出现散落各处的硬编码颜色，
也方便日后整体调整风格。
"""

from __future__ import annotations

from dataclasses import dataclass

import customtkinter as ctk

# --------------------------------------------------------------------------
# 颜色 —— 深蓝灰背景 + 蓝色强调色
# --------------------------------------------------------------------------

BG_APP = "#1A212C"
BG_PANEL = "#212A38"
BG_INPUT = "#26313F"
BG_INPUT_FOCUS = "#2B3849"
BG_LOG = "#151B24"
BG_HOVER = "#303D50"

BORDER = "#33415A"
BORDER_STRONG = "#3F4F6B"

TEXT = "#E8EDF5"
TEXT_SECONDARY = "#A7B4C8"
TEXT_DIM = "#71829A"

PRIMARY = "#3B82F6"
PRIMARY_HOVER = "#2F6FE0"
PRIMARY_TEXT = "#FFFFFF"

SUCCESS = "#34D399"
WARNING = "#FBBF24"
ERROR = "#F87171"
NEUTRAL = "#64748B"

PROGRESS_TRACK = "#2C3849"

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
