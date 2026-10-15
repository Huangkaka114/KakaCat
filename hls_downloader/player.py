"""在线播放用的外部播放器：找到播放器、启动它，并把控制权拿在手里。

在线播放与下载完全解耦——本模块只启动一个外部播放器进程，把播放列表地址
交给它，由播放器自己去取分片边下边播。程序自己不落盘：不建临时目录、不写
分片、不占输出目录，也不参与解密（标准整段 AES-128 由播放器依据播放列表
自己声明的密钥地址完成）。

支持两个播放器，能力不同：

* **mpv**（优先）：播放窗口自带可拖动的进度条（OSC）与倍速快捷键，**播放控制
  就在窗口里完成**；本程序再通过 ``--input-ipc-server`` 的 JSON IPC 读到位置与
  时长用于展示、并拿到 ``log-message`` / ``end-file`` 给出的失败原因。
* **ffplay**（回退）：随 FFmpeg 一起安装，几乎人人都有，但它既没有窗口内的
  进度条，也没有任何控制通道——位置只能靠解析 stderr、倍速只能在启动时用滤镜
  设定。因此回退时界面上会说明「装 mpv 才有进度条与倍速」。

两者的输出都可能带着**签名的完整地址**，所以一律只收进内存、不落盘，展示前
把地址脱敏。播放器报的错也不能丢：「点了播放却什么都没发生」时它是唯一的线索。
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Iterator

from .app_paths import executable_name, find_bundled_tool
from .models import HLSDownloaderError
from .url_utils import redact_url

#: ffplay 可执行文件名。它和 FFmpeg 同源，通常在同一个 bin 目录里。
FFPLAY_EXECUTABLE = "ffplay"

#: mpv 可执行文件名。它提供 IPC 控制通道，因此优先使用。
MPV_EXECUTABLE = "mpv"

#: 停止播放时先请求正常退出，超过这个时间再强杀（秒）
STOP_TIMEOUT_SECONDS = 2.0

#: 最多保留多少行播放器输出
STDERR_KEEP_LINES = 40

#: 播放器退出后等读线程收尾的时间（秒）。管道已到 EOF，正常几乎立即返回。
STDERR_DRAIN_TIMEOUT_SECONDS = 0.3

#: 连接 mpv IPC 端点的最长等待时间（秒）。mpv 启动到建好管道要一点时间。
MPV_IPC_CONNECT_TIMEOUT_SECONDS = 5.0

#: 等 IPC 通道来数据的单次时长（秒）。收不到就回头再看一次，不要在阻塞读里
#: 干等——见 :class:`_IpcTransport` 的说明。
IPC_WAIT_TIMEOUT_SECONDS = 0.1

#: mpv 允许的倍速范围
MPV_SPEED_MIN = 0.01
MPV_SPEED_MAX = 100.0

#: 界面上提供的倍速档位
SPEED_CHOICES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

#: mpv 的常见安装位置：环境变量名 + 目录层级（不含文件名）。只在 PATH 找不到时才用。
_MPV_KNOWN_LOCATIONS = (
    ("ProgramFiles", "MPV Player"),
    ("ProgramFiles(x86)", "MPV Player"),
    ("LOCALAPPDATA", "Programs", "mpv"),
    ("ProgramFiles", "mpv"),
)

#: 我们通过 IPC 观察的 mpv 属性 → 观察 id
_MPV_OBSERVED_PROPERTIES = {"time-pos": 1, "duration": 2}

#: 播放器自己结束的原因里，哪些算「正常播完」
_CLEAN_END_REASONS = frozenset({"eof", "quit", "stop", "redirect"})

#: 匹配一行播放器输出里的绝对地址，用于脱敏
_ABSOLUTE_URL_PATTERN = re.compile(r"https?://[^\s\"'<>()]+")

if os.name == "nt":
    # Windows 的命名管道要先问「有没有数据」再读，原因见 _IpcTransport。
    import ctypes
    import ctypes.wintypes as _wintypes
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PeekNamedPipe = _kernel32.PeekNamedPipe
    _PeekNamedPipe.argtypes = [
        _wintypes.HANDLE,
        ctypes.c_void_p,
        _wintypes.DWORD,
        ctypes.POINTER(_wintypes.DWORD),
        ctypes.POINTER(_wintypes.DWORD),
        ctypes.POINTER(_wintypes.DWORD),
    ]
    _PeekNamedPipe.restype = _wintypes.BOOL
else:  # pragma: no cover - 目标平台是 Windows
    _PeekNamedPipe = None


def _pending_bytes(handle: int) -> int | None:
    """这个管道里还有多少字节可读；管道已断返回 ``None``。"""
    if _PeekNamedPipe is None:  # pragma: no cover - 目标平台是 Windows
        return None
    available = _wintypes.DWORD(0)
    ok = _PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
    return int(available.value) if ok else None


class PlayerError(HLSDownloaderError):
    """在线播放相关的错误。"""


class PlayerNotFoundError(PlayerError):
    """找不到可用的外部播放器可执行文件。"""


# --------------------------------------------------------------------------
# 查找可执行文件
# --------------------------------------------------------------------------


def find_ffplay() -> str | None:
    """查找 ffplay：先看随程序分发的，再查 PATH，都没有返回 ``None``。

    与 :func:`hls_downloader.merger.find_ffmpeg` 同样的策略：先看程序目录下
    随包分发的 ``bin\\``，再查 PATH，不硬编码其它路径；找不到就由调用方给出
    安装提示。
    """
    bundled = find_bundled_tool(FFPLAY_EXECUTABLE)
    if bundled is not None:
        return str(bundled)
    return shutil.which(FFPLAY_EXECUTABLE)


def find_mpv() -> str | None:
    """查找 mpv：随包分发的 → PATH → 几个标准安装位置。

    只看 PATH 是不够的——实测用 ``winget install shinchiro.mpv`` 装完后
    ``C:\\Program Files\\MPV Player\\mpv.exe`` 就位，却**不会**被加进 PATH，
    于是用户装好了、程序却找不到。所以按「随包的 → PATH → 常见位置」依次找。
    """
    bundled = find_bundled_tool(MPV_EXECUTABLE)
    if bundled is not None:
        return str(bundled)

    found = shutil.which(MPV_EXECUTABLE)
    if found:
        return found

    filename = executable_name(MPV_EXECUTABLE)
    for parts in _MPV_KNOWN_LOCATIONS:
        root = os.environ.get(parts[0])
        if not root:
            continue
        candidate = Path(root).joinpath(*parts[1:], filename)
        if candidate.is_file():
            return str(candidate)
    return None


def _redact_urls(text: str) -> str:
    """把一行播放器输出里的绝对地址换成脱敏形式。"""

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        # 句尾的冒号/逗号不属于地址本身：播放器常写成「地址: 错误原因」
        trimmed = raw.rstrip(":,.;")
        return redact_url(trimmed) + raw[len(trimmed) :]

    return _ABSOLUTE_URL_PATTERN.sub(replace, text)


# --------------------------------------------------------------------------
# 播放器句柄
# --------------------------------------------------------------------------


class PlayerHandle:
    """播放器进程句柄。

    转发 ``subprocess.Popen`` 上会用到的几个方法（所以 :func:`stop_playback`
    可以直接接收它），并给出一套「控制播放」的接口。基类默认表示**不支持**
    精细控制（ffplay 就是如此）；:class:`MpvProcess` 会真正实现它们。
    """

    #: 供界面与日志展示的名字
    name = "播放器"

    #: 是否支持 seek / 倍速等精细控制
    supports_control = False

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process
        self._started_at = time.monotonic()

    # -- 转发给底层进程，接口与 subprocess.Popen 对齐 --------------------
    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def poll(self):
        return self._process.poll()

    def wait(self, timeout=None):
        return self._process.wait(timeout)

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def elapsed_seconds(self) -> float:
        """已经运行了多久。刚起来就退出往往意味着它根本没播起来。"""
        return time.monotonic() - self._started_at

    # -- 精细控制：基类一律不支持 ----------------------------------------
    def position(self) -> float | None:
        """当前播放位置（秒）；取不到时为 ``None``。"""
        return None

    def duration(self) -> float | None:
        """总时长（秒）；直播或取不到时为 ``None``。"""
        return None

    def end_reason(self) -> str | None:
        """播放器自己结束的原因；仍在播放或取不到时为 ``None``。"""
        return None

    def ended_with_error(self) -> bool:
        """播放器是否明确报告了错误（而不是正常播完）。"""
        return False

    def set_speed(self, value: float) -> bool:
        """设置倍速；不支持时返回 ``False``。"""
        return False

    def seek(self, seconds: float) -> bool:
        """跳到指定位置（秒）；不支持时返回 ``False``。"""
        return False

    def failure_hint(self) -> str:
        """播放器退出原因的可展示文本（已脱敏）；没有则返回空串。"""
        return ""


class FfplayProcess(PlayerHandle):
    """ffplay 句柄：转发进程方法，并把 stderr 的最后若干行收在内存里。

    ffplay 没有控制通道，只能看它说了什么。
    """

    name = "ffplay"

    def __init__(self, process: subprocess.Popen) -> None:
        super().__init__(process)
        self._tail: deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        # 读线程在写、主线程在读，必须加锁：deque 在迭代期间被追加会抛
        # RuntimeError，而"播放器刚起来就退出"正是最需要这几行的时刻。
        self._lock = threading.Lock()
        self._reader = threading.Thread(
            target=self._drain_stderr, name="ffplay-stderr", daemon=True
        )
        self._reader.start()

    def _drain_stderr(self) -> None:
        """持续读走 stderr。不读的话管道写满会把播放器卡死。"""
        stream = self._process.stderr
        if stream is None:
            return
        try:
            for raw in stream:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    with self._lock:
                        self._tail.append(line)
        except (OSError, ValueError):  # pragma: no cover - 进程被强杀时管道会断
            pass

    def failure_hint(self) -> str:
        """播放器自己退出后，用它的输出拼一段可展示的说明（已脱敏）。

        没抓到输出时返回空串，此时至少还有返回码可用。
        """
        # 进程已退出，管道已到 EOF，读线程马上就收尾；先等它一下，否则最后
        # 那几行（往往正是错误原因）会读不到。
        self._reader.join(timeout=STDERR_DRAIN_TIMEOUT_SECONDS)
        with self._lock:
            lines = list(self._tail)
        return "\n".join(_redact_urls(line) for line in lines)


class _IpcTransport:
    """mpv 的 JSON IPC 通道：写一行是一条命令，读一行是一条回复或事件。

    用**无缓冲**的原始流，自己按 ``\\n`` 切行（有缓冲的读写流在同一个对象上
    交替读写有坑）。

    **读不能阻塞**，这是 Windows 上的硬性要求：命名管道的句柄是同步的，一个
    线程停在阻塞 ``ReadFile`` 上时，同一个句柄上的 ``WriteFile`` 也会被一起
    挡住——读线程一挂住，seek 和倍速就永远发不出去。因此这里先用
    ``PeekNamedPipe`` 问「有没有数据」，有才读。POSIX 的套接字没有这个问题
    （读写是两个独立方向），那边直接阻塞读即可。
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self._buffer = bytearray()
        self._handle: int | None = None
        if _PeekNamedPipe is not None:
            try:
                self._handle = msvcrt.get_osfhandle(stream.fileno())
            except (OSError, ValueError, AttributeError):  # pragma: no cover
                self._handle = None

    def send(self, payload: dict) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._stream.write(data)

    def poll_lines(self) -> list[str] | None:
        """读一次通道，返回其中**完整**的行。

        返回 ``[]`` 表示这次没读到完整的一行；返回 ``None`` 表示通道已关闭。
        两种情况都不会长时间阻塞。
        """
        if not self._wait_for_data():
            return []

        chunk = self._stream.read(65536)
        if not chunk:
            return None
        self._buffer.extend(chunk)

        lines: list[str] = []
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                break
            lines.append(bytes(self._buffer[:index]).decode("utf-8", "replace").strip())
            del self._buffer[: index + 1]
        return lines

    def _wait_for_data(self) -> bool:
        """等到有数据可读（最多 :data:`IPC_WAIT_TIMEOUT_SECONDS`）。

        非 Windows 直接返回 ``True``：那边阻塞读是安全的，也就不用轮询。
        """
        if self._handle is None:
            return True

        deadline = time.monotonic() + IPC_WAIT_TIMEOUT_SECONDS
        while True:
            pending = _pending_bytes(self._handle)
            if pending is None:
                # 管道断了：让调用方去读到 EOF
                return True
            if pending > 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError:  # pragma: no cover - 通道可能已经断了
            pass


