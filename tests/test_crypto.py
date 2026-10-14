"""HLS 整段 AES-128 解密的单元测试。

纯计算，不涉及网络，也不访问任何真实站点。
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hls_downloader import crypto
from hls_downloader.models import EncryptionKey, SegmentDecryptionError

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
OTHER_KEY = bytes.fromhex("ffeeddccbbaa99887766554433221100")
IV = bytes.fromhex("0f0e0d0c0b0a09080706050403020100")


def encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    """把明文按 HLS 的方式加密：PKCS7 填充 + AES-128-CBC。"""
    pad = crypto.AES_BLOCK_SIZE - (len(plain) % crypto.AES_BLOCK_SIZE)
    padded = plain + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def make_key(iv: str | None = None) -> EncryptionKey:
    return EncryptionKey(method="AES-128", uri="https://key.test/k", iv=iv)


# --------------------------------------------------------------------------
# IV 推导（RFC 8216 §5.2）
# --------------------------------------------------------------------------


def test_explicit_iv_is_used():
    key = make_key("0x0f0e0d0c0b0a09080706050403020100")

    assert crypto.build_iv(key, media_sequence=999, index=5) == IV


def test_iv_prefix_and_case_do_not_matter():
    upper = make_key("0F0E0D0C0B0A09080706050403020100")

    assert crypto.build_iv(upper, media_sequence=0, index=0) == IV


def test_iv_defaults_to_media_sequence():
    """未给出 IV 时，用媒体序号作为 128 位大端整数。"""
    key = make_key(None)

    assert crypto.build_iv(key, media_sequence=7, index=0) == (7).to_bytes(16, "big")


def test_default_iv_advances_with_segment_index():
    key = make_key(None)

    assert crypto.build_iv(key, media_sequence=10, index=3) == (13).to_bytes(16, "big")


def test_bad_iv_text_is_rejected():
    key = make_key("not-hex-at-all")

    with pytest.raises(SegmentDecryptionError) as excinfo:
        crypto.build_iv(key, media_sequence=0, index=0)

    assert "十六进制" in excinfo.value.message


def test_iv_of_wrong_length_is_rejected():
    key = make_key("0x0102")

    with pytest.raises(SegmentDecryptionError) as excinfo:
        crypto.build_iv(key, media_sequence=0, index=0)

    assert "16" in excinfo.value.message


# --------------------------------------------------------------------------
# 解密
# --------------------------------------------------------------------------


def test_round_trip_with_explicit_iv():
    plain = b"MPEG-TS like payload " * 10

    assert crypto.decrypt_segment(encrypt(plain, KEY, IV), KEY, IV) == plain


def test_round_trip_when_plaintext_is_exactly_one_block():
    """明文长度正好是分组的整数倍时，填充为完整的一块，去掉后必须还原。"""
    plain = b"X" * 16

    assert crypto.decrypt_segment(encrypt(plain, KEY, IV), KEY, IV) == plain


def test_round_trip_with_sequence_derived_iv():
    key = make_key(None)
    iv = crypto.build_iv(key, media_sequence=4, index=2)
    plain = b"segment payload" * 7
    cipher_text = encrypt(plain, KEY, iv)

    assert crypto.decrypt_segment(cipher_text, KEY, iv) == plain


def test_wrong_key_is_rejected_not_silently_wrong():
    """密钥不对时必须报错，绝不能返回一段看起来正常的垃圾数据。

    这里所有输入都是固定常量，因此结果是确定性的（不是概率性偶发）。
    """
    cipher_text = encrypt(b"secret payload" * 5, KEY, IV)

    with pytest.raises(SegmentDecryptionError):
        crypto.decrypt_segment(cipher_text, OTHER_KEY, IV)


def test_wrong_iv_is_rejected_when_it_corrupts_the_padding():
    """密文只有**一个**分组时，错误的 IV 会让填充校验失败，能被抓出来。"""
    plain = b"tiny"  # 填充后正好一个分组
    cipher_text = encrypt(plain, KEY, IV)

    with pytest.raises(SegmentDecryptionError):
        crypto.decrypt_segment(cipher_text, KEY, bytes(16))


def test_wrong_iv_leaves_only_the_first_block_corrupted():
    """把 CBC 的固有性质固定下来：错误的 IV 只破坏第一个分组。

    填充在**最后一个**分组里，所以多分组分片上的 IV 错误**无法**靠去填充发现。
    这里显式断言这个已知行为，免得以后误以为它已经被校验覆盖了。
    """
    plain = b"A" * 48  # 三个分组
    cipher_text = encrypt(plain, KEY, IV)

    result = crypto.decrypt_segment(cipher_text, KEY, bytes(16))

    assert result != plain
    assert result[16:] == plain[16:], "只有第一个分组应当受影响"


def test_truncated_ciphertext_is_rejected():
    """长度不是 16 的整数倍，说明分片下载不完整，不能当成明文。"""
    cipher_text = encrypt(b"payload" * 4, KEY, IV)

    with pytest.raises(SegmentDecryptionError) as excinfo:
        crypto.decrypt_segment(cipher_text[:-3], KEY, IV)

    assert "16" in excinfo.value.message


def test_empty_ciphertext_is_rejected():
    with pytest.raises(SegmentDecryptionError):
        crypto.decrypt_segment(b"", KEY, IV)


def test_key_of_wrong_length_is_rejected():
    with pytest.raises(SegmentDecryptionError) as excinfo:
        crypto.decrypt_segment(encrypt(b"data", KEY, IV), KEY[:8], IV)

    assert "16" in excinfo.value.message


def test_iv_of_wrong_length_is_rejected():
    with pytest.raises(SegmentDecryptionError):
        crypto.decrypt_segment(encrypt(b"data", KEY, IV), KEY, IV[:8])


# --------------------------------------------------------------------------
# 边界：不实现 DRM
# --------------------------------------------------------------------------


def test_supported_methods_only_contains_aes128():
    """本模块只声称支持整段 AES-128，不包含任何 DRM 采样级加密。"""
    assert crypto.SUPPORTED_METHODS == frozenset({"AES-128"})


def test_encryption_key_is_encrypted_flag():
    assert make_key().is_encrypted is True
    assert EncryptionKey(method="NONE", uri="").is_encrypted is False
