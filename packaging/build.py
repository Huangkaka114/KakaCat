"""一条命令打出可分发的程序目录（以及可选的安装程序）。

用法::

    python packaging/build.py                  # 只打程序目录 → dist/KakaCat/
    python packaging/build.py --bundle-tools   # 先把 ffmpeg/mpv 拷进 bin/ 再打
    python packaging/build.py --installer      # 再调 Inno Setup 生成安装程序

为什么要有这个脚本而不是直接敲 pyinstaller：版本号只有一个来源
(``hls_downloader.__version__``)，Windows 的版本资源文件要按它生成；随包工具、
图标、工作目录这些也得统一，否则很容易打出一个"能跑但信息是错的"的 exe。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_DIR = PROJECT_ROOT / "packaging"
SPEC_FILE = PACKAGING_DIR / "KakaCat.spec"
GENERATED_DIR = PROJECT_ROOT / "build" / "generated"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build" / "pyinstaller"
TOOLS_DIR = PROJECT_ROOT / "bin"
APP_NAME = "KakaCat"

#: 可以随包分发的第三方工具：名字 → 在 PATH / 常见位置里怎么找
BUNDLEABLE_TOOLS = ("ffmpeg", "ffplay", "mpv")

#: 软件名，写进 exe 的版本资源与安装程序
APP_DISPLAY_NAME = "KakaCat"


def _fail(message: str) -> int:
    print(f"错误：{message}", file=sys.stderr)
    return 1


def read_version() -> str:
    """从源码里读版本号——唯一的版本来源。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    from hls_downloader import __version__

    return __version__


def write_version_info(version: str) -> Path:
    """生成 PyInstaller 用的 Windows 版本资源文件。"""
    parts = []
    for chunk in (version.split(".") + ["0", "0", "0"])[:4]:
        parts.append(int("".join(ch for ch in chunk if ch.isdigit()) or 0))
    tuple_text = ", ".join(str(p) for p in parts)

    content = f"""# 由 packaging/build.py 生成，不要手工编辑
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({tuple_text}),
    prodvers=({tuple_text}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [StringStruct('CompanyName', 'KakaCat'),
        StringStruct('FileDescription', '{APP_DISPLAY_NAME}'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{APP_DISPLAY_NAME}'),
        StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [0x0804, 1200])])
  ]
)
"""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = GENERATED_DIR / "version_info.txt"
    path.write_text(content, encoding="utf-8")
    return path


