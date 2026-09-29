"""订单与售后仓储。

订单同步的**双轨设计**（TDD-01 §5.2、TDD-03）：

    1. 按 `order_time` 拉新   —— 捕获新下单的订单
    2. 按 `updated_at` 回看 14 天 —— 捕获已下单订单的发货、取消、结算变化

    只做第 1 条会**静默漏数据**：订单 3 天前下的、今天才发货，
    按 `order_time` 增量同步永远看不到这次状态变化。
    报表会因此少算履约，而且没有任何报错。

**幂等**：`(shop_id, platform_order_id)` 唯一。
    平台重推同一订单时必须走更新语义，不能产生重复行 ——
    重复订单会直接污染销售额与利润。

**PII**：买家信息加密存 `buyer_encrypted`，
    明文查询请走 `core.security.crypto`，且必须留审计。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select

from core.constants import OrderStatus, RefundStatus
from core.models import Order, OrderFee, OrderItem, Refund, ReturnRecord
from repositories.base import BaseRepository, SyncBaseRepository

__all__ = [
    "OrderFeeRepository",
    "OrderItemRepository",
    "OrderRepository",
    "RefundRepository",
    "ReturnRecordRepository",
    "SyncOrderFeeRepository",
    "SyncOrderItemRepository",
    "SyncOrderRepository",
    "SyncRefundRepository",
    "SyncReturnRecordRepository",
]


class _OrderQueries:
    """查询构建（async / sync 共用）。"""

    # ---------- 订单 ----------

    def _q_order_by_platform_id(self, shop_id: int, platform_order_id: str) -> Select:
        """按平台订单号查（幂等键的一半）。"""
        return (
            self._stmt()
            .where(Order.shop_id == shop_id)
            .where(Order.platform_order_id == platform_order_id)
        )

    def _q_orders_by_order_time(self, since: datetime, until: datetime | None) -> Select:
        """**增量轨道 1**：按下单时间拉取。

        新订单靠这条捕获。
        """
        stmt = self._stmt().where(Order.order_time >= since)
        if until is not None:
            stmt = stmt.where(Order.order_time < until)
        return self._apply_ordering(stmt, "order_time")

    def _q_orders_by_updated(self, since: datetime) -> Select:
        """**增量轨道 2**：按更新时间回看。

        已下单订单的发货/取消/结算变化靠这条捕获。
        缺了它，老订单的状态变化永远不会被同步。
        """
        return self._apply_ordering(self._stmt().where(Order.updated_at >= since), "updated_at")

    def _q_orders_by_status(self, order_status: str) -> Select:
        return self._stmt().where(Order.order_status == order_status)

    def _q_orders_anomaly(self) -> Select:
        """异常订单（状态换向、金额异常等）。

        TDD-04 的决策：异常**不阻断同步，只告警** ——
        数据完整性优先于状态纯洁性。
        """
        return self._stmt().where(Order.anomaly_flag.is_(True))

    def _q_orders_by_shop(self, shop_id: int) -> Select:
        return self._stmt().where(Order.shop_id == shop_id)

    # ---------- 明细与费用 ----------

    def _q_items_by_order(self, order_id: int) -> Select:
        return self._stmt().where(OrderItem.order_id == order_id)

    def _q_fees_by_order(self, order_id: int) -> Select:
        return self._stmt().where(OrderFee.order_id == order_id)

    def _q_fees_by_shop_type(self, shop_id: int, fee_type: str) -> Select:
        return (
            self._stmt().where(OrderFee.shop_id == shop_id).where(OrderFee.fee_type == fee_type)
        )

    # ---------- 售后 ----------

    def _q_refund_by_platform_id(self, platform_refund_id: str) -> Select:
        return self._stmt().where(Refund.platform_refund_id == platform_refund_id)

    def _q_refunds_by_order(self, order_id: int) -> Select:
        return self._stmt().where(Refund.order_id == order_id)

    def _q_refunds_open(self) -> Select:
        """未完结的退款单。

        终态是 REJECTED / CLOSED（见 `RefundStatus.terminal`），
        其余都还需要跟进 —— 挂在"待处理"里没人管会变成客诉。
        """
        return self._stmt().where(
            Refund.status.notin_(
                [RefundStatus.REJECTED.value, RefundStatus.CLOSED.value]
            )
        )

    def _q_return_by_platform_id(self, platform_return_id: str) -> Select:
        return self._stmt().where(ReturnRecord.platform_return_id == platform_return_id)

    def _q_returns_by_shop(self, shop_id: int) -> Select:
        return self._stmt().where(ReturnRecord.shop_id == shop_id)


class OrderRepository(BaseRepository[Order], _OrderQueries):
    """订单仓储。"""

    model = Order

    async def get_by_platform_order_id(
        self, shop_id: int, platform_order_id: str
    ) -> Order | None:
        """按幂等键查订单（同步任务判断"新建还是更新"）。"""
        stmt = self._q_order_by_platform_id(shop_id, platform_order_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_created_since(
        self,
        since: datetime,
        *,
        until: datetime | None = None,
        shop_id: int | None = None,
        limit: int | None = None,
    ) -> list[Order]:
        """增量轨道 1：拉取指定时间之后**新下单**的订单。"""
        stmt = self._q_orders_by_order_time(since, until)
        if shop_id is not None:
            stmt = stmt.where(Order.shop_id == shop_id)
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_updated_since(
        self,
        since: datetime,
        *,
        shop_id: int | None = None,
        limit: int | None = None,
    ) -> list[Order]:
        """增量轨道 2：拉取指定时间之后**被更新过**的订单。

        与 `list_created_since` 配套使用，缺一条就会漏状态变化。
        """
        stmt = self._q_orders_by_updated(since)
        if shop_id is not None:
            stmt = stmt.where(Order.shop_id == shop_id)
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_status(
        self, order_status: OrderStatus | str, *, limit: int | None = None
    ) -> list[Order]:
        value = order_status.value if isinstance(order_status, OrderStatus) else order_status
        stmt = self._q_orders_by_status(value)
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_anomalies(self, *, shop_id: int | None = None) -> list[Order]:
        """异常订单列表（供告警与人工核查）。"""
        stmt = self._q_orders_anomaly()
        if shop_id is not None:
            stmt = stmt.where(Order.shop_id == shop_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_shop(self, shop_id: int, *, limit: int | None = None) -> list[Order]:
        stmt = self._q_orders_by_shop(shop_id)
        stmt = self._apply_ordering(stmt, "-order_time")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class OrderItemRepository(BaseRepository[OrderItem], _OrderQueries):
    """订单明细仓储。"""

    model = OrderItem

    async def list_by_order(self, order_id: int) -> list[OrderItem]:
        return list((await self.session.execute(self._q_items_by_order(order_id))).scalars().all())


class OrderFeeRepository(BaseRepository[OrderFee], _OrderQueries):
    """订单费用仓储（佣金、运费、广告分摊等）。"""

    model = OrderFee

    async def list_by_order(self, order_id: int) -> list[OrderFee]:
        return list((await self.session.execute(self._q_fees_by_order(order_id))).scalars().all())

    async def list_by_type(self, shop_id: int, fee_type: str) -> list[OrderFee]:
        stmt = self._q_fees_by_shop_type(shop_id, fee_type)
        return list((await self.session.execute(stmt)).scalars().all())


class RefundRepository(BaseRepository[Refund], _OrderQueries):
    """退款仓储。"""

    model = Refund

    async def get_by_platform_refund_id(self, platform_refund_id: str) -> Refund | None:
        stmt = self._q_refund_by_platform_id(platform_refund_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_by_order(self, order_id: int) -> list[Refund]:
        return list((await self.session.execute(self._q_refunds_by_order(order_id))).scalars().all())

    async def list_open(self, *, limit: int | None = None) -> list[Refund]:
        """未完结的退款单（待人工或自动处理）。"""
        stmt = self._q_refunds_open()
        stmt = self._apply_ordering(stmt, "created_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class ReturnRecordRepository(BaseRepository[ReturnRecord], _OrderQueries):
    """退货记录仓储。"""

    model = ReturnRecord

    async def get_by_platform_return_id(self, platform_return_id: str) -> ReturnRecord | None:
        stmt = self._q_return_by_platform_id(platform_return_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_by_shop(self, shop_id: int, *, limit: int | None = None) -> list[ReturnRecord]:
        stmt = self._apply_ordering(self._q_returns_by_shop(shop_id), "-created_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


# ============================================================
# 同步版本（Celery worker 用）
# ============================================================


class SyncOrderRepository(SyncBaseRepository[Order], _OrderQueries):
    model = Order

    def get_by_platform_order_id(self, shop_id: int, platform_order_id: str) -> Order | None:
        stmt = self._q_order_by_platform_id(shop_id, platform_order_id)
        return self.session.execute(stmt).scalars().first()

    def list_created_since(
        self,
        since: datetime,
        *,
        until: datetime | None = None,
        shop_id: int | None = None,
        limit: int | None = None,
    ) -> list[Order]:
        stmt = self._q_orders_by_order_time(since, until)
        if shop_id is not None:
            stmt = stmt.where(Order.shop_id == shop_id)
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list(self.session.execute(stmt).scalars().all())

    def list_updated_since(
        self, since: datetime, *, shop_id: int | None = None, limit: int | None = None
    ) -> list[Order]:
        stmt = self._q_orders_by_updated(since)
        if shop_id is not None:
            stmt = stmt.where(Order.shop_id == shop_id)
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list(self.session.execute(stmt).scalars().all())

    def list_anomalies(self, *, shop_id: int | None = None) -> list[Order]:
        stmt = self._q_orders_anomaly()
        if shop_id is not None:
            stmt = stmt.where(Order.shop_id == shop_id)
        return list(self.session.execute(stmt).scalars().all())


class SyncOrderItemRepository(SyncBaseRepository[OrderItem], _OrderQueries):
    model = OrderItem

    def list_by_order(self, order_id: int) -> list[OrderItem]:
        return list(self.session.execute(self._q_items_by_order(order_id)).scalars().all())


class SyncOrderFeeRepository(SyncBaseRepository[OrderFee], _OrderQueries):
    model = OrderFee

    def list_by_order(self, order_id: int) -> list[OrderFee]:
        return list(self.session.execute(self._q_fees_by_order(order_id)).scalars().all())

    def list_by_type(self, shop_id: int, fee_type: str) -> list[OrderFee]:
        return list(
            self.session.execute(self._q_fees_by_shop_type(shop_id, fee_type)).scalars().all()
        )


class SyncRefundRepository(SyncBaseRepository[Refund], _OrderQueries):
    model = Refund

    def get_by_platform_refund_id(self, platform_refund_id: str) -> Refund | None:
        return self.session.execute(self._q_refund_by_platform_id(platform_refund_id)).scalars().first()

    def list_open(self, *, limit: int | None = None) -> list[Refund]:
        stmt = self._apply_ordering(self._q_refunds_open(), "created_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list(self.session.execute(stmt).scalars().all())


class SyncReturnRecordRepository(SyncBaseRepository[ReturnRecord], _OrderQueries):
    model = ReturnRecord

    def get_by_platform_return_id(self, platform_return_id: str) -> ReturnRecord | None:
        return self.session.execute(self._q_return_by_platform_id(platform_return_id)).scalars().first()
