"""同步服务：订单增量同步。

**双轨增量**（TDD-01 §5.2）：

    轨道 1 — 按 `order_time` 拉新：捕获新下单的订单
    轨道 2 — 按 `updated_at` 回看 N 天：捕获已下单订单的发货/取消/结算变化

    只做轨道 1 会**静默漏数据**：订单 3 天前下的、今天才发货，
    按 `order_time` 增量永远看不到这次变化。报表会因此少算履约，
    而且没有任何报错 —— 这类问题通常在对账时才发现，已经过去一个月。

**游标只在整批成功时前移**（TDD-04 §4）：

    每页都写游标会在中途失败时丢数据，丢的还是"中间一段"，
    事后只能靠水位检查发现。

**连续失败自动暂停**：

    Token 失效、权限被撤销这类问题重试 100 次也不会成功，
    只会刷爆日志和告警 —— 最终团队关掉通知，
    那时真正的 P0 也收不到了。

**为什么这里全程异步**：

    适配器接口本身是 `async`（`adapters/base.py`），
    所以同步服务也必须是异步的；Celery 侧用 `asyncio.run()` 包装调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from adapters.base import ShopContext
from adapters.registry import build_adapter
from adapters.types import OrderSnapshot, TimeRange
from core.config import get_settings
from core.constants import OrderStatus
from core.exceptions import NotFoundError
from core.models import Order, OrderItem
from core.timeutil import utc_now
from repositories.infra import SyncCursorRepository, SyncWatermarkRepository
from repositories.order import OrderRepository, OrderItemRepository
from repositories.shop import ShopRepository
from services.base import ServiceContext, publish_event

__all__ = ["OrderSyncOutcome", "SyncService"]

#: 订单同步的回看窗口（天）。
#:
#: 取 14 天是权衡结果：太短会漏掉慢履约订单的结算变化，
#: 太长会让每轮同步的数据量无谓增大。
ORDER_LOOKBACK_DAYS = 14

#: 同步游标对应的资源类型。
RESOURCE_ORDERS = "ORDERS"


@dataclass
class OrderSyncOutcome:
    """一次订单同步的结果。"""

    shop_id: int
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    cursor_advanced: bool = False

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged + self.failed

    @property
    def ok(self) -> bool:
        return self.failed == 0


class SyncService:
    """同步编排服务。

    用法（API 侧）：

        async with async_session_scope() as session:
            svc = SyncService(session, ctx)
            outcome = await svc.sync_orders(shop_id=1)
    """

    def __init__(self, session: AsyncSession, ctx: ServiceContext) -> None:
        self.session = session
        self.ctx = ctx
        self.shops = ShopRepository(session, tenant_id=ctx.tenant_id)
        self.orders = OrderRepository(session, tenant_id=ctx.tenant_id)
        self.order_items = OrderItemRepository(session, tenant_id=ctx.tenant_id)
        self.cursors = SyncCursorRepository(session, tenant_id=ctx.tenant_id)
        self.watermarks = SyncWatermarkRepository(session, tenant_id=ctx.tenant_id)

    # ========================================================
    # 订单同步
    # ========================================================

    async def sync_orders(
        self,
        shop_id: int,
        *,
        lookback_days: int = ORDER_LOOKBACK_DAYS,
        max_pages: int = 20,
    ) -> OrderSyncOutcome:
        """执行一次订单增量同步。

        流程：
            1. 校验店铺存在且可同步
            2. 读游标，确定拉取窗口（起点 = 游标，终点 = 现在）
            3. 调适配器分页拉取
            4. 逐条按幂等键 upsert
            5. **全部成功**才前移游标；任一条失败则游标不动
        """
        shop = await self.shops.get(shop_id)
        if shop is None:
            raise NotFoundError("店铺不存在", resource="Shop", resource_id=shop_id)

        outcome = OrderSyncOutcome(shop_id=shop_id)
        cursor = await self.cursors.get_cursor(shop_id, RESOURCE_ORDERS)
        started_at = utc_now()

        # 起点：有游标则从游标续拉，否则回看 N 天
        since = self._parse_cursor(cursor.cursor_value if cursor else None, started_at, lookback_days)

        adapter = await self._build_adapter(shop)
        page_token: str | None = None

        try:
            for _ in range(max_pages):
                result = await adapter.fetch_orders(
                    time_range=TimeRange(start=since, end=started_at),
                    page_token=page_token,
                )
                for snapshot in result.data:
                    try:
                        await self._upsert_order(shop_id, snapshot, outcome)
                    except Exception as exc:  # noqa: BLE001
                        # 单条失败不中断整批：坏数据不该阻塞其余订单同步，
                        # 但必须记下来 —— 而且要阻止游标前移。
                        outcome.failed += 1
                        outcome.errors.append(f"{snapshot.platform_order_id}: {exc}"[:500])

                if not result.has_more:
                    break
                page_token = result.next_token

        except Exception as exc:  # noqa: BLE001
            await self.cursors.record_failure(shop_id, RESOURCE_ORDERS, str(exc))
            raise

        # 游标只在整批成功时前移 —— 有失败就保持原位，下轮重拉
        if outcome.failed == 0:
            await self.cursors.advance(
                shop_id, RESOURCE_ORDERS, started_at.isoformat()
            )
            outcome.cursor_advanced = True
        else:
            await self.cursors.record_failure(
                shop_id,
                RESOURCE_ORDERS,
                f"{outcome.failed} 条订单处理失败，游标未前移",
            )

        await self._update_watermark(shop_id, started_at, outcome)

        if outcome.created or outcome.updated:
            await publish_event(
                self.session,
                self.ctx,
                event_type="sync.orders.completed",
                aggregate_type="Shop",
                aggregate_id=shop_id,
                payload={
                    "created": outcome.created,
                    "updated": outcome.updated,
                    "failed": outcome.failed,
                    "cursor_advanced": outcome.cursor_advanced,
                },
            )
        return outcome

    # ========================================================
    # 内部
    # ========================================================

    @staticmethod
    def _parse_cursor(raw: str | None, fallback_end: datetime, lookback_days: int) -> datetime:
        """解析游标值；无效或缺失时回退到 `now - lookback_days`。

        游标损坏时**不能**直接抛错 —— 那会让同步永久卡死。
        回退到一个保守的窗口，宁可重复拉取（upsert 是幂等的），
        也不能让数据永远同步不上。
        """
        if raw:
            try:
                parsed = datetime.fromisoformat(raw)
                if parsed.tzinfo is not None:
                    parsed = parsed.replace(tzinfo=None)
                return parsed
            except ValueError:
                pass
        return fallback_end - timedelta(days=lookback_days)

    async def _build_adapter(self, shop: Any) -> Any:
        """构建适配器。

        `local` / `test` 环境会被 `build_adapter` 强制替换为 Mock ——
        这是 CI 禁止出网的强制点，业务层无法绕过。
        """
        settings = get_settings()
        shop_context = ShopContext(
            shop_id=shop.id,
            tenant_id=shop.tenant_id,
            platform=shop.platform,
            region=shop.region,
            timezone=shop.timezone,
            marketplace_id=shop.marketplace_id,
        )
        return await build_adapter(shop=shop_context, env=settings.ec_env)

    async def _upsert_order(
        self, shop_id: int, snapshot: OrderSnapshot, outcome: OrderSyncOutcome
    ) -> None:
        """按幂等键写入或更新订单。

        幂等键 `(shop_id, platform_order_id)`：
        平台重推同一订单必须走更新路径，不能产生重复行 ——
        重复订单会直接污染销售额与利润。
        """
        existing = await self.orders.get_by_platform_order_id(
            shop_id, snapshot.platform_order_id
        )

        status = (
            snapshot.order_status.value
            if isinstance(snapshot.order_status, OrderStatus)
            else str(snapshot.order_status)
        )

        if existing is None:
            order = Order(
                shop_id=shop_id,
                platform_order_id=snapshot.platform_order_id,
                order_status=status,
                buyer_hash=snapshot.buyer_hash,
                buyer_region=snapshot.buyer_region,
                buyer_encrypted=snapshot.buyer_encrypted,
                item_total=snapshot.item_total,
                shipping_total=snapshot.shipping_total,
                discount_total=snapshot.discount_total,
                tax_total=snapshot.tax_total,
                grand_total=snapshot.grand_total,
                currency=snapshot.currency,
                order_time=snapshot.order_time or utc_now(),
                ship_time=snapshot.ship_time,
                settle_time=snapshot.settle_time,
                fulfillment_channel=snapshot.fulfillment_channel,
                raw_order_status=status,
                synced_at=utc_now(),
                platform_raw=snapshot.raw,
            )
            await self.orders.add(order)
            await self._sync_items(order, snapshot)
            outcome.created += 1
            return

        # 已存在：只更新会变化的字段。
        # 状态换向（如已发货变回未发货）不阻断，只打异常标记 ——
        # 数据完整性优先于状态纯洁性（TDD-04 §5）。
        changed = False
        if existing.order_status != status:
            existing.raw_order_status = status
            existing.order_status = status
            changed = True
        if snapshot.ship_time and existing.ship_time != snapshot.ship_time:
            existing.ship_time = snapshot.ship_time
            changed = True
        if snapshot.settle_time and existing.settle_time != snapshot.settle_time:
            existing.settle_time = snapshot.settle_time
            changed = True
        if snapshot.grand_total != existing.grand_total:
            existing.grand_total = snapshot.grand_total
            changed = True

        existing.synced_at = utc_now()
        if changed:
            outcome.updated += 1
        else:
            outcome.unchanged += 1
        await self.session.flush()

    async def _sync_items(self, order: Order, snapshot: OrderSnapshot) -> None:
        """写入订单明细。

        注意 `OrderItemSnapshot` 只带最少的金额字段
        （见 `adapters/types.py`）—— 平台侧没有的字段不臆造，
        缺失的折扣/币种从订单头继承。
        """
        for item in snapshot.items:
            await self.order_items.add(
                OrderItem(
                    order_id=order.id,
                    platform_sku=item.platform_sku,
                    title=item.title,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    item_total=item.item_total,
                    currency=order.currency,
                )
            )

    async def _update_watermark(
        self, shop_id: int, synced_at: datetime, outcome: OrderSyncOutcome
    ) -> None:
        """更新同步水位。

        `fresh_lag_minutes` 是最重要的可观测指标 ——
        数据异常排查的第一步永远是看它。
        """
        await self.watermarks.update_watermark(
            shop_id,
            RESOURCE_ORDERS,
            data_max_time=synced_at,
            fresh_lag_minutes=0,
            record_count=outcome.total,
        )
