"""订单、退款、退货。

**PII 处理（TDD-02 §8，最重要的一张表的设计决策）**：
    `orders` 表**不存买家姓名、地址、电话明文**。设计是：

    - 能用哈希关联的分析场景 → `buyer_hash`
    - 只做地域分析 → `buyer_region`
    - 必须留存用于发货的收货信息 → `buyer_encrypted` 加密存储，
      且受 Amazon 30 天删除规则约束（PRD 15.3）

    **这意味着**：ERP 打单所需的明文地址不经过本系统。
    本系统定位是经营分析，不是履约系统（PRD 2.3 已声明不做 WMS）。

口径时间（PRD F5.2.1）：
    - `order_time`   下单时间（经营口径）
    - `ship_time`    发货时间
    - `settle_time`  结算时间（财务口径）

    退款金额按 `settle_time` 归属，不按 `order_time` ——
    否则会出现"当月退款调整上月销售额"的混乱（TDD-05 §2.2 第 3 条）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.constants import OrderStatus, RefundStatus, RefundType, RiskLevel, RestockStatus
from core.db import Base
from core.models.mixins import TimestampMixin
from core.models.types import (
    BigIntCol,
    BigIntColNullable,
    BigIntPK,
    BinaryColNullable,
    BoolCol,
    IntCol,
    JsonColNullable,
    MoneyCol,
    MoneyColNullable,
    PercentColNullable,
    SmallBinaryColNullable,
    TimestampCol,
    TimestampColNullable,
)

__all__ = ["Order", "OrderItem", "OrderFee", "Refund", "ReturnRecord"]


class Order(Base, TimestampMixin):
    """订单（平台驱动，本系统只做镜像）。"""

    __tablename__ = "orders"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    platform_order_id: Mapped[str] = mapped_column(String(255), nullable=False)

    #: 订单状态（7 态，平台驱动）
    order_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=OrderStatus.PENDING.value,
        server_default=OrderStatus.PENDING.value,
    )

    fulfillment_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ---- 买家信息：PII 最小化（TDD-02 §8）----
    #: 买家标识的不可逆哈希（能统计复购率，但无法还原是谁）
    buyer_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 仅保留地区（已聚合，非 PII）
    buyer_region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 必须留存的收货信息（加密）。受 30 天删除规则约束。
    buyer_encrypted: Mapped[bytes | None] = mapped_column(BinaryColNullable, nullable=True)
    buyer_nonce: Mapped[bytes | None] = mapped_column(SmallBinaryColNullable, nullable=True)

    # ---- 金额 ----
    item_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    shipping_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    discount_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    tax_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    grand_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    # ---- 口径时间 ----
    order_time: Mapped[datetime] = mapped_column(TimestampCol, nullable=False, comment="下单时间（经营口径）")
    ship_time: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    settle_time: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True, comment="结算时间（财务口径）")

    # ---- 数据质量 ----
    #: 平台上报的状态换向不符合预期时置位（TDD-04 §5.3）
    #: **不阻断写入，只标记** —— 数据完整性优先于状态纯洁性
    anomaly_flag: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")
    anomaly_detail: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    #: 未映射的平台状态（触发"需补映射"告警）
    raw_order_status: Mapped[str | None] = mapped_column(String(64), nullable=True)

    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    platform_raw: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        UniqueConstraint("shop_id", "platform_order_id", name="uk_orders_shop_order"),
        Index("idx_orders_shop_time", "tenant_id", "shop_id", "order_time"),
        Index("idx_orders_status", "shop_id", "order_status", "order_time"),
        Index("idx_orders_settle", "shop_id", "settle_time"),
        Index("idx_orders_buyer_hash", "buyer_hash"),
        Index("idx_orders_anomaly", "tenant_id", "anomaly_flag"),
        {"comment": "订单（平台镜像）"},
    )


class OrderItem(Base):
    """订单行。"""

    __tablename__ = "order_items"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    order_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    platform_sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)

    quantity: Mapped[int] = mapped_column(IntCol, nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    item_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    discount_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    #: 商品成本（用于利润核算，来自 products.cost_price 快照）
    unit_cost: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)

    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_order_items_order", "order_id"),
        Index("idx_order_items_sku", "tenant_id", "platform_sku"),
        {"comment": "订单行"},
    )


class OrderFee(Base):
    """平台费用明细。

    对账关键：**每笔费用能追溯到订单或结算批次**。
    这是财务对账能闭合的前提。
    """

    __tablename__ = "order_fees"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    settlement_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 费用类型：REFERRAL_FEE / FBA_FULFILLMENT_FEE / STORAGE_FEE / AD_SPEND / ...
    fee_type: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    platform_sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fee_time: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_order_fees_order", "order_id"),
        Index("idx_order_fees_settlement", "shop_id", "settlement_id"),
        Index("idx_order_fees_type", "tenant_id", "fee_type", "fee_time"),
        {"comment": "平台费用明细"},
    )


class Refund(Base, TimestampMixin):
    """退款。

    混合驱动（TDD-04 §6）：
        - Amazon：不支持卖家侧执行退款，状态全部由平台同步驱动
        - TikTok / Shopee / Lazada / Shopify：可走自动批准

    `auto_decision` 留痕是硬要求（TDD-04 §6.4）：
        当买家投诉"为什么给我拒了"，需要能复现当时的决策依据。
        只有决策结果没有依据，等于没有审计能力。
    """

    __tablename__ = "refunds"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    order_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    platform_refund_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    refund_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RefundType.FULL.value,
        server_default=RefundType.FULL.value,
    )

    #: 平台原始原因码
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: LLM 归类结果：quality / size / not_liked / shipping / wrong_item
    reason_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_confidence: Mapped[Decimal | None] = mapped_column(PercentColNullable, nullable=True)

    amount: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RefundStatus.REQUESTED.value,
        server_default=RefundStatus.REQUESTED.value,
    )

    risk_level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RiskLevel.LOW.value,
        server_default=RiskLevel.LOW.value,
    )

    #: 自动化判定留痕（TDD-04 §6.4）
    auto_decision: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    handled_by: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    restock_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="SELLABLE / UNSELLABLE / PENDING"
    )

    #: 若走了审批，关联审批单
    approval_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("shop_id", "platform_refund_id", name="uk_refunds_shop_platform_id"),
        Index("idx_refunds_order", "order_id"),
        Index("idx_refunds_status", "shop_id", "status", "created_at"),
        Index("idx_refunds_reason", "tenant_id", "reason_category"),
        {"comment": "退款"},
    )


class ReturnRecord(Base, TimestampMixin):
    """退货记录。

    注意表名用 `return_records` 而非 `returns` ——
    `RETURN` 在部分数据库语境下是保留字，避免踩坑。
    """

    __tablename__ = "return_records"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    platform_return_id: Mapped[str] = mapped_column(String(255), nullable=False)
    platform_sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quantity: Mapped[int] = mapped_column(IntCol, nullable=False, default=1)

    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 库存处置：可再售 / 不可售 / 待判定
    restock_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=RestockStatus.PENDING.value
    )

    #: 退货损失（退回运费 + 不可售残值损失 + 换标费）
    loss_amount: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)
    loss_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    returned_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("shop_id", "platform_return_id", name="uk_return_records_shop_return"),
        Index("idx_return_records_order", "order_id"),
        Index("idx_return_records_status", "tenant_id", "restock_status"),
        {"comment": "退货记录"},
    )
