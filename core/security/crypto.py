"""信封加密（Envelope Encryption）。

架构（TDD-06 §2.1）：

    KEK（主密钥）          来自环境变量 / KMS，不落盘
      └── 加密 ──→ DEK（数据加密密钥）  每店铺一个，密文存库
                    └── 加密 ──→ 业务数据（Token、PII）

为什么用信封加密而不是直接用主密钥：
    1. **轮换成本低**：换主密钥时只需重新加密 DEK（几十字节），
       不必重新加密全部业务数据（可能几百万行）
    2. **爆炸半径小**：单店铺的 DEK 泄漏不影响其他店铺
    3. **可审计**：DEK 的每次使用都可记录

算法：AES-256-GCM
    - 认证加密，密文被篡改会解密失败（而非返回错误明文）
    - 12 字节 nonce（GCM 推荐值）
    - 支持 AAD（附加认证数据），用于绑定上下文

AAD 的用途（重要）：
    把 shop_id 等上下文作为 AAD 参与认证，
    这样 A 店铺的密文无法被搬到 B 店铺的行里使用（会解密失败）。
    防止"数据库被部分篡改后，密文被跨行搬运"这类攻击。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.exceptions import AppError, ConfigurationError

__all__ = [
    "NONCE_SIZE",
    "KEY_SIZE",
    "EncryptedBlob",
    "EnvelopeEncryption",
    "get_encryptor",
    "DecryptionError",
]

#: GCM 推荐的 nonce 长度
NONCE_SIZE: Final[int] = 12

#: AES-256 密钥长度
KEY_SIZE: Final[int] = 32


class DecryptionError(AppError):
    """解密失败。

    常见原因：
    - 密文被篡改（GCM 认证失败）
    - AAD 不匹配（密文被跨行搬运）
    - 使用了错误的密钥版本
    """

    code = "DECRYPTION_FAILED"
    http_status = 500


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    """加密结果。

    对应数据库中的三列（如 shop_credentials）：
        access_token_encrypted  ← ciphertext
        access_token_nonce      ← nonce
        key_version             ← key_version
    """

    ciphertext: bytes
    nonce: bytes
    key_version: int

    def __post_init__(self) -> None:
        if len(self.nonce) != NONCE_SIZE:
            raise ValueError(f"nonce 长度必须为 {NONCE_SIZE} 字节，实际 {len(self.nonce)}")

    @property
    def is_empty(self) -> bool:
        return len(self.ciphertext) == 0


def _load_master_key(raw: str, *, key_version: int) -> bytes:
    """把配置中的主密钥规整为 32 字节。

    接受两种形式：
    1. base64 编码的 32 字节（推荐）
    2. 任意字符串 → 用 SHA-256 派生（兼容，但会损失熵）

    第二种是刻意保留的宽容度：开发环境随手写一个密码也能跑，
    但生产环境会在 Settings 校验阶段拦截空密钥。
    """
    if not raw:
        raise ConfigurationError(
            "未配置加密主密钥 EC_MASTER_KEY",
            code="MASTER_KEY_MISSING",
            action="在 .env 或环境变量中配置 EC_MASTER_KEY",
        )

    # 尝试 base64 解码
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) == KEY_SIZE:
            return decoded
    except (ValueError, TypeError):
        pass

    # 回退：SHA-256 派生（输出恰好 32 字节）
    return hashlib.sha256(f"ec-autoflow:kek:v{key_version}:{raw}".encode()).digest()


class EnvelopeEncryption:
    """信封加密器。

    无状态 —— 可安全地在多线程/多进程间共享。

    用法：
        enc = EnvelopeEncryption(master_key, key_version=1)

        # 1. 为新店铺生成 DEK
        dek = enc.generate_dek()
        wrapped = enc.wrap_dek(dek)          # 存库

        # 2. 加密数据
        blob = enc.encrypt(token.encode(), dek, aad=f"shop:{shop_id}".encode())

        # 3. 解密
        dek = enc.unwrap_dek(wrapped)
        token = enc.decrypt(blob, dek, aad=f"shop:{shop_id}".encode())
    """

    __slots__ = ("_kek", "_key_version")

    def __init__(self, master_key: str, *, key_version: int = 1) -> None:
        self._kek = _load_master_key(master_key, key_version=key_version)
        self._key_version = key_version

    @property
    def key_version(self) -> int:
        return self._key_version

    # ---------- DEK 管理 ----------

    @staticmethod
    def generate_dek() -> bytes:
        """生成新的数据加密密钥（32 字节，密码学安全随机）。"""
        return secrets.token_bytes(KEY_SIZE)

    def wrap_dek(self, dek: bytes) -> bytes:
        """用主密钥加密 DEK。

        输出格式：nonce(12) || ciphertext+tag
        这样存库只需一列 VARBINARY。
        """
        if len(dek) != KEY_SIZE:
            raise ValueError(f"DEK 长度必须为 {KEY_SIZE} 字节")
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = AESGCM(self._kek).encrypt(nonce, dek, b"dek")
        return nonce + ciphertext

    def unwrap_dek(self, wrapped: bytes) -> bytes:
        """用主密钥解密 DEK。"""
        if len(wrapped) <= NONCE_SIZE:
            raise DecryptionError(
                "DEK 密文长度异常",
                code="DEK_MALFORMED",
                context={"length": len(wrapped)},
            )
        nonce, ciphertext = wrapped[:NONCE_SIZE], wrapped[NONCE_SIZE:]
        try:
            return AESGCM(self._kek).decrypt(nonce, ciphertext, b"dek")
        except InvalidTag as exc:
            raise DecryptionError(
                "DEK 解密失败：密文被篡改或主密钥版本不匹配",
                code="DEK_DECRYPT_FAILED",
                action="检查 EC_MASTER_KEY 与 EC_MASTER_KEY_VERSION 是否与加密时一致",
            ) from exc

    # ---------- 数据加解密 ----------

    def encrypt(
        self,
        plaintext: bytes,
        dek: bytes,
        *,
        aad: bytes | None = None,
    ) -> EncryptedBlob:
        """加密数据。

        Args:
            plaintext: 明文。
            dek: 数据加密密钥（来自 generate_dek 或 unwrap_dek）。
            aad: 附加认证数据，建议传上下文（如 b"shop:123"）。
                 传了 AAD 后，解密时必须传相同的值。

        Returns:
            EncryptedBlob，含密文、nonce、密钥版本。
        """
        if len(dek) != KEY_SIZE:
            raise ValueError(f"DEK 长度必须为 {KEY_SIZE} 字节")
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        return EncryptedBlob(
            ciphertext=ciphertext, nonce=nonce, key_version=self._key_version
        )

    def decrypt(
        self,
        blob: EncryptedBlob,
        dek: bytes,
        *,
        aad: bytes | None = None,
    ) -> bytes:
        """解密数据。

        Raises:
            DecryptionError: 认证失败（密文被改、AAD 不符、密钥错误）。
        """
        if len(dek) != KEY_SIZE:
            raise ValueError(f"DEK 长度必须为 {KEY_SIZE} 字节")
        try:
            return AESGCM(dek).decrypt(blob.nonce, blob.ciphertext, aad)
        except InvalidTag as exc:
            raise DecryptionError(
                "解密失败：密文被篡改、AAD 不匹配或密钥错误",
                code="DATA_DECRYPT_FAILED",
                context={"key_version": blob.key_version},
            ) from exc

    # ---------- 便捷方法（单层加密，用于非店铺级小数据）----------

    def encrypt_text(self, text: str, *, aad: bytes | None = None) -> bytes:
        """用主密钥直接加密文本，返回自包含字节串（nonce 前置）。

        适用于无需 DEK 的小数据（如一次性导出文件的密码）。
        店铺 Token 这类长期数据请用 DEK 两层方案。
        """
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = AESGCM(self._kek).encrypt(nonce, text.encode("utf-8"), aad)
        return nonce + ciphertext

    def decrypt_text(self, payload: bytes, *, aad: bytes | None = None) -> str:
        if len(payload) <= NONCE_SIZE:
            raise DecryptionError("密文长度异常", code="DATA_MALFORMED")
        nonce, ciphertext = payload[:NONCE_SIZE], payload[NONCE_SIZE:]
        try:
            return AESGCM(self._kek).decrypt(nonce, ciphertext, aad).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise DecryptionError("解密失败", code="DATA_DECRYPT_FAILED") from exc


# ---------- 全局单例 ----------

_encryptor: EnvelopeEncryption | None = None


def get_encryptor() -> EnvelopeEncryption:
    """获取加密器单例。

    延迟初始化 —— 避免 import 时就读环境变量，
    这样测试可以在设置环境变量后再取。
    """
    global _encryptor  # noqa: PLW0603
    if _encryptor is None:
        from core.config import get_settings

        settings = get_settings()
        _encryptor = EnvelopeEncryption(
            settings.ec_master_key,
            key_version=settings.ec_master_key_version,
        )
    return _encryptor
