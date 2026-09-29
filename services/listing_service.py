"""上架服务。

**Phase 1 交付边界**（TDD-01 §5.3）：

    上架状态机完整实现，但流程**终止在「待审批」**，不自动提交到平台。
    原因：平台授权尚未到位，自动提交只会产生"以为上架了、其实没有"的错觉，
    比不做更危险。

状态流转：

    DRAFT → VALIDATING → PENDING_APPROVAL → QUEUED → SUBMITTED
                                                      → PROCESSING → ACTIVE
                                                                   → PARTIAL_ACTIVE

**PARTIAL_ACTIVE 是独立状态**（TDD-04 §2）：
    多变体部分成功时商品已在售但不完整。既不能归为 ACTIVE（会漏监控库存），
    也不能归为 FAILED（会被重试覆盖掉已成功的变体）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import ListingStatus
from core.exceptions import BusinessError, NotFoundError
from core.models import ProductListing
from domain.state_machines import ListingContext, ListingStateMachine
from repositories.product import ProductListingRepository
from services.base import ServiceContext, publish_event, record_transition

__all__ = ["ListingService"]


class ListingService:
    """上架编排服务。"""

    def __init__(self, session: AsyncSession, ctx: ServiceContext) -> None:
        self.session = session
        self.ctx = ctx
        self.listings = ProductListingRepository(session, tenant_id=ctx.tenant_id)

    # ========================================================
    # 校验
    # ========================================================

    async def validate(self, listing_id: int, *, missing_fields: tuple[str, ...] = ()) -> ProductListing:
        """校验刊登信息（DRAFT → VALIDATING → 下一步）。

        校验不通过会退回 DRAFT 并抛出业务异常 ——
        带着缺失字段继续走下去，失败会发生在平台侧，
        那时的错误信息远不如这里清晰。
        """
        listing = await self._get(listing_id)
        await self._apply(listing, event="validate", context=ListingContext(roles=self.ctx.roles))

        if missing_fields:
            await self._apply(
                listing,
                event="validation_failed",
                context=ListingContext(
                    roles=self.ctx.roles,
                    missing_required_fields=missing_fields,
                ),
            )
            raise BusinessError(
                f"刊登缺少必填属性：{', '.join(missing_fields)}",
                code="LISTING_SCHEMA_MISMATCH",
                action="请在商品编辑页补充缺失属性后重新提交",
                context={"listing_id": listing_id, "missing": list(missing_fields)},
            )

        await self._apply(
            listing,
            event="validation_passed",
            context=self._context_for(listing),
        )
        return listing

    # ========================================================
    # 审批衔接
    # ========================================================

    async def mark_queued(self, listing_id: int) -> ProductListing:
        """审批通过后进入待提交队列（PENDING_APPROVAL → QUEUED）。

        由审批事件的消费者调用，**不在这里直接提交平台** ——
        提交是异步长流程（部分平台要轮询 processingReport），
        放在审批事务里会拖垮事务。
        """
        listing = await self._get(listing_id)
        await self._apply(listing, event="approve", context=self._context_for(listing))

        await publish_event(
            self.session,
            self.ctx,
            event_type="listing.queued",
            aggregate_type="ProductListing",
            aggregate_id=listing.id,
            payload={"shop_id": listing.shop_id, "product_id": listing.product_id},
        )
        return listing

    async def mark_rejected(self, listing_id: int, *, reason: str) -> ProductListing:
        """审批驳回（PENDING_APPROVAL → REJECTED）。"""
        listing = await self._get(listing_id)
        await self._apply(
            listing,
            event="reject",
            reason=reason,
            context=self._context_for(listing),
        )
        return listing

    # ========================================================
    # 平台结果回写
    # ========================================================

    async def apply_platform_result(
        self,
        listing_id: int,
        *,
        succeeded: int,
        total: int,
        error: str | None = None,
    ) -> ProductListing:
        """回写平台提交结果（PROCESSING → ACTIVE / PARTIAL_ACTIVE / FAILED）。

        三态判定：
            全部成功 → ACTIVE
            部分成功 → PARTIAL_ACTIVE（**不能归为 ACTIVE，也不能归为 FAILED**）
            全部失败 → FAILED
        """
        listing = await self._get(listing_id)
        context = self._context_for(listing)
        context.variant_total = max(total, 1)
        context.variant_succeeded = succeeded

        if succeeded <= 0:
            event = "all_failed"
        elif succeeded < total:
            event = "partially_succeeded"
        else:
            event = "all_succeeded"

        await self._apply(listing, event=event, reason=error, context=context)
        listing.last_error_msg = (error or "")[:1000] if error else None
        await self.session.flush()
        return listing

    # ========================================================
    # 内部
    # ========================================================

    async def _get(self, listing_id: int) -> ProductListing:
        listing = await self.listings.get(listing_id)
        if listing is None:
            raise NotFoundError("刊登不存在", resource="ProductListing", resource_id=listing_id)
        return listing

    def _context_for(self, listing: ProductListing) -> ListingContext:
        """构造状态机判定上下文。"""
        return ListingContext(
            roles=self.ctx.roles,
            has_edit_permission=bool(self.ctx.roles & {"ops", "admin"}),
            requires_approval=True,
        )

    async def _apply(
        self,
        listing: ProductListing,
        *,
        event: str,
        context: ListingContext,
        reason: str | None = None,
    ) -> None:
        """执行状态转换并留痕。"""
        result = ListingStateMachine.resolve_or_raise(listing.listing_status, event, context)
        if not result.changed:
            raise BusinessError(
                f"刊登当前状态 {listing.listing_status} 不允许执行 {event}",
                code="LISTING_STATE_UNCHANGED",
            )

        await record_transition(
            self.session,
            self.ctx,
            machine=ListingStateMachine.machine_name,
            entity_type="ProductListing",
            entity_id=listing.id,
            from_state=result.from_state,
            to_state=result.to_state,
            event=event,
            reason=reason,
        )
        listing.listing_status = result.to_state
        await self.session.flush()
