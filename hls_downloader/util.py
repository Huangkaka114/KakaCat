"""预留参数的工具包。

两个预留参数的处理入口都在这里，下载核心只依赖本模块，不依赖具体业务实现。

* **参数 A**：填写时视为该服务的授权密钥。下载器在发起请求前用它按
  ``wsSecret`` / ``wsTime`` 规则重建播放列表 URL（见 :func:`rebuild_url`）。
  密钥只参与本地签名计算：不写入日志，也不会用于分片、密钥等其它请求。
* **参数 B**：该服务的基础地址。填写后与参数 A 一起，按固定模板从下载链接
  推导出真实的媒体播放列表地址（见 :data:`MEDIA_PATH_TEMPLATE`）。

本模块不发起任何网络请求。
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import SplitResult, urlsplit

from .models import InvalidUrlError

TAG_SECRET = "wsSecret"
TAG_TIME = "wsTime"


#: 签名 URL 的有效期（秒）：``wsTime`` 取「当前时间 + 该值」。
CLIENT_WS_TTL = 7200

#: 填写参数 B（基础地址）时，媒体播放列表在它下面的固定路径模板。
MEDIA_PATH_TEMPLATE = "/mu/{token}/hls/1/index.m3u8"


def md5hex(text: str) -> str:
    """返回 ``text`` 的 MD5 十六进制摘要。"""
    return hashlib.md5(text.encode()).hexdigest()


def sign(path: str, key: str, ws_time: int) -> str:
    """按 ``md5(key + path + ws_time)`` 生成请求签名。

    ``key`` 就是密钥本身，签名只在本地计算，不会随请求发送。
    """
    return md5hex(key + path + str(ws_time))


def build_url(host: str, path: str, key: str, ws_time: int, scheme: str = "https") -> str:
    """为 ``host`` + ``path`` 生成带签名的请求 URL。

    Args:
        host: 裸主机名（可含端口），不含协议与路径。
        path: URL 路径，以 ``/`` 开头。
        key: 授权密钥，只参与本地签名计算。
        ws_time: 签名有效期的时间戳（秒）。
        scheme: ``http`` 或 ``https``。
    """
    query = f"{TAG_SECRET}={sign(path, key, ws_time)}&{TAG_TIME}={ws_time}"
    return f"{scheme}://{host}{path}?{query}"


def resource_token(link: str) -> str:
    """从下载链接里取出资源标识段（形如 ``<哈希>__<数字>``）。

    优先取路径中第一个含 ``__`` 的段；没有这样的段时退回文件名所在的目录，
    仍然取不到就用去掉扩展名的文件名。

    Raises:
        InvalidUrlError: 链接里没有任何路径段。
    """
    segments = [s for s in urlsplit(link).path.split("/") if s]
    if not segments:
        raise InvalidUrlError("下载链接缺少路径，无法推导资源标识。")

    for segment in segments:
        if "__" in segment:
            return segment
    if len(segments) >= 2:
        return segments[-2]

    name = segments[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _base_parts(base: str) -> SplitResult:
    """解析参数 B（基础地址）；没写协议时按 https 处理。"""
    text = str(base).strip()
    if "://" not in text:
        text = f"https://{text}"

    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise InvalidUrlError(
            "参数 B 不是有效的地址，请填写形如 https://example.com 的基础地址。"
        )
    return parts


def rebuild_url(
    url: str,
    key: str,
    *,
    base: str | None = None,
    now: float | None = None,
) -> str:
    """用授权密钥重建请求 URL。

    两种模式：

    * ``base`` 为空：在 ``url`` 上原地重签名，保留协议、主机与路径；
    * 给了 ``base``（参数 B，基础地址）：按 :data:`MEDIA_PATH_TEMPLATE` 从
      ``url`` 中取出资源标识，拼到 ``base`` 下面，得到真实的媒体播放列表地址。

    两种模式都会丢弃原有的查询串与锚点（例如上个任务留下的旧签名）。

    Args:
        url: 原始链接；给了 ``base`` 时它是下载链接，否则是媒体播放列表 URL。
        key: 授权密钥（参数 A）。
        base: 基础地址（参数 B），可省略。
        now: 计算有效期用的当前时间戳（秒），默认取系统时间；测试可注入。

    Returns:
        带 ``wsSecret`` / ``wsTime`` 查询参数的 URL。

    Raises:
        InvalidUrlError: 密钥为空、基础地址无效、URL 无法解析或缺少主机名。
    """
    secret = (key or "").strip()
    if not secret:
        raise InvalidUrlError("参数 A 为空，无法重建请求 URL。")

    ws_time = int(time.time() if now is None else now) + CLIENT_WS_TTL

    if base is None or not str(base).strip():
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            raise InvalidUrlError("URL 无法解析，请检查链接是否完整。")
        return build_url(parts.netloc, parts.path or "/", secret, ws_time, scheme=parts.scheme)

    prefix = _base_parts(base)
    path = prefix.path.rstrip("/") + MEDIA_PATH_TEMPLATE.format(token=resource_token(url))
    return build_url(prefix.netloc, path, secret, ws_time, scheme=prefix.scheme)


def process_optional_a(value: str | None) -> str | None:
    """处理预留参数 A（授权密钥）。

    密钥本身就是原文，没有可转换的内容，这里原样返回；
    真正使用它的是 :func:`rebuild_url`。
    """
    return value


def process_optional_b(value: str | None) -> str | None:
    """处理预留参数 B（基础地址）。

    地址本身就是原文，没有可转换的内容，这里原样返回；
    真正使用它的是 :func:`rebuild_url` 的 ``base`` 参数。
    """
    return value


def process_optional_params(
    optional_a: str | None,
    optional_b: str | None,
) -> tuple[str | None, str | None]:
    """预留参数的统一处理入口：参数 A 和 B 都从这里经过。

    当前实现为透传，返回值与入参一致；参数 A 的签名逻辑在
    :func:`rebuild_url` 中，由下载核心在发起请求前调用。
    """
    return process_optional_a(optional_a), process_optional_b(optional_b)
