"""财务：结算、成本、汇率、会计期间、利润快照。

核心口径（TDD-05 §2.1）：

    净销售额 = 商品销售额 + 买家承担运费 − 折扣 − 退款 − 拒付 − 销售相关税费
    履约前贡献利润 = 净销售额 − 商品销售成本 − 头程分摊
    履约后贡献利润 = 履约前贡献利润 − 平台佣金 − 配送/仓储费 − 退货处理及不可售损失
    广告后贡献利润 = 履约后贡献利润 − 可归因广告费 − 促销/达人佣金 − 其他可变费用
    TACOS = 广告费 / 净销售额          ← 分母统一为净销售额

**三级贡献利润字段独立存储**，不靠运行时计算：
    报表要按不同维度聚合（店铺/类目/SKU/月份），
    如果每次聚合都重算三级利润，会因舍入累积产生尾差。
    存储快照保证"合计 = 分项之和"。

**会计期间关账后禁止写入**（TDD-05 §2.4）：
    检查在 service 层做（而非数据库触发器），因为需要给出
    "请先 reopen" 这样的操作建议 —— 触发器做不到。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.constants import PeriodStatus
from core.db import Base
from core.models.mixins import TimestampMixin
from core.models.types import (
    BigIntCol,
    BigIntColNullable,
    BigIntPK,
    BoolCol,
    IntCol,
    JsonColNullable,
    MoneyCol,
    MoneyColNullable,
    PercentColNullable,
    RateCol,
    TimestampCol,
    TimestampColNullable,
)

__all__ = [
    "ExchangeRate",
    "AccountingPeriod",
    "CostItem",
    "CostRule",
    "SettlementRecord",
    "SettlementFee",
    "ProfitSnapshot",
]


class ExchangeRate(Base, TimestampMixin):
    """汇率（多口径）。

    四种口径（TDD-05 §2.5），查询时按优先级选择：
        settlement  结算对账（优先级 1，最真实）
        month_end   财务报表（优先级 2）
        daily       经营日报（优先级 3）
        order_date  订单当天（备选）

    **同一报表内必须用同一口径的汇率** ——
    混用会导致"合计不等于分项之和"，表现为几毛钱的尾差且极难追溯。
    """

    __tablename__ = "exchange_rates"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    from_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    to_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    rate: Mapped[Decimal] = mapped_column(RateCol, nullable=False)

    #: 口径：SETTLEMENT / MONTH_END / DAILY / ORDER_DATE
    rate_type: Mapped[str] = mapped_column(String(32), nullable=False)

    effective_date: Mapped[date] = mapped_column(
        TimestampCol, nullable=False, comment="生效日期（UTC）"
    )

    #: 数据来源（可追溯）
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "from_currency", "to_currency", "rate_type", "effective_date",
            name="uk_exchange_rates_key",
        ),
        Index("idx_exchange_rates_lookup", "tenant_id", "from_currency", "to_currency", "rate_type", "effective_date"),
        {"comment": "汇率（多口径）"},
    )


class AccountingPeriod(Base, TimestampMixin):
    """会计期间（关账管理）。

    CLOSED 期间**禁止任何写入** —— 变更必须先 reopen
    （需 admin 权限 + 审计记录）。
    """

    __tablename__ = "accounting_periods"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    #: 期间标识，如 "2026-09"
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[date] = mapped_column(TimestampCol, nullable=False)
    period_end: Mapped[date] = mapped_column(TimestampCol, nullable=False)

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PeriodStatus.OPEN.value,
        server_default=PeriodStatus.OPEN.value,
    )

    closed_by: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    reopen_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 关账时的口径快照（成本计价方式、汇率口径），
    #: 便于事后复现"当时是按什么口径算的"
    calculation_snapshot: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "shop_id", "period", name="uk_accounting_periods_key"),
        Index("idx_accounting_periods_status", "tenant_id", "status"),
        {"comment": "会计期间"},
    )


class CostItem(Base, TimestampMixin):
    """成本项（COGS、头程、包装、关税等）。"""

    __tablename__ = "cost_items"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    internal_sku: Mapped[str] = mapped_column(String(128), nullable=False)

    #: 成本类型：PURCHASE / INBOUND_FREIGHT / PACKAGING / TARIFF / OTHER
    cost_type: Mapped[str] = mapped_column(String(64), nullable=False)

    amount: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")

    quantity: Mapped[int | None] = mapped_column(IntCol, nullable=True, comment="数量（用于单位成本）")
    unit_cost: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)

    #: 批次（FIFO 计价依赖它）
    batch_no: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: 归属期间
    period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(TimestampColNullable, nullable=True)

    #: 是否含可抵扣增值税（TDD-05 §2.2 第 1 条：默认不含）
    vat_included: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")

    supplier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_cost_items_sku", "tenant_id", "internal_sku", "cost_type"),
        Index("idx_cost_items_period", "tenant_id", "period"),
        {"comment": "成本项"},
    )


class CostRule(Base, TimestampMixin):
    """成本与分摊规则（**版本化**）。

    规则必须可追溯 —— 改了分摊方式后，历史数据的成本
    必须还能用当时的规则解释。
    """

    __tablename__ = "cost_rules"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: 规则类型：ALLOCATION（分摊） / VALUATION（计价） / ROUNDING（舍入）
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)

    #: 规则参数，如 {"method": "by_weight", "fallback": "by_value"}
    params: Mapped[dict] = mapped_column(JsonColNullable, nullable=False, default=dict)

    version: Mapped[int] = mapped_column(IntCol, nullable=False, default=1, server_default="1")
    effective_from: Mapped[datetime] = mapped_column(
        TimestampCol, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    effective_to: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    is_active: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=True, server_default="1")

    __table_args__ = (
        UniqueConstraint("tenant_id", "rule_code", "version", name="uk_cost_rules_version"),
        Index("idx_cost_rules_active", "tenant_id", "rule_type"),
        {"comment": "成本规则（版本化）"},
    )


class SettlementRecord(Base, TimestampMixin):
    """平台结算批次。"""

    __tablename__ = "settlement_records"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    settlement_id: Mapped[str] = mapped_column(String(255), nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    total_amount: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    #: 对账状态：PENDING / MATCHED / DISCREPANCY
    reconcile_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", server_default="PENDING"
    )
    #: 对账差异金额
    discrepancy_amount: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)
    discrepancy_detail: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("shop_id", "settlement_id", name="uk_settlement_records_shop_id"),
        Index("idx_settlement_records_status", "tenant_id", "reconcile_status"),
        {"comment": "平台结算批次"},
    )


class SettlementFee(Base):
    """结算费用明细。

    **对账核心**：差异必须能追到具体条目。
    没有到条目级的明细，"差异 0.5%" 这个结论无法行动。
    """

    __tablename__ = "settlement_fees"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    settlement_id: Mapped[str] = mapped_column(String(255), nullable=False)
    platform_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform_sku: Mapped[str | None] = mapped_column(String(255), nullable=True)

    fee_type: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    #: 是否已在订单费用表中找到对应条目（对账用）
    matched_order_fee_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    is_matched: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")

    fee_time: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_settlement_fees_settlement", "shop_id", "settlement_id"),
        Index("idx_settlement_fees_matched", "tenant_id", "is_matched"),
        Index("idx_settlement_fees_order", "platform_order_id"),
        {"comment": "结算费用明细"},
    )


class ProfitSnapshot(Base, TimestampMixin):
    """利润快照（多维度）。

    **三级贡献利润独立存储**，不靠运行时计算 ——
    报表要按不同维度聚合，每次重算会因舍入累积产生尾差。
    存储快照保证"合计 = 分项之和"。
    """

    __tablename__ = "profit_snapshots"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    #: 快照粒度：DAY / WEEK / MONTH / PERIOD
    granularity: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 维度键（如 "2026-09" 或 "2026-09-29"）
    period_key: Mapped[str] = mapped_column(String(32), nullable=False)

    #: 可选维度
    platform_sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    # ---- 口径 1：净销售额 ----
    gross_sales: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    shipping_income: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    discount_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    refund_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    chargeback_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    tax_total: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    net_sales: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))

    # ---- 口径 2：履约前贡献利润 ----
    cogs: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    inbound_freight: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    pre_fulfillment_margin: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))

    # ---- 口径 3：履约后贡献利润 ----
    platform_commission: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    fulfillment_fee: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    storage_fee: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    return_loss: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    post_fulfillment_margin: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))

    # ---- 口径 4：广告后贡献利润（最终口径）----
    ad_spend: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    promotion_cost: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    other_variable_cost: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))
    post_ads_margin: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False, default=Decimal("0"))

    # ---- 派生指标（分母统一为净销售额）----
    tacos: Mapped[Decimal | None] = mapped_column(PercentColNullable, nullable=True)
    margin_rate: Mapped[Decimal | None] = mapped_column(PercentColNullable, nullable=True)
    refund_rate: Mapped[Decimal | None] = mapped_column(PercentColNullable, nullable=True)

    # ---- 业务量 ----
    order_count: Mapped[int] = mapped_column(IntCol, nullable=False, default=0)
    unit_count: Mapped[int] = mapped_column(IntCol, nullable=False, default=0)

    #: 使用的口径参数快照（成本计价方式、汇率口径）
    calculation_meta: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "shop_id", "granularity", "period_key", "platform_sku",
            name="uk_profit_snapshots_key",
        ),
        Index("idx_profit_snapshots_lookup", "tenant_id", "shop_id", "granularity", "period_key"),
        Index("idx_profit_snapshots_sku", "tenant_id", "platform_sku"),
        {"comment": "利润快照"},
    )
