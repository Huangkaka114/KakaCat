"""分片合并与视频封装。

策略：

* MPEG-TS 分片合并为 ``.ts``：直接二进制拼接，不需要 FFmpeg；
* MPEG-TS 分片但目标扩展名是 ``.mp4`` / ``.mov``：需要 FFmpeg 重新封装
  （``-c copy``，不重新编码）；
* fMP4 / CMAF 分片：初始化分片加媒体分片依次二进制拼接即为合法文件。

任何情况下都不会因为“文件已生成”就报告成功：输出必须存在且大小符合
预期，否则抛出 :class:`MergeError`。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from .models import MergeError, MergeResult, SegmentKind

COPY_CHUNK_SIZE = 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = 60 * 60

#: 需要 FFmpeg 重新封装的目标容器（TS 分片 → 这些容器时）
_REMUX_SUFFIXES = frozenset({".mp4", ".m4v", ".mov", ".mkv"})

#: 目标扩展名 → FFmpeg 输出格式名。必须显式指定，因为实际写入的可能是
#: 同目录下的 ``.part`` 临时文件，FFmpeg 无法从扩展名推断容器格式。
_FFMPEG_MUXERS = {
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".mov": "mov",
    ".mkv": "matroska",
}


def find_ffmpeg() -> str | None:
    """在 PATH 中查找 FFmpeg，找到返回可执行文件路径，否则返回 ``None``。"""
    return shutil.which("ffmpeg")


def merge_segments(
    segment_paths: Sequence[str | os.PathLike[str]],
    output_path: str | os.PathLike[str],
    segment_kind: SegmentKind,
    *,
    container_suffix: str | None = None,
    ffmpeg_path: str | None = None,
    logger: logging.Logger | None = None,
) -> MergeResult:
    """把已下载的分片按顺序合并成最终视频文件。

    Args:
        segment_paths: 按播放顺序排列的本地分片文件（fMP4 时第一项应是初始化分片）。
        output_path: 最终输出文件路径（可能是同目录下的 ``.part`` 临时文件）。
        segment_kind: 分片容器类型，决定合并策略。
        container_suffix: 目标容器扩展名（如 ``.mp4``）。仅当 ``output_path``
            是临时文件、扩展名不代表最终格式时才需要显式指定。
        ffmpeg_path: FFmpeg 可执行文件路径；为 ``None`` 时自动在 PATH 中查找。
        logger: 可选的日志记录器。

    Returns:
        :class:`MergeResult`。

    Raises:
        MergeError: 分片缺失/为空、FFmpeg 不可用或执行失败、输出文件校验不通过。
    """
    log = logger or logging.getLogger("hls_downloader")
    paths = [Path(p) for p in segment_paths]
    target = Path(output_path)

    if not paths:
        raise MergeError("没有可供合并的分片。")

    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise MergeError(f"分片文件缺失，无法合并：{missing[0]}")

    empty = [str(p) for p in paths if p.stat().st_size == 0]
    if empty:
        raise MergeError(f"分片文件为空，无法合并：{empty[0]}")

    suffix = (container_suffix or target.suffix).lower()
    log.info("合并策略：分片类型=%s，目标扩展名=%s", segment_kind.label, suffix or "(无)")

    if segment_kind is SegmentKind.TS and suffix in _REMUX_SUFFIXES:
        return _remux_with_ffmpeg(paths, target, _FFMPEG_MUXERS.get(suffix), ffmpeg_path, log)

    return _concat_binary(paths, target, log)


def _concat_binary(
    paths: Sequence[Path],
    target: Path,
    log: logging.Logger,
) -> MergeResult:
    """按顺序二进制拼接分片。"""
    expected = sum(p.stat().st_size for p in paths)
    written = 0

    try:
        with open(target, "wb") as output:
            for path in paths:
                with open(path, "rb") as source:
                    while True:
                        chunk = source.read(COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
                        written += len(chunk)
    except OSError as exc:
        _remove_quietly(target)
        raise MergeError(f"写入输出文件失败：{exc}") from exc

    if written != expected:
        _remove_quietly(target)
        raise MergeError(
            f"合并结果不完整：预期写入 {expected} 字节，实际写入 {written} 字节。"
        )

    log.info("二进制拼接完成，共 %s 字节。", written)
    return MergeResult(output_path=str(target), total_bytes=written, strategy="concat")


def _remux_with_ffmpeg(
    paths: Sequence[Path],
    target: Path,
    muxer: str | None,
    ffmpeg_path: str | None,
    log: logging.Logger,
) -> MergeResult:
    """调用 FFmpeg 把 TS 分片重新封装为目标容器（不重新编码）。"""
    executable = ffmpeg_path or find_ffmpeg()
    if not executable:
        raise MergeError(
            "把 TS 分片封装为 MP4 需要 FFmpeg，但当前系统中没有找到 ffmpeg 可执行文件。\n"
            "请安装 FFmpeg 并加入 PATH，或把输出文件名改为 .ts 后缀（无需 FFmpeg）。"
        )

    list_file = target.with_name(target.name + ".concat.txt")
    try:
        list_file.write_text(_build_concat_list(paths), encoding="utf-8")
    except OSError as exc:
        raise MergeError(f"无法创建 FFmpeg 合并列表文件：{exc}") from exc

    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
    ]
    if muxer:
        # 输出路径可能是 .part 临时文件，必须显式指定容器格式。
        command.extend(["-f", muxer])
    command.append(str(target))

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        _remove_quietly(target)
        raise MergeError("FFmpeg 合并超时，任务已中止。") from exc
    except OSError as exc:
        _remove_quietly(target)
        raise MergeError(f"无法执行 FFmpeg：{exc}") from exc
    finally:
        _remove_quietly(list_file)

    if completed.returncode != 0:
        _remove_quietly(target)
        detail = (completed.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {completed.returncode}"
        raise MergeError(f"FFmpeg 合并失败：{tail}")

    if not target.is_file() or target.stat().st_size == 0:
        _remove_quietly(target)
        raise MergeError("FFmpeg 执行结束但没有生成有效的输出文件。")

    size = target.stat().st_size
    log.info("FFmpeg 重新封装完成，共 %s 字节。", size)
    return MergeResult(output_path=str(target), total_bytes=size, strategy="ffmpeg-remux")


def _build_concat_list(paths: Sequence[Path]) -> str:
    """生成 FFmpeg concat 分离器所需的列表文件内容。"""
    lines = []
    for path in paths:
        escaped = str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 清理失败不应掩盖原始错误
        pass
