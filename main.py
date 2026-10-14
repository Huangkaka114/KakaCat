"""应用启动入口。

运行方式::

    python main.py

界面层位于 :mod:`gui`，下载核心位于 :mod:`hls_downloader`；
本文件只负责初始化主题并启动主窗口。
"""

from __future__ import annotations

import sys
import traceback


def main() -> int:
    """初始化主题并启动主窗口。"""
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
