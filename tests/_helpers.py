"""测试辅助工具：完全本地的假 HTTP 会话。

所有测试都通过这里的假对象完成，不会访问任何真实视频网站。
"""

from __future__ import annotations

import threading
from typing import Iterable

import requests


class FakeResponse:
    """模拟 ``requests.Response`` 中被下载器实际用到的部分。"""

    def __init__(
        self,
        *,
        status_code: int = 200,
        body: bytes | str = b"",
        headers: dict[str, str] | None = None,
        encoding: str | None = "utf-8",
        url: str = "https://cdn.test/",
        chunks: list[bytes] | None = None,
        cancel_event: threading.Event | None = None,
        cancel_after: int | None = None,
    ) -> None:
        self.status_code = status_code
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.content = body
        self.headers = dict(headers or {})
        self.encoding = encoding
        self.url = url
        self._chunks = chunks
        self._cancel_event = cancel_event
        self._cancel_after = cancel_after

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def iter_content(self, chunk_size: int = 1) -> Iterable[bytes]:
        if self._chunks is not None:
            for index, chunk in enumerate(self._chunks):
                yield chunk
                if (
                    self._cancel_event is not None
                    and self._cancel_after is not None
                    and index + 1 >= self._cancel_after
                ):
                    self._cancel_event.set()
            return

        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def close(self) -> None:
        return None


class FakeSession:
    """按 URL 返回预设响应，并记录收到的每一个请求。

    路由值可以是：

    * :class:`FakeResponse`：直接返回；
    * ``list[FakeResponse]``：按顺序依次弹出，用于测试重试；
    * ``Exception``：抛出该异常（例如 ``requests.Timeout()``）；
    * 可调用对象：调用后取其返回值。
    """

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes: dict[str, object] = dict(routes or {})
        self.requests: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}
        self.closed = False

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.requests.append((url, kwargs))

        if url not in self.routes:
            return FakeResponse(status_code=404, url=url)

        entry = self.routes[url]
        if isinstance(entry, list):
            if not entry:
                raise AssertionError(f"路由 {url} 的预设响应已经用尽")
            entry = entry.pop(0)
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            entry = entry()
        return entry

    def close(self) -> None:
        self.closed = True

    @property
    def requested_urls(self) -> list[str]:
        return [url for url, _ in self.requests]


def build_media_playlist(
    segment_names: list[str],
    *,
    durations: list[float] | None = None,
    endlist: bool = True,
    prefix_lines: list[str] | None = None,
) -> str:
    """构造一个普通的、未加密的 HLS 媒体播放列表文本。"""
    durations = durations or [4.0] * len(segment_names)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:4",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    lines.extend(prefix_lines or [])
    for name, duration in zip(segment_names, durations):
        lines.append(f"#EXTINF:{duration},")
        lines.append(name)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def build_segment_routes(
    base: str,
    names: list[str],
    *,
    size: int = 64,
    fill: bytes = b"X",
) -> dict[str, FakeResponse]:
    """为每个分片名生成一段内容固定的假响应。"""
    routes: dict[str, FakeResponse] = {}
    for index, name in enumerate(names):
        body = fill * (size + index)
        routes[f"{base}{name}"] = FakeResponse(
            body=body,
            headers={"Content-Length": str(len(body))},
        )
    return routes
