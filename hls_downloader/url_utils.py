"""URL 校验、相对地址解析与日志脱敏工具。"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from .models import InvalidUrlError

SUPPORTED_SCHEMES = ("http", "https")


def validate_http_url(url: str | None) -> str:
    """校验并规范化一个 HTTP/HTTPS URL。

    Args:
        url: 用户输入的原始字符串。

    Returns:
        去除首尾空白后的 URL。

    Raises:
        InvalidUrlError: URL 为空、协议不支持、缺少主机名或包含空白字符。
    """
    if url is None:
        raise InvalidUrlError("URL 不能为空，请输入以 http:// 或 https:// 开头的链接。")

    text = str(url).strip()
    if not text:
        raise InvalidUrlError("URL 不能为空，请输入以 http:// 或 https:// 开头的链接。")

    if "://" not in text:
        raise InvalidUrlError("URL 格式不正确，需要以 http:// 或 https:// 开头。")

    if any(ch.isspace() for ch in text):
        raise InvalidUrlError("URL 中不能包含空格或换行，请检查后重试。")

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise InvalidUrlError(f"仅支持 http/https 链接，当前协议为 “{parts.scheme or '空'}”。")

    if not parts.netloc:
        raise InvalidUrlError("URL 缺少主机名，请检查链接是否完整。")

    return text


def resolve_url(base_url: str, reference: str) -> str:
    """把播放列表中可能出现的相对地址解析为绝对地址。

    绝对 URL、以 ``//`` 开头的协议相对地址、以 ``/`` 开头的根相对地址
    以及普通相对地址都可以正确处理。
    """
    ref = (reference or "").strip()
    if not ref:
        raise InvalidUrlError("分片地址为空。")
    return urljoin(base_url, ref)


def without_query(url: str) -> str:
    """去掉查询串与锚点，只保留协议、主机和路径。

    用于生成与签名无关的稳定标识：``wsTime`` 之类的参数每次都不一样，
    直接拿完整 URL 比较会导致永远对不上。
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def get_extension(url: str) -> str:
    """返回 URL 路径部分的小写扩展名（含点），没有则返回空字符串。"""
    path = urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def redact_url(url: str) -> str:
    """为日志脱敏：隐藏路径中的中间层级与整个查询串。

    保留协议、主机名和最后一个路径片段（通常是文件名），便于排查问题，
    同时避免把令牌、签名或 Cookie 写进日志。
    """
    if not url:
        return "<空>"

    try:
        parts = urlsplit(url)
    except ValueError:
        return "<无法解析的 URL>"

    if not parts.scheme or not parts.netloc:
        return "<无效 URL>"

    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        redacted_path = "/"
    elif len(segments) == 1:
        redacted_path = "/" + segments[0]
    else:
        redacted_path = "/…/" + segments[-1]

    query = "?…" if parts.query else ""
    # 只保留主机（含端口），去掉 ``user:password@`` 形式的用户信息：有些服务把
    # 令牌放在那里，那不是"路径"能脱敏掉的。
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return f"{parts.scheme}://{netloc}{redacted_path}{query}"
