"""分片合并与 FFmpeg 封装的单元测试。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hls_downloader import merger
from hls_downloader.models import DownloadCancelled, MergeError, SegmentKind


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_binary_concat_for_ts(tmp_path):
    paths = [_write(tmp_path / f"s{i}.ts", bytes([i]) * 10) for i in range(3)]
    target = tmp_path / "out.ts"

    result = merger.merge_segments(paths, target, SegmentKind.TS)

    assert target.read_bytes() == bytes([0]) * 10 + bytes([1]) * 10 + bytes([2]) * 10
    assert result.total_bytes == 30
    assert result.strategy == "concat"
    assert result.output_path == str(target)


def test_fmp4_concat_keeps_init_segment_first(tmp_path):
    init = _write(tmp_path / "init.mp4", b"INIT")
    segment = _write(tmp_path / "s0.m4s", b"DATA")

    result = merger.merge_segments([init, segment], tmp_path / "out.mp4", SegmentKind.FMP4)

    assert (tmp_path / "out.mp4").read_bytes() == b"INITDATA"
    assert result.strategy == "concat"


def test_missing_segment_file_raises(tmp_path):
    paths = [_write(tmp_path / "s0.ts", b"DATA"), tmp_path / "missing.ts"]

    with pytest.raises(MergeError) as excinfo:
        merger.merge_segments(paths, tmp_path / "out.ts", SegmentKind.TS)

    assert "缺失" in excinfo.value.message
    assert not (tmp_path / "out.ts").exists()


def test_empty_segment_file_raises(tmp_path):
    paths = [_write(tmp_path / "s0.ts", b"DATA"), _write(tmp_path / "s1.ts", b"")]

    with pytest.raises(MergeError) as excinfo:
        merger.merge_segments(paths, tmp_path / "out.ts", SegmentKind.TS)

    assert "为空" in excinfo.value.message


def test_no_segments_raises(tmp_path):
    with pytest.raises(MergeError):
        merger.merge_segments([], tmp_path / "out.ts", SegmentKind.TS)


def test_ts_to_mp4_without_ffmpeg_explains_how_to_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(merger, "find_ffmpeg", lambda: None)
    paths = [_write(tmp_path / "s0.ts", b"TSDATA")]
    target = tmp_path / "out.mp4"

    with pytest.raises(MergeError) as excinfo:
        merger.merge_segments(paths, target, SegmentKind.TS)

    message = excinfo.value.message
    assert "FFmpeg" in message
    assert ".ts" in message  # 提示可以改用 .ts 输出
    assert not target.exists()


def _fake_ffmpeg(
    recorded: dict, *, returncode: int = 0, stderr: str = "", write: bytes | None = b"REMUXED"
):
    """替身：接替 merger._run_ffmpeg，返回 (returncode, stderr)。"""

    def run(command, cancel_event):
        recorded["command"] = command
        recorded["cancel_event"] = cancel_event
        if write is not None:
            Path(command[-1]).write_bytes(write)
        return returncode, stderr

    return run


def test_ffmpeg_remux_uses_explicit_format(tmp_path, monkeypatch):
    recorded: dict = {}
    monkeypatch.setattr(merger, "_run_ffmpeg", _fake_ffmpeg(recorded))
    paths = [_write(tmp_path / "s0.ts", b"TSDATA")]
    target = tmp_path / "out.mp4"

    result = merger.merge_segments(paths, target, SegmentKind.TS, ffmpeg_path="ffmpeg")

    assert result.strategy == "ffmpeg-remux"
    assert target.read_bytes() == b"REMUXED"
    command = recorded["command"]
    assert command[0] == "ffmpeg"
    assert command[command.index("-c") + 1] == "copy"  # 不重新编码
    # 输出格式必须显式指定，且是最后一个 -f（前面的 -f concat 是输入格式）
    assert command[-3:] == ["-f", "mp4", str(target)]


def test_ffmpeg_uses_container_suffix_for_part_file(tmp_path, monkeypatch):
    """下载器会先合并到 .part 临时文件，容器格式必须按最终扩展名决定。"""
    recorded: dict = {}
    monkeypatch.setattr(merger, "_run_ffmpeg", _fake_ffmpeg(recorded))
    paths = [_write(tmp_path / "s0.ts", b"TSDATA")]
    staging = tmp_path / "out.mp4.part"

    result = merger.merge_segments(
        paths, staging, SegmentKind.TS, container_suffix=".mp4", ffmpeg_path="ffmpeg"
    )

    assert result.strategy == "ffmpeg-remux"
    assert staging.read_bytes() == b"REMUXED"
    command = recorded["command"]
    assert command[-1] == str(staging)
    assert command[-3:-1] == ["-f", "mp4"]


def test_ffmpeg_failure_removes_partial_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        merger,
        "_run_ffmpeg",
        _fake_ffmpeg({}, returncode=1, stderr="boom: invalid data"),
    )
    paths = [_write(tmp_path / "s0.ts", b"TSDATA")]
    target = tmp_path / "out.mp4"

    with pytest.raises(MergeError) as excinfo:
        merger.merge_segments(paths, target, SegmentKind.TS, ffmpeg_path="ffmpeg")

    assert "invalid data" in excinfo.value.message
    assert not target.exists()


def test_ffmpeg_success_without_output_file_is_rejected(tmp_path, monkeypatch):
    """FFmpeg 返回 0 但没有生成文件时，不能报告成功。"""
    monkeypatch.setattr(merger, "_run_ffmpeg", _fake_ffmpeg({}, write=None))
    paths = [_write(tmp_path / "s0.ts", b"TSDATA")]
    target = tmp_path / "out.mp4"

    with pytest.raises(MergeError) as excinfo:
        merger.merge_segments(paths, target, SegmentKind.TS, ffmpeg_path="ffmpeg")

    assert "有效" in excinfo.value.message


def test_ffmpeg_concat_list_file_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr(merger, "_run_ffmpeg", _fake_ffmpeg({}))
    paths = [_write(tmp_path / "s0.ts", b"TSDATA")]

    merger.merge_segments(paths, tmp_path / "out.mp4", SegmentKind.TS, ffmpeg_path="ffmpeg")

    assert [p.name for p in tmp_path.iterdir()] == ["out.mp4", "s0.ts"]


def test_ffmpeg_execution_error_is_wrapped(tmp_path, monkeypatch):
    def run(command, cancel_event):
        raise OSError("ffmpeg 不可用")

    monkeypatch.setattr(merger, "_run_ffmpeg", run)
    paths = [_write(tmp_path / "s0.ts", b"TSDATA")]
    target = tmp_path / "out.mp4"

    with pytest.raises(MergeError) as excinfo:
        merger.merge_segments(paths, target, SegmentKind.TS, ffmpeg_path="ffmpeg")

    assert "FFmpeg" in excinfo.value.message
    assert not target.exists()


def test_find_ffmpeg_returns_string_or_none():
    found = merger.find_ffmpeg()
    assert found is None or isinstance(found, str)


# --------------------------------------------------------------------------
# 回归测试：合并阶段必须响应取消
# --------------------------------------------------------------------------


def test_concat_aborts_when_already_cancelled(tmp_path):
    """取消信号已置位时，二进制拼接必须立刻中止且不留下输出。"""
    cancel = threading.Event()
    cancel.set()
    paths = [_write(tmp_path / f"s{i}.ts", b"D" * 100) for i in range(3)]
    target = tmp_path / "out.ts"

    with pytest.raises(DownloadCancelled):
        merger.merge_segments(paths, target, SegmentKind.TS, cancel_event=cancel)

    assert not target.exists(), "取消后不应留下半成品输出"


def test_remux_aborts_when_already_cancelled(tmp_path, monkeypatch):
    """取消信号已置位时，FFmpeg 重新封装不应被启动。"""
    started = {"value": False}

    def run(command, cancel_event):
        started["value"] = True
        return 0, ""

    monkeypatch.setattr(merger, "_run_ffmpeg", run)
    cancel = threading.Event()
    cancel.set()
    paths = [_write(tmp_path / "s0.ts", b"TSDATA")]
    target = tmp_path / "out.mp4"

    with pytest.raises(DownloadCancelled):
        merger.merge_segments(
            paths, target, SegmentKind.TS, ffmpeg_path="ffmpeg", cancel_event=cancel
        )

    assert not started["value"], "已取消的任务不应再启动 FFmpeg"
    assert not target.exists()


def test_remux_cancel_propagates_and_cleans_up(tmp_path, monkeypatch):
    """FFmpeg 执行期间被取消时，取消异常要向上传递，且清理中间文件。"""
    recorded: dict = {}

    def run(command, cancel_event):
        recorded["command"] = command
        raise DownloadCancelled("任务已取消。")

    monkeypatch.setattr(merger, "_run_ffmpeg", run)
    paths = [_write(tmp_path / "s0.ts", b"TSDATA")]
    target = tmp_path / "out.mp4"

    with pytest.raises(DownloadCancelled):
        merger.merge_segments(paths, target, SegmentKind.TS, ffmpeg_path="ffmpeg")

    assert not target.exists()
    # 供 FFmpeg 使用的中间拼接文件也必须被清理
    assert [p.name for p in tmp_path.iterdir()] == ["s0.ts"]


def test_ffmpeg_remux_concatenates_bytes_before_remuxing(tmp_path, monkeypatch):
    """重封装前必须先字节拼接成单个 TS，而不是把分片交给 concat 分离器。

    直接把多个分片交给 FFmpeg 的 concat 分离器，会让每个分片自带的时间戳基准
    在交界处冲突，产生大量 "non monotonically increasing dts" 告警，并算出偏长
    的时长（真实流上实测 30 个分片会有 3826 条告警、时长多出约 3.3 秒）。
    """
    recorded: dict = {}

    def run(command, cancel_event):
        recorded["command"] = command
        index = command.index("-i") + 1
        source = Path(command[index])
        recorded["input_exists"] = source.is_file()
        recorded["input_bytes"] = source.read_bytes() if source.is_file() else b""
        Path(command[-1]).write_bytes(b"REMUXED")
        return 0, ""

    monkeypatch.setattr(merger, "_run_ffmpeg", run)
    chunks = [b"AAA", b"BBB", b"CCC"]
    paths = [_write(tmp_path / f"s{i}.ts", data) for i, data in enumerate(chunks)]

    result = merger.merge_segments(
        paths, tmp_path / "out.mp4", SegmentKind.TS, ffmpeg_path="ffmpeg"
    )

    command = recorded["command"]
    assert "concat" not in command, "不应再把分片交给 concat 分离器"
    assert recorded["input_exists"], "交给 FFmpeg 的输入文件不存在"
    assert recorded["input_bytes"] == b"AAABBBCCC", "交给 FFmpeg 的应是已拼接好的单个 TS"
    assert result.strategy == "ffmpeg-remux"
    # 中间拼接文件必须被清理，只留下产物与原始分片
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.mp4", "s0.ts", "s1.ts", "s2.ts"]
