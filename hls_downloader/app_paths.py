"""程序自己的目录：安装目录在哪、运行数据放哪、随包分发的工具在哪。

打包成客户端之后，这几件事都必须有确定答案，而且**不能往安装目录之外乱写**：

* **随包分发的工具**（``ffmpeg.exe`` / ``mpv.exe`` / ``ffplay.exe``）放在程序
  目录下的 ``bin\\`` 里，查找时优先看它们——这样打包后不要求用户自己去配
  PATH。放在 ``sys._MEIPASS`` 里也能找到（PyInstaller 的 ``--add-binary``
  就是放到那儿）。
* **运行数据**（临时文件、将来的日志与配置）便携优先：先看程序目录下的
  ``data\\`` 能不能写，不能写（例如装在 ``C:\\Program Files``）再退回
  用户目录 ``%LOCALAPPDATA%\\KakaCat``。

注意：**下载产物与分片临时目录不在这里**——它们跟着用户选择的输出目录走，
不会跑进安装目录，也不会占系统盘。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 程序名，用于用户目录下的子目录名
APP_NAME = "KakaCat"

#: 便携模式下的运行数据目录名（相对程序目录）
DATA_DIR_NAME = "data"

#: 随包分发的工具默认放在这个子目录里
TOOLS_DIR_NAME = "bin"

#: 查找随包工具时依次尝试的子目录（相对程序目录）
_TOOL_SUBDIRS = (TOOLS_DIR_NAME, "", "tools", "ffmpeg", "mpv")

#: Windows 下可执行文件的扩展名
_EXECUTABLE_SUFFIX = ".exe" if os.name == "nt" else ""

_runtime_data_dir: Path | None = None


def application_dir() -> Path:
    """程序所在目录。

    打包后是 **exe 所在目录**（而不是 PyInstaller 的临时解包目录），这样
    ``bin\\`` 这种"随程序一起放"的东西才能被找到。开发时就是仓库根目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _search_roots() -> tuple[Path, ...]:
    """找随包工具时该翻哪些根目录。"""
    roots = [application_dir()]
    # PyInstaller 的 --add-binary 把文件解到 _MEIPASS，onefile 模式下
    # 它和 exe 所在目录不是同一个地方。
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.insert(0, Path(bundle))
    return tuple(roots)


def executable_name(name: str) -> str:
    """给工具名补上平台对应的扩展名（Windows 上是 ``.exe``）。

    ``shutil.which`` 会自己按 PATHEXT 试扩展名，但手工拼路径不会——漏了这一步
    就会去找 ``MPV Player\\mpv`` 而不是 ``mpv.exe``，然后"明明装了却找不到"。
    """
    return f"{name}{_EXECUTABLE_SUFFIX}"


def find_bundled_tool(name: str) -> Path | None:
    """找随程序分发的工具（不带扩展名的 ``name``，如 ``"ffmpeg"``）。

    找到返回路径，否则返回 ``None``。只在程序自己的目录里找，不碰 PATH。
    """
    filename = executable_name(name)
    for root in _search_roots():
        for subdir in _TOOL_SUBDIRS:
            candidate = (root / subdir / filename) if subdir else (root / filename)
            if candidate.is_file():
                return candidate
    return None


def user_data_dir() -> Path:
    """用户目录下的程序数据目录（``%LOCALAPPDATA%\\KakaCat``）。

    程序目录不可写时的退路。Windows 之外的平台退回 ``~/.local/share/KakaCat``。
    """
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_NAME


def _is_writable(directory: Path) -> bool:
    """这个目录能不能写。会顺手把它建出来。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def runtime_data_dir() -> Path:
    """放程序自己的运行数据：便携优先，程序目录不可写时退回用户目录。

    结果会缓存——同一个进程里不必反复试写。
    """
    global _runtime_data_dir
    if _runtime_data_dir is not None:
        return _runtime_data_dir

    portable = application_dir() / DATA_DIR_NAME
    if _is_writable(portable):
        _runtime_data_dir = portable
        return portable

    fallback = user_data_dir()
    fallback.mkdir(parents=True, exist_ok=True)
    _runtime_data_dir = fallback
    return fallback


def reset_cache() -> None:
    """清掉 :func:`runtime_data_dir` 的缓存（测试用）。"""
    global _runtime_data_dir
    _runtime_data_dir = None
