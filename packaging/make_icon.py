"""生成应用图标（``packaging/resources/KakaCat.ico``）。

图标本身是**提交进仓库**的（很小），这个脚本只是让它可重新生成、可微调：
设计语言跟着界面的蓝黑科技风走——近黑深蓝底 + 电光蓝描边 + 青蓝播放三角。

用法::

    python packaging/make_icon.py

只想换个配色的话改下面的常量就行，不必动别的。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

#: 与 gui/theme.py 保持一致
BG_PANEL = (13, 20, 32, 255)  # #0D1420
BORDER = (44, 69, 104, 255)  # #2C4568
PRIMARY = (31, 111, 235, 255)  # #1F6FEB
ACCENT = (76, 194, 255, 255)  # #4CC2FF
TEXT = (230, 240, 255, 255)  # #E6F0FF

#: Windows 图标常用的几个尺寸；小尺寸单独画，否则缩下去会糊成一团
SIZES = (16, 24, 32, 48, 64, 128, 256)

OUTPUT = Path(__file__).resolve().parent / "resources" / "KakaCat.ico"

#: 画布按这个尺寸设计，其它尺寸按比例缩
BASE = 256


def _draw(size: int) -> Image.Image:
    """画一枚图标。``size`` 是边长（正方形）。"""
    scale = size / BASE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = max(1, round(6 * scale))
    radius = max(2, round(52 * scale))
    draw.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=radius,
        fill=BG_PANEL,
        outline=BORDER,
        width=max(1, round(4 * scale)),
    )

    # 播放三角：往右推一点，视觉上才居中（三角形的重心偏左）
    left = size * 0.34
    right = size * 0.72
    top = size * 0.28
    bottom = size * 0.72
    draw.polygon(
        [(left, top), (right, (top + bottom) / 2), (left, bottom)],
        fill=ACCENT,
    )

    if size >= 32:
        # 大尺寸才加这条下载箭头，小尺寸加上只会变成一坨
        shaft_top = size * 0.60
        shaft_bottom = size * 0.80
        cx = size * 0.53
        width = max(2, round(14 * scale))
        draw.line([(cx, shaft_top), (cx, shaft_bottom)], fill=PRIMARY, width=width)
        head = size * 0.06
        draw.polygon(
            [
                (cx - head, shaft_bottom - head),
                (cx + head, shaft_bottom - head),
                (cx, shaft_bottom + head * 0.6),
            ],
            fill=PRIMARY,
        )

    return image


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [_draw(size) for size in SIZES]
    # 最小的一帧当主图，Windows 会自己挑合适的尺寸
    frames[-1].save(OUTPUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"已生成 {OUTPUT}（{len(SIZES)} 个尺寸，{OUTPUT.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
