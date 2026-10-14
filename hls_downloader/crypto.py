"""HLS 整段 AES-128 解密（RFC 8216 §4.3.2.4 与 §5.2）。

只实现标准 AES-128 整段加密：

* 密钥为 16 字节，由播放列表中的 ``#EXT-X-KEY:URI=...`` 直接提供；
* 每个分片用 AES-128-CBC 独立加密，各自使用自己的 IV；
* 明文按 PKCS7 填充，解密后需要去掉填充。

本模块是纯函数集合：只做解密计算，不发起任何网络请求，也不关心密钥从哪里来。
"""

from __future__ import annotations

import binascii

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .models import EncryptionKey, SegmentDecryptionError

#: AES 分组长度，同时也是 AES-128 的密钥长度
AES_BLOCK_SIZE = 16

#: 当前版本支持的整段加密方式
SUPPORTED_METHODS = frozenset({"AES-128"})


def build_iv(key: EncryptionKey, media_sequence: int, index: int) -> bytes:
    """推导某个分片使用的 16 字节 IV。

    按 RFC 8216 §5.2：``#EXT-X-KEY`` 显式给出 ``IV`` 时使用它；否则使用该
    分片的媒体序号（``#EXT-X-MEDIA-SEQUENCE`` + 分片下标）作为 128 位大端整数。
    """
    if key.iv:
        return _parse_hex_iv(key.iv)

    sequence = media_sequence + index
    if sequence < 0:
        raise SegmentDecryptionError(f"分片媒体序号为负数（{sequence}），无法推导 IV。")
    return sequence.to_bytes(AES_BLOCK_SIZE, "big")


def decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    """解密一个完整的加密分片并去掉 PKCS7 填充。

    Args:
        data: 该分片的完整密文。
        key: 16 字节密钥。
        iv: 16 字节初始向量。

    Returns:
        解密后的明文。

    Raises:
        SegmentDecryptionError: 密钥/IV 长度不对、密文长度不是分组整数倍，
            或去填充失败（通常意味着密钥或 IV 不正确）。

    Note:
        去填充校验能可靠地发现**密钥**错误（错误密钥会打乱所有分组），但
        发现不了多分组分片上的 **IV** 错误：CBC 下错误的 IV 只影响第一个
        分组，而填充位于最后一个分组。这是 CBC 的固有性质，不是本实现的
        疏漏。IV 的正确性最终由播放列表本身保证（RFC 8216 §5.2）。
    """
    if len(key) != AES_BLOCK_SIZE:
        raise SegmentDecryptionError(
            f"密钥长度应为 {AES_BLOCK_SIZE} 字节，实际为 {len(key)} 字节。"
        )
    if len(iv) != AES_BLOCK_SIZE:
        raise SegmentDecryptionError(
            f"IV 长度应为 {AES_BLOCK_SIZE} 字节，实际为 {len(iv)} 字节。"
        )
    if not data:
        raise SegmentDecryptionError("分片内容为空，无法解密。")
    if len(data) % AES_BLOCK_SIZE != 0:
        raise SegmentDecryptionError(
            f"加密分片长度为 {len(data)} 字节，不是 {AES_BLOCK_SIZE} 的整数倍，"
            "数据可能不完整。"
        )

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    return _strip_pkcs7(plain)


def _parse_hex_iv(value: str) -> bytes:
    """把 ``#EXT-X-KEY`` 里的 IV 属性解析为 16 字节。"""
    text = value.strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    try:
        raw = binascii.unhexlify(text)
    except (binascii.Error, ValueError) as exc:
        raise SegmentDecryptionError(f"IV “{value}” 不是合法的十六进制字符串。") from exc
    if len(raw) != AES_BLOCK_SIZE:
        raise SegmentDecryptionError(
            f"IV 应为 {AES_BLOCK_SIZE} 字节（32 个十六进制字符），实际为 {len(raw)} 字节。"
        )
    return raw


def _strip_pkcs7(data: bytes) -> bytes:
    """去掉 PKCS7 填充，并校验填充是否合法。"""
    pad = data[-1]
    if pad == 0 or pad > AES_BLOCK_SIZE:
        raise SegmentDecryptionError(
            "解密结果的填充不合法，密钥或 IV 可能不正确。"
        )
    if data[-pad:] != bytes([pad]) * pad:
        raise SegmentDecryptionError(
            "解密结果的填充不一致，密钥或 IV 可能不正确。"
        )
    return data[:-pad]
