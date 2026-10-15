"""程序目录与运行数据目录的单元测试。

这些决定了打包成客户端之后「文件找不找得到」和「会不会往安装目录外乱写」。
"""

from __future__ import annotations

import os

import pytest

from hls_downloader import app_paths


@pytest.fixture(autouse=True)
def _clear_cache():
    app_paths.reset_cache()
    yield
    app_paths.reset_cache()


def test_executable_name_adds_the_platform_suffix():
    name = app_paths.executable_name("ffmpeg")

    if os.name == "nt":
        assert name == "ffmpeg.exe"
    else:
        assert name == "ffmpeg"


def test_find_bundled_tool_finds_the_bin_subdirectory(tmp_path, monkeypatch):
    monkeypatch.setattr(app_paths, "application_dir", lambda: tmp_path)
    tools = tmp_path / app_paths.TOOLS_DIR_NAME
    tools.mkdir()
    binary = tools / app_paths.executable_name("mpv")
    binary.write_text("", encoding="utf-8")

    assert app_paths.find_bundled_tool("mpv") == binary


def test_find_bundled_tool_also_looks_next_to_the_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(app_paths, "application_dir", lambda: tmp_path)
    binary = tmp_path / app_paths.executable_name("ffmpeg")
    binary.write_text("", encoding="utf-8")

    assert app_paths.find_bundled_tool("ffmpeg") == binary


def test_find_bundled_tool_finds_files_unpacked_by_pyinstaller(tmp_path, monkeypatch):
    """--add-binary 会把文件解到 _MEIPASS，那里和 exe 所在目录不是一个地方。"""
    unpacked = tmp_path / "meipass"
    (unpacked / app_paths.TOOLS_DIR_NAME).mkdir(parents=True)
    binary = unpacked / app_paths.TOOLS_DIR_NAME / app_paths.executable_name("ffmpeg")
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(app_paths, "application_dir", lambda: tmp_path / "elsewhere")
    monkeypatch.setattr(app_paths.sys, "_MEIPASS", str(unpacked), raising=False)

    assert app_paths.find_bundled_tool("ffmpeg") == binary


def test_find_bundled_tool_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(app_paths, "application_dir", lambda: tmp_path)

    assert app_paths.find_bundled_tool("ffmpeg") is None


def test_runtime_data_dir_prefers_the_directory_next_to_the_app(tmp_path, monkeypatch):
    """便携优先：程序目录能写就写在那儿，整个目录拷走就能跑。"""
    monkeypatch.setattr(app_paths, "application_dir", lambda: tmp_path)

    resolved = app_paths.runtime_data_dir()

    assert resolved == tmp_path / app_paths.DATA_DIR_NAME
    assert resolved.is_dir()


def test_runtime_data_dir_falls_back_when_the_app_directory_is_not_writable(
    tmp_path, monkeypatch
):
    """装在只读位置（例如 Program Files）时要退回用户目录，而不是直接崩。"""
    # 用一个「文件」占住程序目录的位置：在它下面建 data 目录必然失败
    blocked = tmp_path / "app"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(app_paths, "application_dir", lambda: blocked)
    user_dir = tmp_path / "本地应用数据"
    monkeypatch.setenv("LOCALAPPDATA", str(user_dir))

    resolved = app_paths.runtime_data_dir()

    assert resolved == user_dir / app_paths.APP_NAME
    assert resolved.is_dir()


def test_runtime_data_dir_is_resolved_once(tmp_path, monkeypatch):
    """目录不该每次调用都去试写一遍。"""
    calls = []

    def counting_application_dir():
        calls.append(1)
        return tmp_path

    monkeypatch.setattr(app_paths, "application_dir", counting_application_dir)

    first = app_paths.runtime_data_dir()
    second = app_paths.runtime_data_dir()

    assert first == second
    assert len(calls) == 1


def test_user_data_dir_uses_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert app_paths.user_data_dir() == tmp_path / app_paths.APP_NAME


def test_application_dir_is_the_repository_root_when_not_frozen():
    """开发时是仓库根目录——'随程序分发的 bin/' 才放得对地方。"""
    expected = app_paths.Path(app_paths.__file__).resolve().parent.parent

    assert app_paths.application_dir() == expected