def _default_ipc_path() -> str:
    """给这一次播放生成一个唯一的 IPC 端点路径。"""
    token = uuid.uuid4().hex[:12]
    if os.name == "nt":
        # Windows 用命名管道
        return rf"\\.\pipe\kakacat-mpv-{token}"
    return str(Path(tempfile.gettempdir()) / f"kakacat-mpv-{token}.sock")


def _connect_ipc(path: str, timeout: float) -> _IpcTransport | None:
    """连接 mpv 的 IPC 端点，连不上返回 ``None``。

    mpv 从启动到把管道建好有一小段延迟，所以要重试而不是试一次就放弃。
    连不上不影响播放，只是拿不到进度条和倍速。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _IpcTransport(open(path, "rb+", buffering=0))
        except OSError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)


class MpvProcess(PlayerHandle):
    """mpv 句柄：进程方法之外，还通过 JSON IPC 提供真正的播放控制。

    位置与时长由 ``observe_property`` 事件推送；seek 与倍速是即时命令，改倍速
    不需要重启进程；出错原因由 ``log-message`` 事件给出——因此不需要解析
    stderr，也不需要为了换倍速重新起一个播放器。
    """

    name = "mpv"

    @property
    def supports_control(self) -> bool:
        """只有真的连上了 IPC 才算受控。

        不能写成类属性 ``True``：连不上通道时界面会启用一个按下去毫无反应的
        进度条，而 seek / 倍速其实全都返回 ``False``。
        """
        return self._transport is not None

    def __init__(self, process: subprocess.Popen, transport: _IpcTransport | None) -> None:
        super().__init__(process)
        self._transport = transport
        self._lock = threading.Lock()
        self._position: float | None = None
        self._duration: float | None = None
        self._end_reason: str | None = None
        self._errors: deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        self._request_ids: Iterator[int] = itertools.count(1)
        self._reader: threading.Thread | None = None

        if transport is not None:
            self._subscribe()
            self._reader = threading.Thread(
                target=self._read_events, name="mpv-ipc", daemon=True
            )
            self._reader.start()

    # -- 订阅 ------------------------------------------------------------

    def _subscribe(self) -> None:
        """让 mpv 把位置、时长的变化和错误日志主动推给我们。"""
        for name, observe_id in _MPV_OBSERVED_PROPERTIES.items():
            self._command(["observe_property", observe_id, name])
        # 只收 error 级别：那才是「为什么播不了」的答案
        self._command(["request_log_messages", "error"])

    def _command(self, command: list) -> bool:
        transport = self._transport
        if transport is None:
            return False
        try:
            transport.send({"command": command, "request_id": next(self._request_ids)})
        except (OSError, ValueError):
            return False
        return True

    # -- 事件循环 --------------------------------------------------------

    def _read_events(self) -> None:
        transport = self._transport
        if transport is None:  # pragma: no cover - 只有连上了才会起这个线程
            return
        while True:
            try:
                lines = transport.poll_lines()
            except (OSError, ValueError):  # pragma: no cover - 通道被切断
                return
            if lines is None:
                return  # 通道关闭，收工
            for line in lines:
                self._dispatch_line(line)

    def _dispatch_line(self, line: str) -> None:
        if not line:
            return
        try:
            message = json.loads(line)
        except ValueError:  # pragma: no cover - mpv 只会发合法 JSON
            return
        if isinstance(message, dict):
            self._handle_message(message)

    def _handle_message(self, message: dict) -> None:
        event = message.get("event")
        if event == "property-change":
            self._handle_property(message.get("name"), message.get("data"))
        elif event == "end-file":
            reason = str(message.get("reason") or "unknown")
            with self._lock:
                self._end_reason = reason
        elif event == "log-message":
            self._handle_log(message)

    def _handle_property(self, name: object, data: object) -> None:
        if not isinstance(data, (int, float)):
            return
        with self._lock:
            if name == "time-pos":
                self._position = float(data)
            elif name == "duration":
                self._duration = float(data)

    def _handle_log(self, message: dict) -> None:
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        prefix = message.get("prefix")
        line = f"{prefix}: {text.strip()}" if prefix else text.strip()
        with self._lock:
            self._errors.append(line)

    # -- 控制 ------------------------------------------------------------

    def position(self) -> float | None:
        with self._lock:
            return self._position

    def duration(self) -> float | None:
        with self._lock:
            return self._duration

    def end_reason(self) -> str | None:
        with self._lock:
            return self._end_reason

    def ended_with_error(self) -> bool:
        reason = self.end_reason()
        if reason is not None and reason not in _CLEAN_END_REASONS:
            return True
        with self._lock:
            return bool(self._errors)

    def set_speed(self, value: float) -> bool:
        value = float(value)
        if not MPV_SPEED_MIN <= value <= MPV_SPEED_MAX:
            return False
        return self._command(["set_property", "speed", value])

    def seek(self, seconds: float) -> bool:
        return self._command(["seek", max(0.0, float(seconds)), "absolute"])

    def failure_hint(self) -> str:
        """把 mpv 报的错误拼成可展示的文本（已脱敏）。"""
        with self._lock:
            lines = list(self._errors)
        return "\n".join(_redact_urls(line) for line in lines)

    def close(self) -> None:
        """断开 IPC 通道。停止播放时调用，避免读线程挂在管道上。"""
        transport = self._transport
        self._transport = None
        if transport is not None:
            transport.close()


# --------------------------------------------------------------------------
# 启动播放器
# --------------------------------------------------------------------------


def _popen(argv: list[str], *, stderr: object) -> subprocess.Popen:
    """以不弹控制台窗口、不经过 shell 的方式启动播放器。"""
    options: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": stderr,
    }
    if os.name == "nt":
        # 不要给播放器另外开一个控制台窗口
        options["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        return subprocess.Popen(argv, **options)  # noqa: S603 - 列表传参，不经 shell
    except OSError as exc:
        raise PlayerError(f"无法启动播放器：{exc}") from exc


def launch_ffplay(
    url: str,
    *,
    ffplay_path: str | None = None,
    title: str = "HLS 在线播放",
) -> FfplayProcess:
    """用 ffplay 播放 ``url``。

    Args:
        url: 交给播放器的地址（已完成校验与可选参数签名）。
        ffplay_path: 可执行文件路径；留空则按 :func:`find_ffplay` 查找。
        title: 播放器窗口标题。

    Returns:
        :class:`FfplayProcess`。

    Raises:
        PlayerNotFoundError: 找不到 ffplay。
        PlayerError: 播放器进程无法启动（例如权限不足）。
    """
    executable = ffplay_path or find_ffplay()
    if not executable:
        raise PlayerNotFoundError(
            "未检测到 ffplay，无法在线播放。请安装 FFmpeg 并把它加入 PATH"
            "（ffplay 与 ffmpeg 在同一个 bin 目录里）。"
        )

    # 参数以列表形式传入，不经过 shell：地址里可能含有 & 等字符，
    # 拼成命令行会被 shell 解释成另一条语句。
    argv = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-window_title",
        title,
        "-i",
        url,
    ]

    # stderr 必须收进管道并读走：它带着失败原因，而写进文件就等于把签名落到
    # 磁盘上。
    return FfplayProcess(_popen(argv, stderr=subprocess.PIPE))


def launch_mpv(
    url: str,
    *,
    mpv_path: str | None = None,
    title: str = "HLS 在线播放",
    speed: float = 1.0,
    ipc_path: str | None = None,
    ipc_timeout: float = MPV_IPC_CONNECT_TIMEOUT_SECONDS,
) -> MpvProcess:
    """用 mpv 播放 ``url``，并连上它的 IPC 通道以支持 seek 与倍速。

    Args:
        url: 交给播放器的地址（已完成校验与可选参数签名）。
        mpv_path: 可执行文件路径；留空则按 :func:`find_mpv` 查找。
        title: 播放器窗口标题。
        speed: 初始倍速。
        ipc_path: IPC 端点路径；留空则自动生成一个唯一的命名管道。
        ipc_timeout: 等待 mpv 建好 IPC 端点的最长时间（秒）。

    Returns:
        :class:`MpvProcess`。IPC 连不上时仍然返回可用的句柄，只是
        :attr:`~PlayerHandle.supports_control` 为 ``False``。

    Raises:
        PlayerNotFoundError: 找不到 mpv。
        PlayerError: 播放器进程无法启动（例如权限不足）。
    """
    executable = mpv_path or find_mpv()
    if not executable:
        raise PlayerNotFoundError(
            "未检测到 mpv，无法使用进度条与倍速。请安装 mpv 并把它加入 PATH。"
        )

    endpoint = ipc_path or _default_ipc_path()
    argv = [
        executable,
        "--no-terminal",
        "--force-window=yes",
        "--keep-open=no",
        # 播放位置不落盘：这个功能整体不写文件
        "--save-position-on-quit=no",
        # 进度条与倍速的**操作界面就在播放器窗口里**（OSC 上可拖动的进度条，
        # 配合 mpv 自带的 [ / ] 调速、空格暂停等快捷键），不再由本程序另做一套。
        # 显式打开 OSC：万一用户的 mpv.conf 里关掉了它，窗口里就既没有进度条
        # 也没有别的跳转手段了。
        "--osc=yes",
        f"--title={title}",
        f"--speed={speed:g}",
        f"--input-ipc-server={endpoint}",
        url,
    ]

    process = _popen(argv, stderr=subprocess.DEVNULL)
    transport = _connect_ipc(endpoint, ipc_timeout)
    return MpvProcess(process, transport)


def launch_playback(
    url: str,
    *,
    title: str = "HLS 在线播放",
    speed: float = 1.0,
    mpv_path: str | None = None,
    ffplay_path: str | None = None,
) -> PlayerHandle:
    """挑一个可用的播放器播放 ``url``：优先 mpv，没有就退回 ffplay。

    优先 mpv 是因为它才有进度条与倍速；ffplay 只在装了 FFmpeg 时才有，
    作为「至少能看」的兜底。

    Raises:
        PlayerNotFoundError: mpv 与 ffplay 都找不到。
    """
    if mpv_path or find_mpv():
        return launch_mpv(url, mpv_path=mpv_path, title=title, speed=speed)
    return launch_ffplay(url, ffplay_path=ffplay_path, title=title)


def stop_playback(process: PlayerHandle | None, *, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
    """停止播放器进程；已经退出的进程会被忽略。

    只用到 ``poll`` / ``terminate`` / ``wait`` / ``kill``，因此任何提供这几个
    方法的对象（例如测试里的替身）都可以传入。

    先请求正常退出，给播放器一点时间自行收尾；超时后强杀，避免留下一个
    关不掉的窗口。
    """
    if process is None or process.poll() is not None:
        return

    # 先断掉 IPC 通道：否则读线程会一直挂在管道上
    close = getattr(process, "close", None)
    if callable(close):
        try:
            close()
        except OSError:  # pragma: no cover - 通道可能已经断了
            pass

    try:
        process.terminate()
    except OSError:  # pragma: no cover - 进程可能刚好自己退了
        return

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:  # pragma: no cover - 同上
            return
        # 强杀之后也要回收一次，否则 POSIX 上会留下僵尸、returncode 也永远是 None
        try:
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):  # pragma: no cover
            pass