def bundle_tools() -> int:
    """把能找到的 ffmpeg / ffplay / mpv 拷进 ``bin/``，随包分发。

    这一步是**可选**的：不拷也能打包，只是目标机器上得自己装。拷进来的二进制
    不会入库（``bin/`` 已忽略），分发时请自行确认许可。
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from hls_downloader import merger, player

    finders = {
        "ffmpeg": merger.find_ffmpeg,
        "ffplay": player.find_ffplay,
        "mpv": player.find_mpv,
    }
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    missing = []
    for tool in BUNDLEABLE_TOOLS:
        found = finders[tool]()
        if not found:
            missing.append(tool)
            continue
        target = TOOLS_DIR / Path(found).name
        if target.exists():
            print(f"  已有 {target.name}")
            continue
        shutil.copy2(found, target)
        size = target.stat().st_size / 1024 / 1024
        print(f"  拷入 {target.name}（{size:.0f} MB）")

    if missing:
        print(f"  没找到：{', '.join(missing)}（不随包分发，运行时靠 PATH）")
    return 0


def verify_bundle() -> int:
    """跑一次打包后的 exe 自检，确认它真能找到随包分发的工具。

    窗口程序没有控制台，``--check`` 的输出看不到，所以它会另外写一份报告到
    运行数据目录——这里就是去那里读回来。
    """
    exe = DIST_DIR / APP_NAME / f"{APP_NAME}.exe"
    print(f"\n自检：运行 {exe.name} --check")
    try:
        subprocess.run([str(exe), "--check"], timeout=180)
    except subprocess.TimeoutExpired:  # pragma: no cover - 异常机器上才会发生
        return _fail("打包后的程序 --check 超时，可能启动就卡住了")

    candidates = [DIST_DIR / APP_NAME / "data" / "diagnostics.txt"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / APP_NAME / "diagnostics.txt")

    for candidate in candidates:
        if candidate.is_file():
            print(f"--- {candidate} ---")
            print(candidate.read_text(encoding="utf-8").rstrip())
            return 0

    return _fail("没找到检测报告，打包后的程序可能根本没起来")


def run_pyinstaller() -> int:
    if not SPEC_FILE.is_file():
        return _fail(f"找不到打包配置：{SPEC_FILE}")

    argv = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        str(SPEC_FILE),
    ]
    print("运行：", " ".join(argv[1:]))
    result = subprocess.run(argv, cwd=str(PROJECT_ROOT))
    return result.returncode


def run_inno_setup(version: str) -> int:
    """调 Inno Setup 编译安装程序（需要事先装好 Inno Setup）。"""
    iscc = shutil.which("iscc")
    if not iscc:
        # winget 装的是**按用户**安装，落在 %LOCALAPPDATA%\Programs 下，
        # 不在 Program Files 里，也不进 PATH
        candidates = []
        local = os.environ.get("LOCALAPPDATA")
        # 注意别把循环变量叫 version：那会盖掉本函数的 version 参数
        for folder in ("Inno Setup 6", "Inno Setup 7"):
            if local:
                candidates.append(Path(local) / "Programs" / folder / "ISCC.exe")
            candidates.append(Path(rf"C:\Program Files (x86)\{folder}\ISCC.exe"))
            candidates.append(Path(rf"C:\Program Files\{folder}\ISCC.exe"))
        for candidate in candidates:
            if candidate.is_file():
                iscc = str(candidate)
                break
    if not iscc:
        return _fail(
            "找不到 Inno Setup 的编译器 iscc。装一个再试：winget install JRSoftware.InnoSetup"
        )

    script = PACKAGING_DIR / "KakaCat.iss"
    print(f"运行：{iscc} /DMyAppVersion={version} {script.name}")
    result = subprocess.run(
        [iscc, f"/DMyAppVersion={version}", str(script)], cwd=str(PROJECT_ROOT)
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 KakaCat")
    parser.add_argument(
        "--bundle-tools",
        action="store_true",
        help="先把 ffmpeg/ffplay/mpv 拷进 bin/ 再打包（随包分发，体积会大很多）",
    )
    parser.add_argument("--installer", action="store_true", help="额外生成安装程序")
    parser.add_argument("--skip-build", action="store_true", help="跳过 exe，只做安装程序")
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        return _fail(
            "没装 PyInstaller。先执行：pip install -r requirements-build.txt"
        )

    version = read_version()
    print(f"版本：{version}")

    if args.bundle_tools:
        print("拷贝随包工具到 bin/：")
        bundle_tools()

    version_file = write_version_info(version)
    print(f"版本资源：{version_file.relative_to(PROJECT_ROOT)}")

    if not args.skip_build:
        code = run_pyinstaller()
        if code != 0:
            return _fail(f"PyInstaller 失败（退出码 {code}）")

        exe = DIST_DIR / APP_NAME / f"{APP_NAME}.exe"
        if not exe.is_file():
            return _fail(f"打包结束但没找到 {exe}")
        total = sum(f.stat().st_size for f in (DIST_DIR / APP_NAME).rglob("*") if f.is_file())
        print(f"\n完成：{exe}")
        print(f"目录大小：{total / 1024 / 1024:.0f} MB")
        # PyInstaller 6 把 onedir 的东西都收进 _internal/，随包工具在那里面的 bin/
        bundled = sorted(
            p.name
            for p in (DIST_DIR / APP_NAME).rglob("*.exe")
            if p.parent.name == "bin"
        )
        print(f"随包工具：{', '.join(bundled) if bundled else '（无）'}")

        if verify_bundle() != 0:
            return 1

    if args.installer:
        code = run_inno_setup(version)
        if code != 0:
            return _fail(f"Inno Setup 失败（退出码 {code}）")
        print(f"\n安装程序在：{DIST_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
