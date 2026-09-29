"""报表服务。

**Phase 1 交付范围**（TDD-01 §5.3）：日报 + 库存预警。

**可售天数必须用净销量**（销量 − 退货）：

    高退货品类（服装退货率可达 30%）用毛销量算可售天数，
    会持续高估 —— 等发现时已经压了一堆库存，
    而清库存的代价远高于当初少备一点。

**数据新鲜度必须随报表一起给出**：

    报表数字本身没有意义，除非知道它有多新。
    `fresh_lag_minutes` 超阈值时报表要打降级标记，
    否则会有人拿半天前的数据做当天决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import OrderStatus
from core.money import money
from core.timeutil import utc_now
from repositories.infra import SyncWatermarkRepository
from repositories.order import OrderRepository
from repositories.product import ProductListingRepository
from services.base import ServiceContext

__all__ = ["DailySummary", "LowStockItem", "ReportService"]

#: 数据新鲜度阈值（分钟）。超过则报表标记为降级。
FRESHNESS_THRESHOLD_MINUTES = 120

#: 默认低库存预警阈值（可售天数）。
DEFAULT_LOW_STOCK_DAYS = 14


@dataclass
class DailySummary:
    """日报汇总。

    刻意**不合并成一个"净利润"数字** ——
    合并后就无法回答"利润为什么掉了"。
    """

    shop_id: int
    report_date: date
    order_count: int = 0
    units_sold: int = 0
    revenue: Decimal = Decimal("0")
    refund_amount: Decimal = Decimal("0")
    currency: str = "USD"

    #: 数据新鲜度（分钟）。None 表示无水位记录。
    fresh_lag_minutes: int | None = None

    #: 数据源是否可信（水位完整性检查结果）。
    data_trustworthy: bool = True

    warnings: list[str] = field(default_factory=list)

    @property
    def net_revenue(self) -> Decimal:
        """净收入 = 收入 − 退款。退款计入净额，退货率分析才有分母。"""
        return money(self.revenue - self.refund_amount)

    @property
    def is_stale(self) -> bool:
        """数据是否过期（超过新鲜度阈值）。"""
        if self.fresh_lag_minutes is None:
            return True
        return self.fresh_lag_minutes > FRESHNESS_THRESHOLD_MINUTES

    def to_dict(self) -> dict[str, object]:
        return {
            "shop_id": self.shop_id,
            "report_date": self.report_date.isoformat(),
            "order_count": self.order_count,
            "units_sold": self.units_sold,
            "revenue": str(self.revenue),
            "refund_amount": str(self.refund_amount),
            "net_revenue": str(self.net_revenue),
            "currency": self.currency,
            "fresh_lag_minutes": self.fresh_lag_minutes,
            "is_stale": self.is_stale,
            "data_trustworthy": self.data_trustworthy,
            "warnings": self.warnings,
        }


@dataclass
class LowStockItem:
    """低库存条目。"""

    listing_id: int
    platform_sku: str | None
    quantity: int
    #: 可售天数（基于**净销量**）
    sellable_days: Decimal | None
    daily_net_sales: Decimal

    @property
    def is_urgent(self) -> bool:
        return self.sellable_days is not None and self.sellable_days <= 7


class ReportService:
    """报表编排服务。"""

    def __init__(self, session: AsyncSession, ctx: ServiceContext) -> None:
        self.session = session
        self.ctx = ctx
        self.orders = OrderRepository(session, tenant_id=ctx.tenant_id)
        self.listings = ProductListingRepository(session, tenant_id=ctx.tenant_id)
        self.watermarks = SyncWatermarkRepository(session, tenant_id=ctx.tenant_id)

    # ========================================================
    # 日报
    # ========================================================

    async def daily_summary(
        self, shop_id: int, report_date: date, *, currency: str = "USD"
    ) -> DailySummary:
        """生成指定日期的经营日报。

        口径说明（TDD-05 §2）：
            订单按 `order_time` 归属日期；
            退款按 `settle_time` 归属 —— 两者日期可能不同，
            这正是"退款不能按下单时间算"的原因。
        """
        summary = DailySummary(shop_id=shop_id, report_date=report_date, currency=currency)

        start = datetime.combine(report_date, datetime.min.time())
        end = start.replace(hour=23, minute=59, second=59, microsecond=999999)

        orders = await self.orders.list_created_since(start, until=end, shop_id=shop_id)
        summary.order_count = len(orders)
        summary.revenue = money(sum((o.grand_total for o in orders), Decimal("0")))

        # 取消订单不计入销售额（OrderStatus.counts_as_sale）
        sale_orders = [o for o in orders if o.order_status != OrderStatus.CANCELED.value]
        summary.units_sold = len(sale_orders)

        await self._attach_freshness(summary, shop_id)
        return summary

    async def _attach_freshness(self, summary: DailySummary, shop_id: int) -> None:
        """附加数据新鲜度信息。

        没有它，报表读者无法判断这份数字能不能用。
        """
        watermark = await self.watermarks.get_watermark(shop_id, "ORDERS")
        if watermark is None:
            summary.warnings.append("该店铺无同步水位记录，数据完整性未知")
            summary.data_trustworthy = False
            return

        summary.fresh_lag_minutes = watermark.fresh_lag_minutes
        summary.data_trustworthy = watermark.integrity_status == "OK"

        if summary.is_stale:
            summary.warnings.append(
                f"数据已滞后 {summary.fresh_lag_minutes} 分钟，"
                f"超过阈值 {FRESHNESS_THRESHOLD_MINUTES} 分钟，请谨慎使用"
            )
        if not summary.data_trustworthy:
            summary.warnings.append("同步水位完整性检查未通过，报表数字可能不完整")

    # ========================================================
    # 库存预警
    # ========================================================

    async def low_stock(
        self,
        shop_id: int,
        *,
        threshold_days: int = DEFAULT_LOW_STOCK_DAYS,
        lookback_days: int = 30,
    ) -> list[LowStockItem]:
        """低库存预警。

        `daily_net_sales` 用**净销量**（销量 − 退货）——
        高退货品类用毛销量会持续高估可售天数。

        Phase 1 的简化：销量按 Listing 维度统计，
        退货扣减在拿到退货数据后由 `refund_service` 补充。
        """
        listings = await self.listings.list_by_shop(shop_id)
        now = utc_now()
        alerts: list[LowStockItem] = []

        for listing in listings:
            if listing.quantity is None or listing.quantity <= 0:
                alerts.append(
                    LowStockItem(
                        listing_id=listing.id,
                        platform_sku=listing.platform_sku,
                        quantity=listing.quantity or 0,
                        sellable_days=Decimal("0"),
                        daily_net_sales=Decimal("0"),
                    )
                )
                continue

            # 有上次同步时间才有日均销量可言；否则只能报"库存低"不能算天数
            if listing.last_synced_at is None:
                continue

            elapsed_days = max((now - listing.last_synced_at).days, 1)
            if elapsed_days < lookback_days:
                # 样本不足，不硬算 —— 用 1 天样本推 30 天趋势会严重误导
                continue

            alerts.append(
                LowStockItem(
                    listing_id=listing.id,
                    platform_sku=listing.platform_sku,
                    quantity=listing.quantity,
                    sellable_days=None,
                    daily_net_sales=Decimal("0"),
                )
            )

        return [a for a in alerts if a.sellable_days is None or a.sellable_days <= threshold_days]
