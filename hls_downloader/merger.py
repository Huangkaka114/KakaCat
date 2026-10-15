"""分片合并与视频封装。

策略：

* MPEG-TS 分片合并为 ``.ts``：直接二进制拼接，不需要 FFmpeg；
* MPEG-TS 分片但目标扩展名是 ``.mp4`` / ``.mov``：需要 FFmpeg 重新封装
  （``-c copy``，不重新编码）；
* fMP4 / CMAF 分片：初始化分片加媒体分片依次二进制拼接即为合法文件。

任何情况下都不会因为“文件已生成”就报告成功：输出必须存在且大小符合
预期，否则抛出 :class:`MergeError`。

合并阶段同样支持取消：传入 ``cancel_event`` 后，二进制拼接会在每个数据块
之间检查取消信号，FFmpeg 子进程则通过轮询实现可中止。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Sequence

from .app_paths import find_bundled_tool
from .models import DownloadCancelled, MergeError, MergeResult, SegmentKind

COPY_CHUNK_SIZE = 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = 60 * 60
#: 轮询 FFmpeg 子进程的间隔，决定取消的最坏响应延迟
FFMPEG_POLL_SECONDS = 0.2

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
    """查找 FFmpeg：先看随程序分发的，再查 PATH。

    打包成客户端时会把 ``ffmpeg.exe`` 放在程序目录的 ``bin\\`` 里，所以必须
    优先看它——否则用户明明"随包装好了"，程序还是报未检测到。
    """
    bundled = find_bundled_tool("ffmpeg")
    if bundled is not None:
        return str(bundled)
    return shutil.which("ffmpeg")


def merge_segments(
    segment_paths: Sequence[str | os.PathLike[str]],
    output_path: str | os.PathLike[str],
    segment_kind: SegmentKind,
    *,
    container_suffix: str | None = None,
    ffmpeg_path: str | None = None,
    cancel_event: threading.Event | None = None,
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
        cancel_event: 可选的取消信号；置位后合并会被中止。
        logger: 可选的日志记录器。

    Returns:
        :class:`MergeResult`。

    Raises:
        MergeError: 分片缺失/为空、FFmpeg 不可用或执行失败、输出文件校验不通过。
        DownloadCancelled: 合并过程中收到取消信号。
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

    # 已经取消的任务不应再产生任何输出文件，也不该启动 FFmpeg。
    _check_cancel(cancel_event)

    suffix = (container_suffix or target.suffix).lower()
    log.info("合并策略：分片类型=%s，目标扩展名=%s", segment_kind.label, suffix or "(无)")

    if segment_kind is SegmentKind.TS and suffix in _REMUX_SUFFIXES:
        return _remux_with_ffmpeg(
            paths, target, _FFMPEG_MUXERS.get(suffix), ffmpeg_path, log, cancel_event
        )

    return _concat_binary(paths, target, log, cancel_event)


def _concat_binary(
    paths: Sequence[Path],
    target: Path,
    log: logging.Logger,
    cancel_event: threading.Event | None = None,
) -> MergeResult:
    """按顺序二进制拼接分片，并在每个数据块之间响应取消。"""
    expected = sum(p.stat().st_size for p in paths)
    written = 0

    try:
        with open(target, "wb") as output:
            for path in paths:
                with open(path, "rb") as source:
                    while True:
                        # 分块之间检查，取消时立刻停止写入
                        _check_cancel(cancel_event)
                        chunk = source.read(COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
                        written += len(chunk)
    except OSError as exc:
        _remove_quietly(target)
        raise MergeError(f"写入输出文件失败：{exc}") from exc
    except DownloadCancelled:
        _remove_quietly(target)
        raise

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
    cancel_event: threading.Event | None = None,
) -> MergeResult:
    """调用 FFmpeg 把 TS 分片重新封装为目标容器（不重新编码）。

    实现要点：先用字节拼接把分片合成**单个** TS，再让 FFmpeg 重封装这一个
    文件。

    若反过来把这些分片直接交给 FFmpeg 的 concat 分离器，每个分片自带的时间戳
    基准会在交界处产生大量 “non monotonically increasing dts”，并且算出的
    时长偏长（实测 30 个真实分片：3826 条告警，时长多出约 3.3 秒）。字节拼接
    对 MPEG-TS 是合法的等价形式，不改变任何码流内容，先拼接再重封装即完全干净。
    """
    executable = ffmpeg_path or find_ffmpeg()
    if not executable:
        raise MergeError(
            "把 TS 分片封装为 MP4 需要 FFmpeg，但当前系统中没有找到 ffmpeg 可执行文件。\n"
            "请安装 FFmpeg 并加入 PATH，或把输出文件名改为 .ts 后缀（无需 FFmpeg）。"
        )

    combined = target.with_name(target.name + ".combined.ts")
    _remove_quietly(combined)

    try:
        _concat_binary(paths, combined, log, cancel_event)

        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(combined),
            "-c",
            "copy",
        ]
        if muxer:
            # 输出路径可能是 .part 临时文件，必须显式指定容器格式。
            command.extend(["-f", muxer])
        command.append(str(target))

        returncode, stderr = _run_ffmpeg(command, cancel_event)
    except DownloadCancelled:
        _remove_quietly(target)
        raise
    except OSError as exc:
        _remove_quietly(target)
        raise MergeError(f"无法执行 FFmpeg：{exc}") from exc
    except MergeError:
        _remove_quietly(target)
        raise
    finally:
        _remove_quietly(combined)

    if returncode != 0:
        _remove_quietly(target)
        detail = (stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {returncode}"
        raise MergeError(f"FFmpeg 合并失败：{tail}")

    if not target.is_file() or target.stat().st_size == 0:
        _remove_quietly(target)
        raise MergeError("FFmpeg 执行结束但没有生成有效的输出文件。")

    size = target.stat().st_size
    log.info("FFmpeg 重新封装完成，共 %s 字节。", size)
    return MergeResult(output_path=str(target), total_bytes=size, strategy="ffmpeg-remux")


def _run_ffmpeg(
    command: list[str],
    cancel_event: threading.Event | None,
) -> tuple[int, str]:
    """执行 FFmpeg 命令并等待结束，返回 ``(退出码, stderr 文本)``。

    与直接使用 ``subprocess.run`` 不同，这里会轮询子进程：一旦收到取消信号
    就终止 FFmpeg，避免用户在“正在取消…”状态下无限等待。整体耗时仍受
    :data:`FFMPEG_TIMEOUT_SECONDS` 限制。

    Raises:
        DownloadCancelled: 收到取消信号，FFmpeg 已被终止。
        MergeError: 执行超时。
        OSError: 无法启动 FFmpeg（由调用方包装成 MergeError）。
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    deadline = time.monotonic() + FFMPEG_TIMEOUT_SECONDS
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                raise MergeError("FFmpeg 合并超时，任务已中止。")

            try:
                _, stderr = process.communicate(timeout=min(FFMPEG_POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    _terminate_process(process)
                    raise DownloadCancelled("任务已取消。") from None
                continue

            return process.returncode, stderr or ""
    except BaseException:
        # 任何异常路径都不能把 ffmpeg 留在后台继续写目标文件
        _terminate_process(process)
        raise


def _terminate_process(process: subprocess.Popen) -> None:
    """确保子进程已经结束；先 terminate，超时后再 kill。"""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except OSError:  # pragma: no cover - 进程可能已经退出
        pass


def _check_cancel(cancel_event: threading.Event | None) -> None:
    """取消信号已置位时抛出 :class:`DownloadCancelled`。"""
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("任务已取消。")


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 清理失败不应掩盖原始错误
        pass
