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

import time

import pytest

from hls_downloader.models import DownloadCancelled, HLSDownloaderError

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
    """关闭续传时，后台任务必须使用窗口自己持有的临时根目录。

    否则任务线程被强杀时，核心的清理逻辑来不及执行，临时分片会永久残留在
    系统临时目录里——窗口自己持有才可能在关闭时兜底清理。
    """
    from gui import main_window as mw

    captured: dict = {}

    def fake(**kwargs):
        captured.update(kwargs)
        raise HLSDownloaderError("测试结束")

    monkeypatch.setattr(mw, "download_hls_video", fake)
    _prepare(window, tmp_path)
    window.resume_var.set(False)

    window._on_start()
    assert _pump(window, lambda: not window._is_running())

    assert window._temp_root is not None, "窗口没有持有临时根目录"
    assert captured.get("temp_dir") == str(window._temp_root)
    assert window._temp_root.is_dir(), "临时根目录应当真实存在"


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
