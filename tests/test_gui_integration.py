"""图形界面集成测试：需要图形环境，没有时会自动跳过。

这些测试会真正创建主窗口，并驱动完整的“启动 → 后台任务 → 界面恢复”流程，
用于验证：

* 后台线程不直接修改控件，界面状态通过消息队列恢复；
* 任务失败或取消后，按钮与输入框恢复可用（界面不会永久卡死）；
* 宽窄窗口下左侧/右侧面板的布局切换；
* 日志区域的容量上限与清空行为。

实现说明：Tk 在同一个进程内反复创建/销毁根窗口时，可能出现
``Can't find a usable tk.tcl`` 之类的偶发初始化失败。因此这里只在模块级
创建一次窗口，并在每个用例之间重置状态，让测试可以稳定重复执行。
"""

from __future__ import annotations

import threading
import time

import pytest

from hls_downloader.models import DownloadCancelled, HLSDownloaderError
from hls_downloader.player import SPEED_CHOICES

pytestmark = pytest.mark.integration


def _pump(window, until, timeout: float = 15.0) -> bool:
    """驱动 Tk 事件循环，直到条件满足或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window.update()
        if until():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(scope="module")
def _shared_window():
    """整个模块共用同一个根窗口，避免 Tk 反复初始化导致的偶发失败。"""
    import tkinter as tk

    pytest.importorskip("customtkinter")
    from gui import main_window as mw

    with pytest.MonkeyPatch.context() as patcher:
        # 测试中不弹出模态对话框
        patcher.setattr(mw.messagebox, "showerror", lambda *a, **k: None)
        patcher.setattr(mw.messagebox, "showwarning", lambda *a, **k: None)
        patcher.setattr(mw.messagebox, "askyesno", lambda *a, **k: True)

        try:
            win = mw.MainWindow()
        except tk.TclError as exc:  # pragma: no cover - 取决于运行环境
            pytest.skip(f"没有可用的图形环境：{exc}")

        win.withdraw()
        win.update()
        yield win

        try:
            win.destroy()
        except tk.TclError:  # pragma: no cover - 解释器已退出
            pass


@pytest.fixture
def window(_shared_window):
    """每个用例拿到一个状态干净的窗口。"""
    from gui import main_window as mw

    win = _shared_window

    win._closing = False
    win._worker = None
    win._cancel_event = None
    win._filename_customized = False
    win._wide = True
    if win._playback_poll_id is not None:
        try:
            win.after_cancel(win._playback_poll_id)
        except Exception:  # pragma: no cover - 控件可能已销毁
            pass
        win._playback_poll_id = None
    win._playback_worker = None
    win._playback_cancel = None
    win._playback_process = None
    win._playback_generation = 0
    win._playback_speed = 1.0
    win.playback_panel.reset()
    # 倍速档位是会被记住的（这是有意的），用例之间要复位：上个用例停在 2× 的话，
    # 下个用例的加速按钮一开始就是灰的
    win.playback_panel._speed_index = SPEED_CHOICES.index(1.0)
    win.playback_panel._update_speed_label()
    win._apply_responsive_layout(wide=True)
    win._set_running(False)
    win.status_panel.reset()
    win.log_panel.clear()
    win.url_entry.set("")
    win.optional_a_entry.set("")
    win.optional_b_entry.set("")
    win.filename_entry.set("video.ts")
    win.overwrite_var.set(False)
    win.resume_var.set(True)
    win.workers_var.set(str(mw.DEFAULT_WORKERS))
    win.dir_entry.set(str(win._default_output_dir()))

    while not win._message_queue.empty():
        win._message_queue.get_nowait()

    return win


def _prepare(window, tmp_path) -> None:
    window.url_entry.set("https://cdn.test/video/index.m3u8")
    window.dir_entry.set(str(tmp_path))
    window.filename_entry.set("out.ts")


# --------------------------------------------------------------------------
# 后台任务与界面状态恢复
# --------------------------------------------------------------------------


def test_gui_restores_state_after_worker_failure(window, monkeypatch, tmp_path):
    from gui import main_window as mw

    def boom(**_kwargs):
        raise HLSDownloaderError("测试注入的下载失败")

    monkeypatch.setattr(mw, "download_hls_video", boom)
    _prepare(window, tmp_path)

    window._on_start()

    assert _pump(
        window, lambda: window.start_button.cget("state") == "normal"
    ), "任务结束后开始按钮未恢复可用"

    assert window.start_button.cget("state") == "normal"
    assert window.cancel_button.cget("state") == "disabled"
    assert window.url_entry.entry.cget("state") == "normal"
    assert not window._is_running()

    log_text = window.log_panel.textbox.get("1.0", "end")
    assert "测试注入的下载失败" in log_text
    assert "下载失败" in window.status_panel._stage_label.cget("text")


def test_gui_restores_state_after_cancellation(window, monkeypatch, tmp_path):
    from gui import main_window as mw

    def cancelled(**_kwargs):
        raise DownloadCancelled("任务已取消。")

    monkeypatch.setattr(mw, "download_hls_video", cancelled)
    _prepare(window, tmp_path)

    window._on_start()

    assert _pump(
        window, lambda: window.cancel_button.cget("state") == "disabled"
    ), "取消后取消按钮未恢复禁用"

    assert window.start_button.cget("state") == "normal"
    assert window.url_entry.entry.cget("state") == "normal"
    log_text = window.log_panel.textbox.get("1.0", "end")
    assert "任务已取消" in log_text
    assert "已取消" in window.status_panel._stage_label.cget("text")


def test_gui_reports_success_and_restores_state(window, monkeypatch, tmp_path):
    from gui import main_window as mw
    from hls_downloader.models import DownloadResult, SegmentKind

    def succeed(**kwargs):
        output = kwargs["output_path"]
        with open(output, "wb") as handle:
            handle.write(b"FAKE-VIDEO")
        return DownloadResult(
            output_path=output,
            segment_count=3,
            total_bytes=10,
            elapsed_seconds=1.5,
            segment_kind=SegmentKind.TS,
        )

    monkeypatch.setattr(mw, "download_hls_video", succeed)
    _prepare(window, tmp_path)

    window._on_start()

    assert _pump(window, lambda: window.start_button.cget("state") == "normal")

    assert window.cancel_button.cget("state") == "disabled"
    log_text = window.log_panel.textbox.get("1.0", "end")
    assert "下载完成" in log_text
    assert "下载完成" in window.status_panel._stage_label.cget("text")


def test_gui_rejects_invalid_url_without_starting_a_task(window, tmp_path):
    window.url_entry.set("ftp://cdn.test/video/index.m3u8")
    window.dir_entry.set(str(tmp_path))

    window._on_start()

    assert window._worker is None
    assert window.start_button.cget("state") == "normal"
    assert "链接无效" in window.log_panel.textbox.get("1.0", "end")


def test_gui_ignores_second_start_while_running(window, monkeypatch, tmp_path):
    import threading

    from gui import main_window as mw

    started: list[str] = []
    release = threading.Event()

    def blocking(**kwargs):
        started.append(kwargs["playlist_url"])
        release.wait(5.0)
        raise DownloadCancelled("done")

    monkeypatch.setattr(mw, "download_hls_video", blocking)
    _prepare(window, tmp_path)

    window._on_start()
    first_worker = window._worker

    window._on_start()  # 第二次点击应被忽略
    assert window._worker is first_worker
    assert len(started) == 1

    release.set()
    assert _pump(window, lambda: not window._is_running())


# --------------------------------------------------------------------------
# 布局与控件状态
# --------------------------------------------------------------------------


class _FakeConfigure:
    """最小化的 Configure 事件替身。"""

    def __init__(self, widget, width: int) -> None:
        self.widget = widget
        self.width = width


def test_body_uses_two_columns_when_wide(window):
    window._apply_responsive_layout(wide=True)

    left = window._left_column.grid_info()
    right = window._right_column.grid_info()

    assert (int(left["row"]), int(left["column"])) == (0, 0)
    assert (int(right["row"]), int(right["column"])) == (0, 1)


def test_body_stacks_when_narrow(window):
    window._apply_responsive_layout(wide=False)

    left = window._left_column.grid_info()
    right = window._right_column.grid_info()

    assert (int(left["row"]), int(left["column"])) == (0, 0)
    assert (int(right["row"]), int(right["column"])) == (1, 0)


def test_toplevel_resize_switches_layout(window):
    """阈值是逻辑像素，事件里的宽度是物理像素，必须换算后再比。

    高 DPI（缩放 1.5）下如果直接比，窗口已经窄到放不下双栏了还停在双栏。
    """
    import customtkinter as ctk

    from gui import theme

    scaling = ctk.ScalingTracker.get_widget_scaling(window) or 1.0
    threshold = theme.NARROW_WIDTH_THRESHOLD

    window._on_configure(_FakeConfigure(window, int((threshold + 200) * scaling)))
    assert window._wide is True

    window._on_configure(_FakeConfigure(window, int((threshold - 200) * scaling)))
    assert window._wide is False
    assert int(window._right_column.grid_info()["row"]) == 1


def test_configure_from_child_widget_is_ignored(window):
    """子控件的 Configure 事件不应触发重新布局（否则会无限重排）。"""
    window._apply_responsive_layout(wide=True)
    window._wide = True

    window._on_configure(_FakeConfigure(window._body, 300))

    assert window._wide is True
    assert int(window._right_column.grid_info()["column"]) == 1


def test_initial_widget_states(window):
    assert window.start_button.cget("state") == "normal"
    assert window.cancel_button.cget("state") == "disabled"
    assert window.filename_entry.get() == "video.ts"
    assert window.dir_entry.get().strip()
    assert window.overwrite_var.get() is False
    assert window.optional_a_entry.get() == ""
    assert window.optional_b_entry.get() == ""


def test_set_running_toggles_all_inputs(window):
    inputs = (
        window.url_entry,
        window.optional_a_entry,
        window.optional_b_entry,
        window.dir_entry,
        window.filename_entry,
    )

    window._set_running(True)
    assert window.start_button.cget("state") == "disabled"
    assert window.cancel_button.cget("state") == "normal"
    assert all(widget.entry.cget("state") == "disabled" for widget in inputs)

    window._set_running(False)
    assert window.start_button.cget("state") == "normal"
    assert window.cancel_button.cget("state") == "disabled"
    assert all(widget.entry.cget("state") == "normal" for widget in inputs)


def test_log_panel_keeps_line_count_bounded(window):
    from gui.components import MAX_LOG_LINES

    window.log_panel.clear()
    for index in range(MAX_LOG_LINES + 20):
        window.log_panel.append(f"第 {index} 行")

    content = window.log_panel.textbox.get("1.0", "end")
    lines = [line for line in content.splitlines() if line.strip()]

    assert len(lines) <= MAX_LOG_LINES
    assert f"第 {MAX_LOG_LINES + 19} 行" in content  # 最新内容保留
    assert "第 0 行" not in content  # 最旧内容被裁掉


def test_log_panel_clear(window):
    window.log_panel.append("测试日志")
    window.log_panel.clear()
    assert window.log_panel.textbox.get("1.0", "end").strip() == ""


def test_fonts_are_usable_for_chinese(window):
    fonts = window._fonts

    assert fonts.family
    assert fonts.body.measure("中文测试") > 0
    assert fonts.body.cget("family").lower() == fonts.family.lower()


# --------------------------------------------------------------------------
# 回归测试：消息轮询的健壮性
# --------------------------------------------------------------------------


def test_poll_survives_a_handler_exception(window):
    """单条消息处理失败不得让消息轮询停止，否则界面会永久卡死。

    这里是本文件最后一个用例：在修复之前，它会真的把共用窗口的轮询弄死，
    因此刻意放在末尾，避免连带影响其它用例。
    """
    from gui import main_window as mw

    window.log_panel.clear()

    # 毒消息：MSG_DONE 的负载不是 DownloadResult，_handle_message 会抛异常
    window._message_queue.put((mw.MSG_DONE, "这不是一个 DownloadResult"))

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        window.update()
        time.sleep(0.02)

    # 轮询若已停止，这条消息将永远不会被处理
    window._message_queue.put((mw.MSG_LOG, ("info", "轮询存活探针")))

    assert _pump(
        window,
        lambda: "轮询存活探针" in window.log_panel.textbox.get("1.0", "end"),
        timeout=3.0,
    ), "一条消息处理异常后轮询停止，界面会永久卡死"


# --------------------------------------------------------------------------
# 回归测试：日志上限、线程启动兜底、临时目录归属
# --------------------------------------------------------------------------


def test_log_panel_line_cap_holds_for_multiline_messages(window):
    """多行消息也必须受行数上限约束。

    错误提示经常带换行（例如「文件已存在」那三行），修复前 _line_count 按
    *消息条数* 计数而裁剪按 *物理行* 执行，上限会被突破数倍。
    """
    from gui.components import MAX_LOG_LINES

    window.log_panel.clear()
    multi_line = "第一行\n第二行\n第三行"
    for _ in range(MAX_LOG_LINES):
        window.log_panel.append(multi_line, "error")

    content = window.log_panel.textbox.get("1.0", "end-1c")
    lines = content.count("\n") + 1 if content else 0

    assert lines <= MAX_LOG_LINES, f"多行消息突破了行数上限：{lines} > {MAX_LOG_LINES}"


def test_start_recovers_when_worker_thread_cannot_start(window, monkeypatch, tmp_path):
    """线程创建/启动失败不能让按钮永久卡在禁用状态。"""
    from gui import main_window as mw

    def boom(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(mw.threading, "Thread", boom)
    _prepare(window, tmp_path)

    window._on_start()

    assert not window._is_running()
    assert window.start_button.cget("state") == "normal"
    assert window.cancel_button.cget("state") == "disabled"
    assert window.url_entry.entry.cget("state") == "normal"
    assert "无法启动" in window.log_panel.textbox.get("1.0", "end")


def test_worker_uses_the_window_owned_temp_directory(window, monkeypatch, tmp_path):
    """关闭续传时，后台任务用窗口持有的临时目录，且它建在**输出目录**下。

    两个理由：任务线程被强杀时核心的清理来不及执行，得由窗口兜底删除；而它
    不能落在系统临时目录里——分片可能有几个 GB，写进系统盘会把它撑爆。
    """
    from gui import main_window as mw
    from pathlib import Path

    captured: dict = {}

    def fake(**kwargs):
        captured.update(kwargs)
        path = Path(kwargs["temp_dir"])
        captured["existed"] = path.is_dir()
        captured["parent"] = path.parent
        raise HLSDownloaderError("测试结束")

    monkeypatch.setattr(mw, "download_hls_video", fake)
    _prepare(window, tmp_path)
    window.resume_var.set(False)

    window._on_start()
    assert _pump(window, lambda: not window._is_running())
    # 清理发生在主线程处理「任务失败」消息时，比线程结束稍晚一点
    assert _pump(window, lambda: window._temp_root is None), "任务结束后没有清理临时目录"

    assert captured.get("temp_dir"), "没有把临时目录交给核心"
    assert captured["existed"] is True, "临时目录应当在调用前就建好"
    assert captured["parent"] == tmp_path, "临时目录必须挨着输出文件，不能占系统盘"
    assert not Path(captured["temp_dir"]).exists(), "输出目录里不该留下空目录"


def test_worker_lets_the_core_place_resume_segments(window, monkeypatch, tmp_path):
    """开启续传（默认）时不再使用会话临时目录。

    会话目录在关闭窗口时会被删除，放在里面的分片根本留不到下次运行，
    续传也就无从谈起。
    """
    from gui import main_window as mw

    captured: dict = {}

    def fake(**kwargs):
        captured.update(kwargs)
        raise HLSDownloaderError("测试结束")

    monkeypatch.setattr(mw, "download_hls_video", fake)
    _prepare(window, tmp_path)
    assert window.resume_var.get() is True

    window._on_start()
    assert _pump(window, lambda: not window._is_running())

    assert captured.get("resume") is True
    assert captured.get("temp_dir") is None
    assert captured.get("workers") == mw.DEFAULT_WORKERS


# --------------------------------------------------------------------------
# 在线播放
# --------------------------------------------------------------------------


class _FakePlayerProcess:
    """假播放器进程：只实现主窗口用到的那些方法。"""

    def __init__(
        self,
        *,
        alive: bool = True,
        exit_code: int = 0,
        hint: str = "",
        elapsed: float = 60.0,
        name: str = "mpv",
        supports_control: bool = True,
        position: float | None = None,
        duration: float | None = None,
        ended_with_error: bool = False,
    ) -> None:
        self.alive = alive
        self.exit_code = exit_code
        self.hint = hint
        self.elapsed = elapsed
        self.name = name
        self.supports_control = supports_control
        self.position_value = position
        self.duration_value = duration
        self.error_flag = ended_with_error
        self.terminated = False
        self.seeks: list[float] = []
        self.speeds: list[float] = []

    def poll(self):
        return None if self.alive else self.exit_code

    def failure_hint(self) -> str:
        return self.hint

    def elapsed_seconds(self) -> float:
        return self.elapsed

    def position(self):
        return self.position_value

    def duration(self):
        return self.duration_value

    def ended_with_error(self) -> bool:
        return self.error_flag

    def seek(self, seconds: float) -> bool:
        if not self.supports_control:
            return False
        self.seeks.append(seconds)
        self.position_value = seconds
        return True

    def set_speed(self, value: float) -> bool:
        if not self.supports_control:
            return False
        self.speeds.append(value)
        return True

    def close(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.alive = False


def _install_playback(window, monkeypatch, process) -> None:
    """把在线播放链路整体换成替身：不发请求、不启动真实播放器。"""
    from gui import main_window as mw

    monkeypatch.setattr(mw, "find_mpv", lambda: "mpv.exe")
    monkeypatch.setattr(mw, "find_ffplay", lambda: "ffplay.exe")
    monkeypatch.setattr(mw, "inspect_playlist", lambda *args, **kwargs: _playback_info())
    monkeypatch.setattr(mw, "launch_playback", lambda url, **kwargs: process)
    window.url_entry.set("https://cdn.test/video/index.m3u8")


def _playback_info():
    """预检结果的替身。"""
    from hls_downloader.playback import PlaybackInfo

    return PlaybackInfo(
        url="https://cdn.test/signed/index.m3u8",
        segment_count=3,
        total_duration=12.0,
    )


def test_playback_plays_and_returns_to_idle_when_the_player_exits(window, monkeypatch):
    process = _FakePlayerProcess(alive=True)
    _install_playback(window, monkeypatch, process)

    window._on_playback()

    assert _pump(window, lambda: "正在播放" in window.playback_panel._info_label.cget("text"))
    assert "3 个分片" in window.playback_panel._info_label.cget("text")
    assert window.playback_panel.play_button.cget("state") == "disabled"
    assert window.playback_panel.stop_button.cget("state") == "normal"

    # 用户自己把播放器窗口关掉了
    process.alive = False

    assert _pump(window, lambda: "已结束" in window.playback_panel._info_label.cget("text"))
    assert window.playback_panel.play_button.cget("state") == "normal"
    assert window.playback_panel.stop_button.cget("state") == "disabled"


def test_playback_reports_why_the_player_gave_up(window, monkeypatch):
    """播放器刚起来就退出时，必须把它的原因摆到日志里。

    「点了播放却什么都没发生」曾经是完全静默的：播放器输出被丢弃，用户
    除了「窗口没出现」之外拿不到任何信息。
    """
    process = _FakePlayerProcess(
        alive=False,
        exit_code=1,
        elapsed=0.4,
        hint="https://cdn.test/video/index.m3u8: Server returned 403 Forbidden",
    )
    _install_playback(window, monkeypatch, process)

    window._on_playback()

    assert _pump(
        window, lambda: "403 Forbidden" in window.log_panel.textbox.get("1.0", "end")
    )
    assert "返回码 1" in window.log_panel.textbox.get("1.0", "end")
    assert "未能开始播放" in window.playback_panel._info_label.cget("text")
    assert window.playback_panel.play_button.cget("state") == "normal"


def test_playback_treats_a_fast_zero_exit_with_errors_as_failure(window, monkeypatch):
    """ffplay 遇到 404 这类致命错误也是以 0 退出的，不能只看返回码。"""
    process = _FakePlayerProcess(
        alive=False,
        exit_code=0,
        elapsed=0.3,
        hint="http://cdn.test/index.m3u8: Server returned 404 Not Found",
    )
    _install_playback(window, monkeypatch, process)

    window._on_playback()

    assert _pump(
        window, lambda: "404 Not Found" in window.log_panel.textbox.get("1.0", "end")
    )
    assert "未能开始播放" in window.playback_panel._info_label.cget("text")


def test_playback_reports_a_silent_player_failure(window, monkeypatch):
    """播放器连输出都没有时，也要给出下一步能做什么。"""
    process = _FakePlayerProcess(alive=False, exit_code=1, elapsed=0.2, hint="")
    _install_playback(window, monkeypatch, process)

    window._on_playback()

    assert _pump(
        window,
        lambda: "播放器没有给出错误详情" in window.log_panel.textbox.get("1.0", "end"),
    )
    assert "未能开始播放" in window.playback_panel._info_label.cget("text")


def test_playback_reports_a_clean_end_as_success(window, monkeypatch):
    process = _FakePlayerProcess(alive=False, exit_code=0, elapsed=65.0)
    _install_playback(window, monkeypatch, process)

    window._on_playback()

    assert _pump(
        window, lambda: "已结束播放" in window.log_panel.textbox.get("1.0", "end")
    )
    assert "播放已结束" in window.playback_panel._info_label.cget("text")
    assert window.playback_panel.play_button.cget("state") == "normal"


def test_playback_reports_an_error_after_a_long_playback(window, monkeypatch):
    """播了很久才出的错不该被说成「没能开始播放」。"""
    process = _FakePlayerProcess(
        alive=False,
        exit_code=0,
        elapsed=120.0,
        hint="https://cdn.test/s.m3u8: Invalid data found when processing input",
    )
    _install_playback(window, monkeypatch, process)

    window._on_playback()

    assert _pump(
        window,
        lambda: "Invalid data found" in window.log_panel.textbox.get("1.0", "end"),
    )
    assert "播放已结束" in window.playback_panel._info_label.cget("text")


def test_playback_shows_the_read_only_progress_text(window, monkeypatch):
    """位置由播放器推来，面板只作展示——控制在播放器窗口里。"""
    process = _FakePlayerProcess(
        alive=True, position=0.0, duration=300.0, name="mpv", supports_control=True
    )
    _install_playback(window, monkeypatch, process)

    window._on_playback()
    assert _pump(window, lambda: "正在播放" in window.playback_panel._info_label.cget("text"))

    process.position_value = 150.0
    assert _pump(window, lambda: "2:30" in window.playback_panel._time_label.cget("text"))

    text = window.playback_panel._time_label.cget("text")
    assert "已播放 2:30 / 5:00" == text


def test_playback_points_at_the_player_window_for_seeking(window, monkeypatch):
    """进度条留在播放器窗口里，面板不再摆一套——同一个功能放两处只会让人迷糊。"""
    process = _FakePlayerProcess(alive=True, name="mpv", supports_control=True)
    _install_playback(window, monkeypatch, process)

    window._on_playback()
    assert _pump(window, lambda: bool(window.playback_panel._hint_label.cget("text")))

    hint = window.playback_panel._hint_label.cget("text")
    assert "播放器窗口" in hint
    assert "进度条" in hint
    assert not hasattr(window.playback_panel, "_position_slider")


def test_playback_speed_buttons_step_through_the_choices(window, monkeypatch):
    """倍速必须由我们给按钮：mpv 窗口里没有倍速控件。"""
    process = _FakePlayerProcess(alive=True, supports_control=True)
    _install_playback(window, monkeypatch, process)

    window._on_playback()
    assert _pump(window, lambda: window.playback_panel._faster_button.cget("state") == "normal")
    assert window.playback_panel._speed_label.cget("text") == "1×"

    window.playback_panel._faster_button.invoke()
    window.playback_panel._faster_button.invoke()

    assert process.speeds == [1.25, 1.5]
    assert window.playback_panel._speed_label.cget("text") == "1.5×"
    assert window._playback_speed == 1.5


def test_playback_speed_buttons_stop_at_both_ends(window, monkeypatch):
    process = _FakePlayerProcess(alive=True, supports_control=True)
    _install_playback(window, monkeypatch, process)

    window._on_playback()
    assert _pump(window, lambda: window.playback_panel._faster_button.cget("state") == "normal")

    # 往下走到底：0.5× 时减速按钮应当灰掉
    for _ in range(6):
        window.playback_panel._slower_button.invoke()
    assert window.playback_panel._speed_label.cget("text") == "0.5×"
    assert window.playback_panel._slower_button.cget("state") == "disabled"
    assert process.speeds[-1] == 0.5

    # 往上走到底：2× 时加速按钮应当灰掉
    for _ in range(10):
        window.playback_panel._faster_button.invoke()
    assert window.playback_panel._speed_label.cget("text") == "2×"
    assert window.playback_panel._faster_button.cget("state") == "disabled"
    assert process.speeds[-1] == 2.0


def test_playback_speed_is_remembered_for_the_next_play(window, monkeypatch):
    from gui import main_window as mw

    launched: list[dict] = []
    process = _FakePlayerProcess(alive=True, supports_control=True)
    monkeypatch.setattr(mw, "find_mpv", lambda: "mpv.exe")
    monkeypatch.setattr(mw, "inspect_playlist", lambda *a, **k: _playback_info())
    monkeypatch.setattr(
        mw, "launch_playback", lambda url, **kwargs: launched.append(kwargs) or process
    )
    window.url_entry.set("https://cdn.test/video/index.m3u8")

    window._on_playback()
    assert _pump(window, lambda: window.playback_panel._faster_button.cget("state") == "normal")
    window.playback_panel._faster_button.invoke()  # 1.25×
    window._on_playback_stop()
    assert _pump(window, lambda: not window._is_playing())

    window._on_playback()
    assert _pump(window, lambda: len(launched) == 2)

    assert launched[0] == {"speed": 1.0}
    assert launched[1] == {"speed": 1.25}


def test_playback_speed_buttons_are_disabled_without_a_control_channel(window, monkeypatch):
    process = _FakePlayerProcess(alive=True, name="ffplay", supports_control=False)
    _install_playback(window, monkeypatch, process)

    window._on_playback()
    assert _pump(window, lambda: "正在播放" in window.playback_panel._info_label.cget("text"))

    assert window.playback_panel._slower_button.cget("state") == "disabled"
    assert window.playback_panel._faster_button.cget("state") == "disabled"
    # 就算被绕过调用，也不会打到播放器上
    window.playback_panel._step_speed(1)
    assert process.speeds == []


def test_playback_tells_ffplay_users_how_to_get_controls(window, monkeypatch):
    """ffplay 窗口里既没有进度条也没有调速，提示必须换成「装 mpv」。"""
    process = _FakePlayerProcess(alive=True, name="ffplay", supports_control=False)
    _install_playback(window, monkeypatch, process)

    window._on_playback()
    assert _pump(window, lambda: bool(window.playback_panel._hint_label.cget("text")))

    hint = window.playback_panel._hint_label.cget("text")
    assert "mpv" in hint
    assert "没有进度条" in hint
    # 报不出位置的播放器，那行进度文字保持为空
    assert window.playback_panel._time_label.cget("text") == ""


def test_copy_playlist_url_puts_the_address_on_the_clipboard(window, monkeypatch):
    from gui import main_window as mw

    monkeypatch.setattr(
        mw, "prepare_playlist_url", lambda url, a, b: "https://cdn.test/signed/index.m3u8"
    )
    window.url_entry.set("https://cdn.test/video/index.m3u8")

    window._on_copy_playlist_url()

    assert window.clipboard_get() == "https://cdn.test/signed/index.m3u8"
    assert "已复制播放地址" in window.log_panel.textbox.get("1.0", "end")


def test_copy_playlist_url_goes_through_the_shared_url_processing(window, monkeypatch):
    """复制出来的地址必须和下载、在线播放算的是同一个。"""
    from gui import main_window as mw

    calls: list[tuple] = []
    monkeypatch.setattr(
        mw, "prepare_playlist_url", lambda url, a, b: calls.append((url, a, b)) or "signed"
    )
    window.url_entry.set("https://cdn.test/video/index.m3u8")
    window.optional_a_entry.set("secret")
    window.optional_b_entry.set("https://cdn.test")

    window._on_copy_playlist_url()

    assert calls == [("https://cdn.test/video/index.m3u8", "secret", "https://cdn.test")]


def test_copy_playlist_url_reports_a_bad_link(window, monkeypatch):
    from gui import main_window as mw
    from hls_downloader.models import InvalidUrlError

    def boom(url, a, b):
        raise InvalidUrlError("链接无效。")

    monkeypatch.setattr(mw, "prepare_playlist_url", boom)
    window.url_entry.set("不是链接")

    window._on_copy_playlist_url()

    assert "无法生成播放地址" in window.log_panel.textbox.get("1.0", "end")


def test_playback_reports_a_missing_player(window, monkeypatch):
    """mpv 与 ffplay 都没有时才拦下来——有 mpv 就该让 mpv 上。"""
    from gui import main_window as mw

    monkeypatch.setattr(mw, "find_mpv", lambda: None)
    monkeypatch.setattr(mw, "find_ffplay", lambda: None)
    window.url_entry.set("https://cdn.test/video/index.m3u8")

    window._on_playback()

    assert "mpv" in window.playback_panel._info_label.cget("text")
    assert window.playback_panel.play_button.cget("state") == "normal"
    assert "ffplay" in window.log_panel.textbox.get("1.0", "end")


def test_playback_reports_an_inspection_failure(window, monkeypatch):
    from gui import main_window as mw
    from hls_downloader.models import PlaylistFetchError

    def boom(*args, **kwargs):
        raise PlaylistFetchError("获取播放列表失败：服务器返回 HTTP 404。")

    monkeypatch.setattr(mw, "find_ffplay", lambda: "ffplay.exe")
    monkeypatch.setattr(mw, "inspect_playlist", boom)
    window.url_entry.set("https://cdn.test/video/index.m3u8")

    window._on_playback()

    assert _pump(window, lambda: "404" in window.playback_panel._info_label.cget("text"))
    assert window.playback_panel.play_button.cget("state") == "normal"
    assert window.playback_panel.stop_button.cget("state") == "disabled"


def test_playback_stop_terminates_the_player(window, monkeypatch):
    process = _FakePlayerProcess(alive=True)
    _install_playback(window, monkeypatch, process)

    window._on_playback()
    # 必须等到播放器真的起来了：预检阶段停止按钮同样是可用的
    assert _pump(window, lambda: "正在播放" in window.playback_panel._info_label.cget("text"))

    window._on_playback_stop()

    assert "已结束" in window.playback_panel._info_label.cget("text")
    assert window.playback_panel.play_button.cget("state") == "normal"
    # 结束进程放在子线程里做，等它跑完
    assert _pump(window, lambda: process.terminated)


def test_playback_ignores_a_late_result_from_a_stopped_run(window, monkeypatch):
    """停止后立刻重播时，上一次在线播放迟到的结果不能覆盖新状态。"""
    from gui import main_window as mw
    from hls_downloader.playback import PlaybackInfo

    process = _FakePlayerProcess(alive=True)
    _install_playback(window, monkeypatch, process)

    window._on_playback()
    stale_generation = window._playback_generation - 1
    window._on_playback_stop()

    window._message_queue.put(
        (
            mw.MSG_PLAYBACK_READY,
            (stale_generation, PlaybackInfo(url="https://cdn.test/old/index.m3u8")),
        )
    )
    assert _pump(window, lambda: window._message_queue.empty())

    text = window.playback_panel._info_label.cget("text")
    assert "正在播放" not in text


def test_playback_stays_available_while_a_download_runs(window, monkeypatch, tmp_path):
    """在线播放与下载互不阻塞：下载跑着的时候播放按钮照样能点。"""
    from gui import main_window as mw

    release = threading.Event()

    def slow(**kwargs):
        release.wait(5)
        raise HLSDownloaderError("测试结束")

    monkeypatch.setattr(mw, "download_hls_video", slow)
    _prepare(window, tmp_path)

    window._on_start()
    assert _pump(window, lambda: window.start_button.cget("state") == "disabled")

    assert window.playback_panel.play_button.cget("state") == "normal"

    release.set()
    assert _pump(window, lambda: not window._is_running())


def test_closing_the_window_stops_the_playback(window, monkeypatch):
    """播放器是独立进程，关窗时必须主动结束，否则会留在桌面上。"""
    process = _FakePlayerProcess(alive=True)
    _install_playback(window, monkeypatch, process)

    window._on_playback()
    assert _pump(window, lambda: "正在播放" in window.playback_panel._info_label.cget("text"))

    window._stop_playback_on_close()

    assert process.terminated is True
