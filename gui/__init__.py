"""CustomTkinter 图形界面层。

界面只负责收集输入、启动后台任务和展示状态；所有下载逻辑都在
:mod:`hls_downloader` 包中，界面不直接执行任何网络或文件合并操作。
"""

from .main_window import MainWindow

__all__ = ["MainWindow"]
