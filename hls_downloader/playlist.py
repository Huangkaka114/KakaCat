"""M3U8 播放列表解析。

当前版本只支持普通、未加密的 HLS VOD 媒体播放列表：

* 必须是媒体播放列表（media playlist），传入主播放列表会明确报错；
* 必须包含 ``#EXT-X-ENDLIST``（直播或未完成的列表会被拒绝）；
* 必须未加密（``METHOD=NONE`` 视为未加密，其他加密方式一律拒绝）；
* 支持 TS 与 fMP4/CMAF 分片，按字节范围（``#EXT-X-BYTERANGE``）切分的
  播放列表当前版本不支持。
"""

from __future__ import annotations

from .models import (
    InvalidPlaylistError,
    LivePlaylistError,
    MasterPlaylistError,
    PlaylistInfo,
    Segment,
    SegmentKind,
    UnsupportedEncryptionError,
    UnsupportedSegmentError,
)
from .url_utils import get_extension, resolve_url

#: 出现这些标签说明这是一个主播放列表
_MASTER_TAGS = frozenset(
    {
        "#EXT-X-STREAM-INF",
        "#EXT-X-MEDIA",
        "#EXT-X-I-FRAME-STREAM-INF",
    }
)

_TS_SUFFIXES = frozenset({".ts", ".mts", ".m2ts"})
_FMP4_SUFFIXES = frozenset({".m4s", ".mp4", ".m4v", ".cmfv", ".cmfa", ".fmp4"})
_AUDIO_ONLY_SUFFIXES = frozenset({".aac", ".mp3", ".ac3", ".eac3", ".m4a", ".flac", ".opus"})

#: 视为未加密的 METHOD 取值
_UNENCRYPTED_METHODS = frozenset({"", "NONE"})


