"""店铺与凭据。

凭据的安全设计（TDD-06 §2.1、ADR-007 §3.2）：
    用**信封加密**：
        KEK（主密钥，来自环境变量，不落盘）
          └── 加密 → DEK（数据加密密钥，每店铺一个，密文存库）
                       └── 加密 → 实际凭据（access_token / refresh_token）

    为什么不用主密钥直接加密数据：
        1. 轮换成本低：换主密钥只需重加密 DEK（几十字节），
           不必重加密全部凭据
        2. 爆炸半径小：单店铺 DEK 泄漏不影响其他店铺

    存储形态：MySQL 用 VARBINARY（对应 PostgreSQL 的 BYTEA）。

**硬性约束**（写进 repositories/AGENTS.md）：
    1. 凭据明文**只在构造请求头时短暂存在**，用完即弃
    2. 凭据的每次读取都要写 credential_audit_logs
    3. 任何日志不得输出凭据（core.security.masking 的白名单会拦截）
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.constants import CredentialStatus, ShopStatus
from core.db import Base
from core.models.mixins import SoftDeleteMixin, TimestampMixin
from core.models.types import (
    BigIntCol,
    BigIntColNullable,
    BigIntPK,
    BinaryCol,
    BinaryColNullable,
    BoolCol,
    IntCol,
    IntColNullable,
    JsonColNullable,
    SmallBinaryColNullable,
    TimestampCol,
    TimestampColNullable,
)

__all__ = ["Shop", "ShopCredential", "CredentialAuditLog"]


class Shop(Base, TimestampMixin, SoftDeleteMixin):
    """店铺。

    一个店铺 = 一个平台账号在一个区域的一个市场。
    同一卖家在不同区域（NA/EU/FE）算不同店铺，因为
    Base URL、授权、时区都不同。
    """

    __tablename__ = "shops"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    platform: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="平台：AMAZON/TIKTOK/TEMU/..."
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="店铺名称（我方命名）")

    #: 平台侧的店铺标识
    platform_shop_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 卖家 ID（Amazon 的 sellerId）
    seller_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 市场标识（Amazon 的 marketplaceId）
    marketplace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: 区域。Amazon 的 Base URL 按此路由，必须正确（TDD-03 §4.4 坑 3）
    region: Mapped[str] = mapped_column(
        String(16), nullable=False, default="OTHER", server_default="OTHER"
    )

    #: IANA 时区名。报表的"店铺本地日"依赖它（core.timeutil.shop_today）
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )

    #: 币种
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="USD", server_default="USD"
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ShopStatus.ACTIVE.value,
        server_default=ShopStatus.ACTIVE.value,
    )

    #: 同步是否被人工暂停（Kill Switch 的店铺级开关）
    sync_paused: Mapped[bool] = mapped_column(
        BoolCol, nullable=False, default=False, server_default="0"
    )
    sync_paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 平台特有配置（如 TikTok 的 shop_cipher）
    platform_config: Mapped[dict | None] = mapped_column(JsonColNullable, )

    __table_args__ = (
        UniqueConstraint("tenant_id", "platform", "name", name="uk_shops_tenant_platform_name"),
        Index("idx_shops_status", "tenant_id", "status"),
        Index("idx_shops_platform", "tenant_id", "platform"),
        {"comment": "店铺"},
    )


class ShopCredential(Base, TimestampMixin):
    """店铺凭据（加密存储）。

    三列一组构成一个加密字段：
        xxx_encrypted  —— AES-256-GCM 密文（含 tag）
        xxx_nonce      —— 12 字节 nonce
        key_version    —— 密钥版本（支持轮换）

    AAD 约定：使用 `shop:{shop_id}` 作为附加认证数据，
    这样 A 店铺的密文无法被搬到 B 店铺的行里（会解密失败）。
    """

    __tablename__ = "shop_credentials"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    credential_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="OAUTH", server_default="OAUTH",
        comment="凭据类型：OAUTH / API_KEY / BASIC",
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CredentialStatus.ACTIVE.value,
        server_default=CredentialStatus.ACTIVE.value,
    )

    # ---- 信封加密的 DEK ----
    encrypted_dek: Mapped[bytes] = mapped_column(BinaryCol, 
        comment="被主密钥加密的 DEK（nonce 前置）"
    )

    # ---- 访问令牌 ----
    access_token_encrypted: Mapped[bytes | None] = mapped_column(BinaryColNullable, default=None)
    access_token_nonce: Mapped[bytes | None] = mapped_column(SmallBinaryColNullable, default=None)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        TimestampColNullable, nullable=True
    )

    # ---- 刷新令牌（长期凭据，最关键）----
    refresh_token_encrypted: Mapped[bytes | None] = mapped_column(BinaryColNullable, default=None)
    refresh_token_nonce: Mapped[bytes | None] = mapped_column(SmallBinaryColNullable, default=None)

    # ---- 密钥版本（支持轮换）----
    key_version: Mapped[int] = mapped_column(IntCol, nullable=False, default=1, server_default="1")

    # ---- 授权范围 ----
    scopes: Mapped[dict | None] = mapped_column(JsonColNullable, comment="已授权的 scope 列表")

    # ---- 健康度 ----
    last_verified_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(IntCol, nullable=False, default=0, server_default="0")

    #: 授权失效时间（平台侧撤销）
    revoked_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, )

    __table_args__ = (
        UniqueConstraint("shop_id", "credential_type", name="uk_shop_credentials_shop_type"),
        Index("idx_shop_credentials_expiry", "access_token_expires_at"),
        Index("idx_shop_credentials_status", "tenant_id", "status"),
        {"comment": "店铺凭据（加密）"},
    )


class CredentialAuditLog(Base):
    """凭据操作审计（**append-only**）。

    记录谁在何时读取/轮换了凭据。
    写入后禁止 UPDATE/DELETE —— 由触发器强制（ADR-007 §3.9）。

    **绝不记录凭据明文或密文**，只记录操作元数据。
    """

    __tablename__ = "credential_audit_logs"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    credential_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    operation: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="操作：READ / CREATE / ROTATE / REVOKE / DECRYPT"
    )
    actor_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    actor_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SYSTEM", server_default="SYSTEM"
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    result: Mapped[str] = mapped_column(String(32), nullable=False, comment="SUCCESS / FAILED")

    #: 密钥版本（轮换时用于追溯）
    key_version: Mapped[int | None] = mapped_column(IntColNullable, nullable=True)

    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(TimestampCol, 
        server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_cred_audit_shop", "shop_id", "created_at"),
        Index("idx_cred_audit_actor", "actor_id", "created_at"),
        {"comment": "凭据审计（append-only）"},
    )
