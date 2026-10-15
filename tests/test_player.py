"""在线播放器启动器的单元测试。

这些测试**不会真的启动播放器**：``subprocess`` 被替换成记录调用的替身，
只断言命令行参数与进程收尾逻辑。
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from hls_downloader import player
from hls_downloader.app_paths import executable_name as app_paths_executable_name
from hls_downloader.player import (
    PlayerError,
    PlayerNotFoundError,
    find_ffplay,
    launch_ffplay,
    stop_playback,
)

URL = "https://cdn.test/video/index.m3u8?wsSecret=s3cret&wsTime=1a2b3c"
FFPLAY = r"E:\ffmpeg\bin\ffplay.exe"


class _FakeProcess:
    """记录 terminate / wait / kill 调用的假进程。"""

    def __init__(
        self, *, alive: bool = True, stderr_lines: list[str] | None = None
    ) -> None:
        self._alive = alive
        self.terminated = False
        self.killed = False
        self.waited: list[float | None] = []
        self.pid = 4242
        self.returncode = None
        # FfplayProcess 会把这个流读空并留存最后若干行
        self.stderr = [line.encode("utf-8") for line in (stderr_lines or [])]

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        self.waited.append(timeout)
        return 0

    def kill(self) -> None:
        self.killed = True
        self._alive = False


class _StubbornProcess(_FakeProcess):
    """terminate 之后仍然不退出的进程。"""

    def wait(self, timeout=None):
        self.waited.append(timeout)
        raise subprocess.TimeoutExpired(cmd="ffplay", timeout=timeout)


class _SubprocessStub:
    """替换 ``player.subprocess``，避免改动真正的 subprocess 模块。"""

    DEVNULL = subprocess.DEVNULL
    PIPE = subprocess.PIPE
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, process: object | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self._process = process
        self._error = error

    def Popen(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self._error is not None:
            raise self._error
        return self._process if self._process is not None else _FakeProcess()


class _WhichStub:
    """替换 ``player.shutil``，只为 ``which`` 提供实现。"""

    def __init__(self, result: str | None) -> None:
        self.result = result
        self.seen: list[str] = []

    def which(self, name: str) -> str | None:
        self.seen.append(name)
        return self.result


# --------------------------------------------------------------------------
# 查找可执行文件
# --------------------------------------------------------------------------


def test_find_ffplay_looks_it_up_on_path(monkeypatch):
    stub = _WhichStub(FFPLAY)
    monkeypatch.setattr(player, "shutil", stub)

    assert find_ffplay() == FFPLAY
    assert stub.seen == [player.FFPLAY_EXECUTABLE]


def test_find_ffplay_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(player, "shutil", _WhichStub(None))

    assert find_ffplay() is None


# --------------------------------------------------------------------------
# 启动播放器
# --------------------------------------------------------------------------


def test_launch_ffplay_passes_the_url_as_one_argument(monkeypatch):
    stub = _SubprocessStub()
    monkeypatch.setattr(player, "subprocess", stub)

    process = launch_ffplay(URL, ffplay_path=FFPLAY)

    assert isinstance(process, player.FfplayProcess)
    argv, kwargs = stub.calls[0]
    assert argv[0] == FFPLAY
    # 地址必须成一个独立的参数，且跟在 -i 后面：里面的 & 不能被当成命令分隔符
    assert argv[argv.index("-i") + 1] == URL
    assert argv[-1] == URL
    # 绝不能走 shell
    assert "shell" not in kwargs
    assert not kwargs.get("shell", False)


def test_launch_ffplay_never_writes_player_output_to_disk(monkeypatch):
    """播放器输出必须收进内存：它既含失败原因，也含带签名的地址。

    重定向到文件 = 把签名落到磁盘上；直接丢弃 = 失败时完全无从排查。
    所以 stdout 丢弃、stderr 收进管道由 :class:`FfplayProcess` 读走。
    """
    stub = _SubprocessStub()
    monkeypatch.setattr(player, "subprocess", stub)

    launch_ffplay(URL, ffplay_path=FFPLAY)

    _, kwargs = stub.calls[0]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.PIPE


@pytest.mark.skipif(os.name != "nt", reason="只有 Windows 需要抑制控制台窗口")
def test_launch_ffplay_suppresses_the_console_window_on_windows(monkeypatch):
    stub = _SubprocessStub()
    monkeypatch.setattr(player, "subprocess", stub)

    launch_ffplay(URL, ffplay_path=FFPLAY)

    _, kwargs = stub.calls[0]
    assert kwargs["creationflags"] == _SubprocessStub.CREATE_NO_WINDOW


def test_launch_ffplay_uses_the_window_title(monkeypatch):
    stub = _SubprocessStub()
    monkeypatch.setattr(player, "subprocess", stub)

    launch_ffplay(URL, ffplay_path=FFPLAY, title="我的在线播放")

    argv, _ = stub.calls[0]
    assert argv[argv.index("-window_title") + 1] == "我的在线播放"


def test_launch_ffplay_without_ffplay_reports_it(monkeypatch):
    monkeypatch.setattr(player, "find_ffplay", lambda: None)

    with pytest.raises(PlayerNotFoundError) as excinfo:
        launch_ffplay(URL)

    message = str(excinfo.value)
    assert "ffplay" in message
    assert "PATH" in message


def test_launch_ffplay_falls_back_to_the_path_lookup(monkeypatch):
    stub = _SubprocessStub()
    monkeypatch.setattr(player, "subprocess", stub)
    monkeypatch.setattr(player, "find_ffplay", lambda: FFPLAY)

    launch_ffplay(URL)

    assert stub.calls[0][0][0] == FFPLAY


def test_launch_ffplay_translates_startup_failures(monkeypatch):
    stub = _SubprocessStub(error=OSError("拒绝访问"))
    monkeypatch.setattr(player, "subprocess", stub)

    with pytest.raises(PlayerError) as excinfo:
        launch_ffplay(URL, ffplay_path=FFPLAY)

    assert "无法启动播放器" in str(excinfo.value)


# --------------------------------------------------------------------------
# 停止播放器
# --------------------------------------------------------------------------


def test_stop_playback_ignores_a_missing_process():
    stop_playback(None)  # 不应抛异常


def test_stop_playback_ignores_an_already_finished_process():
    process = _FakeProcess(alive=False)

    stop_playback(process)

    assert process.terminated is False
    assert process.killed is False


def test_stop_playback_terminates_a_running_process():
    process = _FakeProcess(alive=True)

    stop_playback(process)

    assert process.terminated is True
    assert process.killed is False
    assert process.waited == [player.STOP_TIMEOUT_SECONDS]


def test_stop_playback_kills_a_process_that_refuses_to_exit():
    process = _StubbornProcess(alive=True)

    stop_playback(process)

    assert process.terminated is True
    assert process.killed is True


# --------------------------------------------------------------------------
# 播放器进程句柄：保留失败原因（内存里，且脱敏）
# --------------------------------------------------------------------------


def _handle(raw: _FakeProcess) -> player.FfplayProcess:
    """把假进程包成句柄，并等 stderr 读线程跑完。"""
    handle = player.FfplayProcess(raw)
    handle._reader.join(timeout=2.0)
    return handle


def test_playback_process_forwards_the_process_calls():
    raw = _FakeProcess(alive=True)
    handle = _handle(raw)

    assert handle.poll() is None
    assert handle.pid == raw.pid

    handle.terminate()
    assert raw.terminated is True
    assert handle.poll() == 0


def test_playback_process_reports_how_long_it_ran():
    handle = _handle(_FakeProcess(alive=True))

    assert handle.elapsed_seconds() >= 0.0


def test_stop_playback_accepts_a_playback_process():
    """stop_playback 只用到那四个方法，句柄应当能直接传进去。"""
    raw = _FakeProcess(alive=True)
    handle = _handle(raw)

    stop_playback(handle)

    assert raw.terminated is True


def test_failure_hint_reports_what_the_player_said():
    raw = _FakeProcess(
        alive=False,
        stderr_lines=[
            "http://cdn.test:8766/index.m3u8: Server returned 403 Forbidden",
            "Failed to open segment 3 of playlist 0",
        ],
    )

    hint = _handle(raw).failure_hint()

    assert "403 Forbidden" in hint
    assert "Failed to open segment 3" in hint


def test_failure_hint_redacts_the_signed_url():
    """播放器的输出会带上完整签名，展示前必须脱敏。"""
    raw = _FakeProcess(
        alive=False,
        stderr_lines=[
            "https://cdn.test/video/index.m3u8?wsSecret=s3cret&wsTime=1a2b: "
            "Server returned 403 Forbidden"
        ],
    )

    hint = _handle(raw).failure_hint()

    assert "s3cret" not in hint
    assert "wsSecret" not in hint
    # 主机名与文件名保留，否则这条提示就没法用来排查了
    assert "cdn.test" in hint
    assert "index.m3u8" in hint
    assert "403 Forbidden" in hint


def test_failure_hint_keeps_a_trailing_colon_out_of_the_url():
    raw = _FakeProcess(
        alive=False,
        stderr_lines=["https://cdn.test/a.m3u8?token=secret: Server returned 500"],
    )

    hint = _handle(raw).failure_hint()

    assert hint.endswith("Server returned 500")
    assert "token=secret" not in hint


def test_failure_hint_is_empty_when_the_player_said_nothing():
    raw = _FakeProcess(alive=False)

    assert _handle(raw).failure_hint() == ""


class _SlowStream:
    """逐行、带延迟地吐字节，用来模拟还没读完的管道。"""

    def __init__(self, lines: list[str], delay: float) -> None:
        self._lines = lines
        self._delay = delay

    def __iter__(self):
        for line in self._lines:
            time.sleep(self._delay)
            yield line.encode("utf-8")


def test_failure_hint_waits_for_the_reader_to_drain():
    """播放器退出后管道里可能还有没读完的行，读完再取——那往往正是错误原因。"""

    class _SlowProcess(_FakeProcess):
        def __init__(self) -> None:
            super().__init__(alive=False)
            self.stderr = _SlowStream(["第一行", "迟到的原因"], delay=0.05)

    hint = player.FfplayProcess(_SlowProcess()).failure_hint()

    assert "第一行" in hint
    assert "迟到的原因" in hint


def test_stop_playback_reaps_a_killed_process():
    """强杀之后要再 wait 一次回收，否则 POSIX 上留下僵尸、returncode 永远是 None。"""

    class _ReapStubbornProcess(_StubbornProcess):
        def __init__(self) -> None:
            super().__init__(alive=True)
            self.waited_after_kill = 0

        def wait(self, timeout=None):
            self.waited.append(timeout)
            if self.killed:
                self.waited_after_kill += 1
                return 0
            raise subprocess.TimeoutExpired(cmd="ffplay", timeout=timeout)

    process = _ReapStubbornProcess()

    stop_playback(process)

    assert process.killed is True
    assert process.waited_after_kill >= 1


def test_failure_hint_keeps_only_the_last_lines():
    raw = _FakeProcess(
        alive=False,
        stderr_lines=[f"第 {i} 行" for i in range(player.STDERR_KEEP_LINES + 25)],
    )

    hint = _handle(raw).failure_hint()

    assert "第 0 行" not in hint
    assert f"第 {player.STDERR_KEEP_LINES + 24} 行" in hint
    assert len(hint.splitlines()) == player.STDERR_KEEP_LINES


def test_playback_process_tolerates_a_process_without_stderr():
    class _NoStderr:
        stderr = None

        def poll(self):
            return None

    handle = player.FfplayProcess(_NoStderr())
    handle._reader.join(timeout=2.0)

    assert handle.failure_hint() == ""


# --------------------------------------------------------------------------
# mpv：IPC 控制
# --------------------------------------------------------------------------

MPV = r"C:\mpv\mpv.exe"


class _FakeTransport:
    """假的 IPC 通道：记录发出的命令，把预置的事件逐行吐出来。"""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._lines = list(lines or [])

    def send(self, payload: dict) -> None:
        self.sent.append(payload)

    def poll_lines(self) -> list[str] | None:
        # 一次性把预置的行吐完，然后模拟通道关闭
        if not self._lines:
            return None
        lines, self._lines = self._lines, []
        return lines

    def close(self) -> None:
        self.closed = True

    @property
    def commands(self) -> list[list]:
        return [payload["command"] for payload in self.sent]


def _mpv_handle(lines: list[str] | None = None) -> "tuple[player.MpvProcess, _FakeTransport]":
    """造一个连着假通道的 mpv 句柄，并等事件读线程跑完。"""
    transport = _FakeTransport(lines)
    handle = player.MpvProcess(_FakeProcess(alive=True), transport)
    handle._reader.join(timeout=2.0)
    return handle, transport


def test_find_mpv_looks_it_up_on_path(monkeypatch):
    monkeypatch.setattr(player, "find_bundled_tool", lambda name: None)
    stub = _WhichStub(MPV)
    monkeypatch.setattr(player, "shutil", stub)

    assert player.find_mpv() == MPV
    assert stub.seen == [player.MPV_EXECUTABLE]


def test_find_mpv_prefers_a_version_shipped_with_the_app(monkeypatch, tmp_path):
    bundled = tmp_path / "mpv.exe"
    monkeypatch.setattr(player, "find_bundled_tool", lambda name: bundled)
    monkeypatch.setattr(player, "shutil", _WhichStub(MPV))

    assert player.find_mpv() == str(bundled)


def test_find_mpv_falls_back_to_known_install_locations(tmp_path, monkeypatch):
    """winget 装的 mpv **不进 PATH**：只查 PATH 会「明明装了却找不到」。"""
    root = tmp_path / "ProgramFiles"
    (root / "MPV Player").mkdir(parents=True)
    binary = root / "MPV Player" / app_paths_executable_name("mpv")
    binary.write_text("", encoding="utf-8")

    monkeypatch.setattr(player, "find_bundled_tool", lambda name: None)
    monkeypatch.setattr(player, "shutil", _WhichStub(None))
    monkeypatch.setitem(os.environ, "ProgramFiles", str(root))
    monkeypatch.setattr(player, "_MPV_KNOWN_LOCATIONS", (("ProgramFiles", "MPV Player"),))

    assert player.find_mpv() == str(binary)


def test_find_mpv_uses_the_extension_when_joining_paths(tmp_path, monkeypatch):
    """手工拼路径不会自动补扩展名，漏了就会去找 ``mpv`` 而不是 ``mpv.exe``。"""
    root = tmp_path / "root"
    (root / "mpv").mkdir(parents=True)
    (root / "mpv" / "mpv").write_text("没有扩展名", encoding="utf-8")
    binary = root / "mpv" / app_paths_executable_name("mpv")
    binary.write_text("", encoding="utf-8")

    monkeypatch.setattr(player, "find_bundled_tool", lambda name: None)
    monkeypatch.setattr(player, "shutil", _WhichStub(None))
    monkeypatch.setitem(os.environ, "MYAPPROOT", str(root))
    monkeypatch.setattr(player, "_MPV_KNOWN_LOCATIONS", (("MYAPPROOT", "mpv"),))

    assert player.find_mpv() == str(binary)


def test_launch_mpv_builds_the_expected_arguments(monkeypatch):
    stub = _SubprocessStub()
    monkeypatch.setattr(player, "subprocess", stub)
    transport = _FakeTransport()
    monkeypatch.setattr(player, "_connect_ipc", lambda path, timeout: transport)

    endpoint = r"\\.\pipe\test"
    player.launch_mpv(URL, mpv_path=MPV, speed=1.5, ipc_path=endpoint)

    argv, kwargs = stub.calls[0]
    assert argv[0] == MPV
    assert argv[-1] == URL
    assert f"--input-ipc-server={endpoint}" in argv
    assert "--speed=1.5" in argv
    assert "--no-terminal" in argv
    # 位置不落盘：这个功能整体不写文件
    assert "--save-position-on-quit=no" in argv
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert "shell" not in kwargs


def test_launch_mpv_without_mpv_reports_it(monkeypatch):
    monkeypatch.setattr(player, "find_mpv", lambda: None)

    with pytest.raises(PlayerNotFoundError) as excinfo:
        player.launch_mpv(URL)

    assert "mpv" in str(excinfo.value)


def test_launch_mpv_subscribes_to_the_properties_it_needs(monkeypatch):
    stub = _SubprocessStub()
    monkeypatch.setattr(player, "subprocess", stub)
    transport = _FakeTransport()
    monkeypatch.setattr(player, "_connect_ipc", lambda path, timeout: transport)

    player.launch_mpv(URL, mpv_path=MPV)

    commands = transport.commands
    assert ["observe_property", 1, "time-pos"] in commands
    assert ["observe_property", 2, "duration"] in commands
    assert ["request_log_messages", "error"] in commands


def test_mpv_survives_an_unreachable_ipc_endpoint(monkeypatch):
    """IPC 连不上不影响播放，只是没有进度条和倍速。"""
    stub = _SubprocessStub()
    monkeypatch.setattr(player, "subprocess", stub)
    monkeypatch.setattr(player, "_connect_ipc", lambda path, timeout: None)

    handle = player.launch_mpv(URL, mpv_path=MPV)

    assert isinstance(handle, player.MpvProcess)
    assert handle.supports_control is False
    assert handle.set_speed(1.5) is False
    assert handle.seek(30.0) is False
    assert handle.position() is None


def test_mpv_process_tracks_position_and_duration():
    handle, _ = _mpv_handle(
        [
            '{"event":"property-change","name":"duration","data":600.5}',
            '{"event":"property-change","name":"time-pos","data":42.25}',
        ]
    )

    assert handle.duration() == pytest.approx(600.5)
    assert handle.position() == pytest.approx(42.25)


def test_mpv_process_ignores_unknown_and_malformed_messages():
    handle, _ = _mpv_handle(
        [
            "这不是 JSON",
            '{"event":"something-else","data":1}',
            '{"event":"property-change","name":"time-pos","data":null}',
            '{"event":"property-change","name":"time-pos","data":7.5}',
        ]
    )

    assert handle.position() == pytest.approx(7.5)


def test_mpv_process_sends_seek_and_speed_commands():
    handle, transport = _mpv_handle()

    assert handle.seek(91.5) is True
    assert handle.set_speed(1.5) is True

    assert ["seek", 91.5, "absolute"] in transport.commands
    assert ["set_property", "speed", 1.5] in transport.commands


def test_mpv_process_clamps_the_speed_range():
    handle, transport = _mpv_handle()
    before = len(transport.sent)

    assert handle.set_speed(0.0) is False
    assert handle.set_speed(1000.0) is False

    assert len(transport.sent) == before


def test_mpv_process_reports_a_clean_end():
    handle, _ = _mpv_handle(['{"event":"end-file","reason":"eof"}'])

    assert handle.end_reason() == "eof"
    assert handle.ended_with_error() is False


def test_mpv_process_reports_a_failed_end():
    handle, _ = _mpv_handle(['{"event":"end-file","reason":"error"}'])

    assert handle.ended_with_error() is True


def test_mpv_process_reports_errors_from_the_log():
    handle, _ = _mpv_handle(
        [
            '{"event":"log-message","level":"error","prefix":"hls",'
            '"text":"Failed to open https://cdn.test/s.m3u8?wsSecret=s3cret"}'
        ]
    )

    hint = handle.failure_hint()

    assert handle.ended_with_error() is True
    assert "s3cret" not in hint
    assert "wsSecret" not in hint
    assert "Failed to open" in hint


def test_stop_playback_closes_the_ipc_transport():
    """不断开通道的话，读线程会一直挂在管道上。"""
    raw = _FakeProcess(alive=True)
    transport = _FakeTransport()
    handle = player.MpvProcess(raw, transport)

    stop_playback(handle)

    assert transport.closed is True
    assert raw.terminated is True


# --------------------------------------------------------------------------
# 播放器选择
# --------------------------------------------------------------------------


def test_launch_playback_prefers_mpv(monkeypatch):
    calls = []
    monkeypatch.setattr(player, "find_mpv", lambda: MPV)
    monkeypatch.setattr(player, "launch_mpv", lambda url, **kw: calls.append(("mpv", kw)) or "mpv-handle")
    monkeypatch.setattr(player, "launch_ffplay", lambda url, **kw: calls.append(("ffplay", kw)) or "ffplay-handle")

    assert player.launch_playback(URL, speed=1.5) == "mpv-handle"
    assert calls[0][0] == "mpv"
    assert calls[0][1]["speed"] == 1.5


def test_launch_playback_falls_back_to_ffplay(monkeypatch):
    monkeypatch.setattr(player, "find_mpv", lambda: None)
    monkeypatch.setattr(player, "launch_ffplay", lambda url, **kw: "ffplay-handle")
    monkeypatch.setattr(player, "launch_mpv", lambda *a, **k: pytest.fail("不该走 mpv"))

    assert player.launch_playback(URL) == "ffplay-handle"


def test_launch_playback_needs_at_least_one_player(monkeypatch):
    monkeypatch.setattr(player, "find_mpv", lambda: None)
    monkeypatch.setattr(player, "find_ffplay", lambda: None)

    with pytest.raises(PlayerNotFoundError):
        player.launch_playback(URL)