def parse_playlist(text: str, base_url: str) -> PlaylistInfo:
    """解析媒体播放列表文本。

    Args:
        text: M3U8 文件的原始文本内容。
        base_url: 播放列表自身的 URL，用于把相对分片地址解析为绝对地址。

    Returns:
        :class:`PlaylistInfo` 实例，其中分片地址已全部是绝对 URL。

    Raises:
        InvalidPlaylistError: 内容为空、缺少 ``#EXTM3U`` 或不包含任何分片。
        MasterPlaylistError: 传入的是主播放列表。
        UnsupportedEncryptionError: 播放列表包含加密分片。
        UnsupportedSegmentError: 分片格式（如纯音频、字节范围）当前版本不支持。
        LivePlaylistError: 播放列表缺少 ``#EXT-X-ENDLIST``。
    """
    if text is None or not str(text).strip():
        raise InvalidPlaylistError("播放列表内容为空。")

    lines = [line.strip() for line in str(text).lstrip("﻿").splitlines()]
    lines = [line for line in lines if line]

    if not lines[0].startswith("#EXTM3U"):
        raise InvalidPlaylistError("这不是有效的 M3U8 播放列表：文件开头缺少 #EXTM3U 标记。")

    segments: list[Segment] = []
    warnings: list[str] = []

    pending_duration: float | None = None
    pending_title: str | None = None
    has_endlist = False
    playlist_type: str | None = None
    version: int | None = None
    target_duration: int | None = None
    media_sequence = 0
    encryption_method: str | None = None
    init_segment_url: str | None = None

    for line in lines[1:]:
        if not line.startswith("#"):
            # 普通 URI 行：即一个分片地址
            url = resolve_url(base_url, line)
            if pending_duration is None:
                warnings.append("部分分片缺少 #EXTINF 时长信息。")
            segments.append(
                Segment(
                    index=len(segments),
                    url=url,
                    duration=pending_duration,
                    title=pending_title,
                )
            )
            pending_duration = None
            pending_title = None
            continue

        tag, _, value = line.partition(":")
        tag = tag.strip().upper()
        value = value.strip()

        if tag in _MASTER_TAGS:
            raise MasterPlaylistError(
                "这是一个主播放列表（master playlist），其中列出了多个不同码率的子列表。\n"
                "请打开具体码率的媒体播放列表（media playlist）后，再使用它的 M3U8 链接下载。"
            )

        if tag == "#EXTINF":
            duration_text, _, title = value.partition(",")
            try:
                pending_duration = float(duration_text.strip())
            except ValueError:
                pending_duration = None
                warnings.append("部分 #EXTINF 时长无法解析。")
            pending_title = title.strip() or None
        elif tag == "#EXT-X-ENDLIST":
            has_endlist = True
        elif tag == "#EXT-X-PLAYLIST-TYPE":
            playlist_type = value.upper() or None
        elif tag == "#EXT-X-VERSION":
            version = _safe_int(value)
        elif tag == "#EXT-X-TARGETDURATION":
            target_duration = _safe_int(value)
        elif tag == "#EXT-X-MEDIA-SEQUENCE":
            media_sequence = _safe_int(value) or 0
        elif tag == "#EXT-X-KEY":
            method = _parse_attributes(value).get("METHOD", "")
            method = (method or "").upper()
            if method not in _UNENCRYPTED_METHODS:
                raise UnsupportedEncryptionError(
                    f"播放列表使用加密方式 {method} 保护分片，当前版本只支持未加密的播放列表。\n"
                    "本程序不会尝试获取密钥或绕过加密。"
                )
            encryption_method = "NONE"
        elif tag == "#EXT-X-MAP":
            init_segment_url = _resolve_map_uri(value, base_url)
        elif tag == "#EXT-X-BYTERANGE":
            raise UnsupportedSegmentError(
                "播放列表使用 #EXT-X-BYTERANGE 按字节范围切分分片，当前版本不支持这种形式。"
            )
        elif tag == "#EXT-X-DISCONTINUITY":
            warnings.append("播放列表包含 #EXT-X-DISCONTINUITY，拼接后的时间戳可能存在不连续。")
        elif tag == "#EXT-X-SESSION-KEY":
            method = (_parse_attributes(value).get("METHOD", "") or "").upper()
            if method not in _UNENCRYPTED_METHODS:
                raise UnsupportedEncryptionError(
                    f"播放列表使用加密方式 {method} 保护分片，当前版本只支持未加密的播放列表。"
                )

    if not segments:
        raise InvalidPlaylistError("播放列表中没有找到任何分片地址，请确认链接指向的是媒体播放列表。")

    if not has_endlist:
        raise LivePlaylistError(
            "播放列表未包含 #EXT-X-ENDLIST 标记，可能是直播列表或尚未生成完成的点播列表。\n"
            "当前版本只支持已经结束的点播（VOD）播放列表。"
        )

    kind = _classify_segments(segments, init_segment_url, warnings)

    return PlaylistInfo(
        segments=tuple(segments),
        source_url=base_url,
        segment_kind=kind,
        target_duration=target_duration,
        version=version,
        media_sequence=media_sequence,
        playlist_type=playlist_type,
        has_endlist=has_endlist,
        encryption_method=encryption_method,
        init_segment_url=init_segment_url,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _classify_segments(
    segments: list[Segment],
    init_segment_url: str | None,
    warnings: list[str],
) -> SegmentKind:
    """根据分片地址推断容器格式，并补充必要的提示信息。"""
    suffixes = {get_extension(segment.url) for segment in segments}
    known = {suffix for suffix in suffixes if suffix}

    if known and known <= _AUDIO_ONLY_SUFFIXES:
        raise UnsupportedSegmentError(
            "该播放列表看起来只包含音频分片，当前版本仅支持下载视频。"
        )

    if known and known <= _TS_SUFFIXES:
        return SegmentKind.TS

    if known and known <= _FMP4_SUFFIXES:
        if init_segment_url is None:
            warnings.append(
                "fMP4 播放列表缺少 #EXT-X-MAP 初始化分片，合并结果可能无法正常播放。"
            )
        return SegmentKind.FMP4

    if not known and init_segment_url is not None:
        return SegmentKind.FMP4

    if known & _TS_SUFFIXES and known & _FMP4_SUFFIXES:
        warnings.append("分片扩展名不一致（同时出现 TS 与 fMP4），将按 TS 处理。")
        return SegmentKind.TS

    if known & _AUDIO_ONLY_SUFFIXES:
        warnings.append("播放列表中包含音频分片，合并结果可能不完整。")

    warnings.append(
        "无法从分片地址判断容器格式（可能存在没有扩展名的分片），将按二进制拼接处理。"
    )
    return SegmentKind.UNKNOWN


def _resolve_map_uri(value: str, base_url: str) -> str | None:
    """解析 ``#EXT-X-MAP:URI="..."`` 中的初始化分片地址。"""
    uri = _parse_attributes(value).get("URI")
    if not uri:
        return None
    return resolve_url(base_url, uri)


def _safe_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _parse_attributes(text: str) -> dict[str, str]:
    """解析 ``KEY=VALUE,KEY="VALUE,WITH,COMMA"`` 形式的标签属性。"""
    attributes: dict[str, str] = {}
    index = 0
    length = len(text)

    while index < length:
        equals = text.find("=", index)
        if equals == -1:
            break

        key = text[index:equals].strip().strip(",").strip().upper()
        index = equals + 1

        if index < length and text[index] == '"':
            index += 1
            end = text.find('"', index)
            if end == -1:
                end = length
            value = text[index:end]
            index = end + 1
        else:
            comma = text.find(",", index)
            if comma == -1:
                comma = length
            value = text[index:comma]
            index = comma

        if key:
            attributes[key] = value.strip()

        while index < length and text[index] in ", ":
            index += 1

    return attributes
