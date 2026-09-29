"""商品、刊登、类目、SKU 映射。

关键设计：

1. **`product_listings.listing_status` 的取值直接对应
   domain.state_machines.listing 的 12 个状态**，两处必须一致。

2. **`category_schemas` 必须版本化**
   Amazon 的 Product Type Definition 会变。缓存不版本化会导致
   "昨天能上架今天不行"，且极难排查。

3. **SKU 映射的唯一性用生成列实现**（ADR-007 §3.7）
   PostgreSQL 的 `EXCLUDE USING btree ... WHERE is_primary = TRUE`
   在 MySQL 不存在。替代方案是生成列 + 唯一索引：
        primary_sku_key = IF(is_primary, internal_sku, NULL)
    MySQL 的唯一索引允许多个 NULL，因此非主映射不受约束，
    **这是 MySQL 里表达"部分唯一索引"的唯一可靠手段**。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Computed, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.constants import ListingStatus, ProductStatus
from core.db import Base
from core.models.mixins import SoftDeleteMixin, TimestampMixin
from core.models.types import (
    BigIntCol,
    BigIntColNullable,
    BigIntPK,
    BinaryColNullable,
    BoolCol,
    IntCol,
    JsonCol,
    JsonColNullable,
    MoneyCol,
    MoneyColNullable,
    PercentColNullable,
    TimestampColNullable,
)

__all__ = [
    "Product",
    "ProductVariant",
    "ProductAttribute",
    "ProductListing",
    "PlatformCategory",
    "CategorySchema",
    "SKUMapping",
    "PriceHistory",
    "PricingRule",
]


class Product(Base, TimestampMixin, SoftDeleteMixin):
    """商品主表（我方视角）。"""

    __tablename__ = "products"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    internal_sku: Mapped[str] = mapped_column(String(128), nullable=False, comment="内部 SKU（全局唯一）")
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)

    category_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="内部类目")

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ProductStatus.DRAFT.value,
        server_default=ProductStatus.DRAFT.value,
    )

    #: 主图 URL
    main_image: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: 图片列表
    images: Mapped[list | None] = mapped_column(JsonColNullable, nullable=True)

    #: 采购成本（用于利润核算）
    cost_price: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    #: 重量（克）—— 头程分摊按重量
    weight_grams: Mapped[int | None] = mapped_column(IntCol, nullable=True)

    attributes: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "internal_sku", name="uk_products_tenant_sku"),
        Index("idx_products_status", "tenant_id", "status"),
        Index("idx_products_brand", "tenant_id", "brand"),
        {"comment": "商品主表"},
    )


class ProductVariant(Base, TimestampMixin):
    """商品变体（父子结构）。"""

    __tablename__ = "product_variants"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    variant_sku: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_sku: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: 变体属性（如 {"color": "black", "size": "M"}）
    variant_attributes: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)

    cost_price: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)
    weight_grams: Mapped[int | None] = mapped_column(IntCol, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "variant_sku", name="uk_product_variants_sku"),
        Index("idx_product_variants_product", "product_id"),
        {"comment": "商品变体"},
    )


class ProductAttribute(Base, TimestampMixin):
    """商品属性明细（支持按属性筛选与追溯来源）。"""

    __tablename__ = "product_attributes"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    attr_name: Mapped[str] = mapped_column(String(128), nullable=False)
    attr_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 属性来源：MANUAL / AI / PLATFORM / SUPPLIER
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="MANUAL", server_default="MANUAL"
    )
    #: 若由 AI 生成，记录模型与提示词版本（TDD-01 原则：AI 产出可追溯）
    ai_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ai_prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("product_id", "attr_name", name="uk_product_attributes_name"),
        {"comment": "商品属性"},
    )


class ProductListing(Base, TimestampMixin):
    """商品在平台的刊登实例（一个商品可铺多店铺）。

    `listing_status` 的 12 个取值与 domain.state_machines.listing 严格一致。
    """

    __tablename__ = "product_listings"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    product_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    variant_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    platform_sku: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="平台侧 SKU")
    platform_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="ASIN / item_id")
    parent_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 上架状态机（12 态）
    listing_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ListingStatus.DRAFT.value,
        server_default=ListingStatus.DRAFT.value,
        comment="上架状态机状态",
    )

    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Feed ID / task ID（异步提交的凭证）
    submission_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    price: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    quantity: Mapped[int | None] = mapped_column(IntCol, nullable=True)

    #: 原始报文（原则 5：原始数据不可变）
    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    platform_raw: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("shop_id", "platform_sku", name="uk_listings_shop_platform_sku"),
        Index("idx_listings_product", "tenant_id", "product_id"),
        Index("idx_listings_shop_status", "shop_id", "listing_status"),
        Index("idx_listings_item", "shop_id", "platform_item_id"),
        Index("idx_listings_submission", "submission_id"),
        {"comment": "平台刊登实例"},
    )


class PlatformCategory(Base, TimestampMixin):
    """平台类目缓存。"""

    __tablename__ = "platform_categories"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    name: Mapped[str] = mapped_column(String(512), nullable=False)
    is_leaf: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")

    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        UniqueConstraint("platform", "region", "category_id", name="uk_platform_categories"),
        Index("idx_platform_categories_parent", "platform", "region", "parent_id"),
        {"comment": "平台类目缓存"},
    )


class CategorySchema(Base, TimestampMixin):
    """类目属性 schema 缓存（Amazon Product Type Definitions 结果）。

    **必须版本化**：schema 会变，不版本化会导致"昨天能上架今天不行"。
    """

    __tablename__ = "category_schemas"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[str] = mapped_column(String(128), nullable=False)
    product_type: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: schema 版本（平台返回）
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 完整属性定义
    schema_json: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)

    #: 冗余必填字段列表（便于快速校验）
    #: 注意：MySQL 无数组类型，用 JSON 存（ADR-007 §3.5）
    required_fields: Mapped[list] = mapped_column(JsonCol, nullable=False, default=list)

    fetched_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    expires_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "platform", "region", "category_id", "schema_version",
            name="uk_category_schemas_version",
        ),
        Index("idx_category_schemas_lookup", "platform", "region", "category_id", "fetched_at"),
        {"comment": "类目属性 schema（版本化）"},
    )


class SKUMapping(Base, TimestampMixin):
    """SKU 映射（平台 SKU ↔ 内部 SKU）。

    唯一性用生成列实现（ADR-007 §3.7）：

        primary_sku_key = IF(is_primary = 1, internal_sku, NULL)
        UNIQUE (shop_id, primary_sku_key)

    MySQL 的唯一索引允许多个 NULL，因此非主映射可以有多条，
    而"每个店铺每个内部 SKU 只能有一个主映射"被强制保证。

    这替代了 PostgreSQL 的
        EXCLUDE USING btree (shop_id WITH =, internal_sku WITH =) WHERE (is_primary)
    """

    __tablename__ = "sku_mappings"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    internal_sku: Mapped[str] = mapped_column(String(128), nullable=False)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_sku: Mapped[str] = mapped_column(String(255), nullable=False)
    platform_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 是否为主映射（一个内部 SKU 在一个店铺只能有一个主映射）
    is_primary: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")

    #: 生成列 —— 用于实现"部分唯一索引"（ADR-007 §3.7）
    #:
    #: 必须用 `Computed()` 声明，**不能**写成普通列：
    #:     写成普通列的话，Alembic 会生成一个可写的 VARCHAR 列，
    #:     而我们的唯一索引期待它由表达式自动计算 ——
    #:     结果是索引建在了一个永远为 NULL/空 的列上，
    #:     约束既不生效也不报错。
    #:
    #: 表达式含义：is_primary=1 时取 internal_sku，否则为 NULL。
    #: 利用"MySQL 唯一索引允许多个 NULL"的特性，
    #: 实现"每个店铺每个内部 SKU 只能有一个主映射"。
    primary_sku_key: Mapped[str | None] = mapped_column(
        String(128),
        Computed("IF(is_primary = 1, internal_sku, NULL)", persisted=True),
        nullable=True,
        comment="生成列：is_primary=1 时为 internal_sku，否则 NULL",
    )

    #: 冲突检测状态（PRD 6.4 要求冲突时阻断）
    has_conflict: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")
    conflict_detail: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("shop_id", "platform_sku", name="uk_sku_mappings_shop_platform_sku"),
        # 部分唯一索引：依赖生成列的 NULL 语义
        Index("uk_sku_mappings_primary", "shop_id", "primary_sku_key", unique=True),
        Index("idx_sku_mappings_internal", "tenant_id", "internal_sku"),
        Index("idx_sku_mappings_shop", "shop_id", "internal_sku"),
        {"comment": "SKU 映射"},
    )


class PriceHistory(Base):
    """价格历史（**append-only**，原则 6：事实表只追加）。

    没有 updated_at —— 历史记录不应被修改。
    """

    __tablename__ = "price_history"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    listing_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    platform_sku: Mapped[str] = mapped_column(String(255), nullable=False)

    old_price: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)
    new_price: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)

    #: 变动比例（%），负数表示降价
    change_pct: Mapped[Decimal | None] = mapped_column(PercentColNullable, nullable=True)

    #: 变更来源：MANUAL / RULE / AI / PLATFORM_SYNC
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 关联的审批单（若走了审批）
    approval_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_price_history_sku", "tenant_id", "shop_id", "platform_sku", "created_at"),
        Index("idx_price_history_time", "tenant_id", "created_at"),
        {"comment": "价格历史（append-only）"},
    )


class PricingRule(Base, TimestampMixin):
    """定价规则（Phase 1 建表，Phase 2 使用）。"""

    __tablename__ = "pricing_rules"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    scope_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="GLOBAL", server_default="GLOBAL"
    )
    scope_value: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 规则条件
    condition_expr: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)
    #: 定价动作
    action: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)

    #: 底线（防止规则算出亏损价）
    min_price: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)
    max_price: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)

    is_active: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=True, server_default="1")
    version: Mapped[int] = mapped_column(IntCol, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("tenant_id", "rule_code", "version", name="uk_pricing_rules_version"),
        Index("idx_pricing_rules_active", "tenant_id", "scope_type"),
        {"comment": "定价规则"},
    )
