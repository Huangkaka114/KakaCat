# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

**用 ``python packaging/build.py`` 调用，不要直接跑 pyinstaller**：版本号、
随包工具、图标这些东西由构建脚本统一处理，直接跑会漏掉。

几个关键决定：

* **onedir 而不是 onefile**。程序要能在自己目录下找到随包分发的 ``bin/``
  工具（见 ``hls_downloader/app_paths.py``），onedir 的结构天然就是
  ``KakaCat/KakaCat.exe`` + ``KakaCat/bin/*.exe``；onefile 每次启动都要先解包
  到临时目录，又慢又容易让人误以为程序在往系统盘写东西。
* **``collect_all("customtkinter")`` 不能省**。customtkinter 读取主题与字体的
  JSON 时用的是自己包目录里的相对路径，不打进包里的话，exe 一启动就
  ``FileNotFoundError: ... theme ... json``，界面根本出不来。
* ``bin/`` 里有什么就带上什么（ffmpeg / ffplay / mpv），没有也能打——只是
  运行时得靠 PATH 或用户自己装。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# SPECPATH 由 PyInstaller 注入，指向本文件所在目录
PROJECT_ROOT = Path(SPECPATH).parent
PACKAGING_DIR = Path(SPECPATH)
GENERATED_DIR = PROJECT_ROOT / "build" / "generated"

APP_NAME = "KakaCat"
ENTRY_SCRIPT = PROJECT_ROOT / "main.py"

# customtkinter 的数据文件与子模块
ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all("customtkinter")

datas = list(ctk_datas)
binaries = list(ctk_binaries)
hiddenimports = list(ctk_hiddenimports)

# 随包分发的第三方工具：放进程序目录的 bin/ 就会被带进包里
tools_dir = PROJECT_ROOT / "bin"
for tool in ("ffmpeg.exe", "ffplay.exe", "mpv.exe"):
    candidate = tools_dir / tool
    if candidate.is_file():
        binaries.append((str(candidate), "bin"))

# 图标：优先用仓库里的，没有就退回 PyInstaller 默认
icon_path = PACKAGING_DIR / "resources" / "KakaCat.ico"
icon = str(icon_path) if icon_path.is_file() else None

# Windows 文件属性里的版本信息，由 build.py 从 hls_downloader.__version__ 生成
version_file = GENERATED_DIR / "version_info.txt"

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 用不到的重型依赖剔掉，别把体积撑起来
    excludes=[
        "tkinter.test",
        "test",
        "unittest",
        "pydoc_data",
        "PyInstaller",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 压过的 exe 更容易被杀软误报
    console=False,  # 桌面程序，不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=str(version_file) if version_file.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
