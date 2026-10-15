"""应用启动入口。

运行方式::

    python main.py
    python main.py --check     # 只报告运行环境检测结果，不开界面

界面层位于 :mod:`gui`，下载核心位于 :mod:`hls_downloader`；
本文件只负责初始化主题并启动主窗口。
"""

from __future__ import annotations

import sys
import traceback


def report_environment() -> int:
    """报告运行环境检测结果，不启动界面。

    给两种场景用：打包之后的自检（确认 exe 真的能找到随包分发的工具），
    以及换一台机器时排障（"为什么说没检测到 ffmpeg"）。

    刻意不导入任何界面模块，因此没有图形环境也能跑。
    """
    from hls_downloader import __version__, merger, player
    from hls_downloader.app_paths import (
        application_dir,
        find_bundled_tool,
        runtime_data_dir,
        user_data_dir,
    )

    frozen = bool(getattr(sys, "frozen", False))
    lines = [
        f"KakaCat {__version__}",
        f"运行方式    : {'打包后的程序' if frozen else '源码运行'}",
        f"程序目录    : {application_dir()}",
        f"运行数据目录: {runtime_data_dir()}",
        f"用户数据目录: {user_data_dir()}",
        "",
        "外部工具检测：",
    ]
    for label, finder in (
        ("mpv", player.find_mpv),
        ("ffplay", player.find_ffplay),
        ("ffmpeg", merger.find_ffmpeg),
    ):
        found = finder()
        origin = ""
        if found:
            bundled = find_bundled_tool(label)
            origin = "（随包分发）" if bundled else "（来自 PATH 或常见安装位置）"
        lines.append(f"  {label:<7}: {found or '未检测到'}{origin}")

    lines += [
        "",
        "说明：",
        "  在线播放需要 mpv（有进度条与倍速）或 ffplay（只能基本播放）；",
        "  输出 .mp4 需要 ffmpeg，没有它时程序会自动改用 .ts。",
    ]
    report = "\n".join(lines)
    print(report)

    written = _write_report(report)
    if written is not None:
        print(f"\n（本报告也写入了一份：{written}）")
    return 0


def _write_report(report: str) -> "Path | None":
    """把报告写一份到运行数据目录。

    打包成窗口程序之后没有控制台，``print`` 出去没人看得见，所以必须留一份
    文件——否则这个自检开关在 exe 上等于没用。
    """
    from hls_downloader.app_paths import runtime_data_dir

    try:
        target = runtime_data_dir() / "diagnostics.txt"
        target.write_text(report + "\n", encoding="utf-8")
        return target
    except OSError as exc:  # pragma: no cover - 目录不可写时只能算了
        print(f"（写不了检测报告：{exc}）", file=sys.stderr)
        return None


def main() -> int:
    """初始化主题并启动主窗口。"""
    if "--check" in sys.argv[1:]:
        return report_environment()

    try:
        import customtkinter as ctk
    except ImportError:
        print(
            "缺少依赖 customtkinter，请先执行：pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    from gui.main_window import MainWindow

    window = MainWindow()
    window.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - 启动阶段的兜底提示
        traceback.print_exc()
        try:
            from tkinter import messagebox

            messagebox.showerror("启动失败", "程序启动时发生错误，详情见控制台输出。")
        except Exception:
            pass
        sys.exit(1)
