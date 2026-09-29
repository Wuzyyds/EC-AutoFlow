"""Mock 适配器（Phase 1 核心交付，ADR-005）。

**这不是"假数据占位"**，它承担四个真实职责：

1. **锁定接口契约**
   Mock 与 Amazon 适配器实现同一接口，接口错了会立刻暴露。

2. **测试真实沙箱测不了的异常分支**
   Mock 能模拟 429、超时、部分成功、字段缺失、结构漂移，
   真实沙箱做不到这些。

3. **验证数据模型**
   用真实响应结构建表，避免等授权后大改 DDL。

4. **可演示**
   能跑通完整业务链路给评审看。

关键要求：
    fixture 必须是**从官方文档或真实响应中提取的真实结构**，不能自己编。
    第一版用官方文档示例，拿到授权后用真实响应替换，
    并执行"影子比对"逐字段验证（tests/contract/test_shadow_compare.py）。

故意保留的不支持能力：
    REFUND_CREATE 与 MESSAGE_READ 声明为 UNSUPPORTED，
    以模拟 Amazon 的行为，验证"生成人工待办"的降级路径。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from adapters.base import (
    NotificationCallback,
    PlatformAdapter,
    PlatformClient,
    ShopContext,
)
from adapters.capabilities import Capability, CapabilityMatrix
from adapters.errors import (
    AdapterError,
    ErrorCategory,
    PartialSuccessError,
    SchemaDriftError,
    ValidationError,
    FieldError,
)
from adapters.registry import register
from adapters.types import (
    AdapterResult,
    AsyncSubmission,
    BatchResult,
    CategoryNode,
    CategorySchema,
    EligibilityResult,
    FeeRecord,
    HealthStatus,
    IdentifierType,
    InventorySnapshot,
    InventoryUpdate,
    ListingDraft,
    ListingPatch,
    ListingRef,
    ListingSnapshot,
    MessageRecord,
    OrderItemSnapshot,
    OrderSnapshot,
    PriceUpdate,
    RefundRecord,
    ReportPayload,
    ReportType,
    ReturnRecord,
    SettlementBatch,
    ShipmentConfirmation,
    SubmissionStatus,
    SubscriptionResult,
    TimeRange,
)
from core.constants import OrderStatus, Platform
from core.money import ZERO, money
from core.security.masking import pseudonymize
from core.timeutil import utc_now

__all__ = ["MockAdapter", "MockConfig"]


@dataclass
class MockConfig:
    """Mock 行为配置 —— 故障注入的开关集合。

    用法（测试里）：
        # 模拟连续限流后恢复
        cfg = MockConfig(rate_limit_times=3, latency_ms=10)
        adapter = MockAdapter(shop, client, cfg)

        # 模拟批量部分失败
        cfg = MockConfig(partial_failure_count=3)
    """

    #: 每次调用的模拟延迟（毫秒）
    latency_ms: int = 0

    #: 随机错误率（0–1）
    error_rate: float = 0.0

    #: 强制指定错误分类（一旦设置，所有调用都抛此错误）
    force_error: ErrorCategory | None = None

    #: 前 N 次调用抛限流错误（用于测试退避策略）
    rate_limit_times: int = 0

    #: 批量操作中失败的行数（用于测试逐行处理）
    partial_failure_count: int = 0

    #: 是否返回结构异常的数据（用于测试 SchemaDriftError）
    inject_schema_drift: bool = False

    #: 分页大小
    page_size: int = 20

    #: 异步提交需要轮询几次才完成
    submission_polls_until_done: int = 2

    #: 随机种子（保证测试可复现）
    seed: int = 42

    #: 是否启用确定性模式（关闭随机性，用于断言）
    deterministic: bool = True


@register(Platform.MOCK)
class MockAdapter(PlatformAdapter):
    """Mock 适配器。

    数据是**确定性生成**的（默认 seed=42），保证测试可复现 ——
    随机数据会让"昨天通过今天失败"这种问题变得无法排查。
    """

    platform = Platform.MOCK.value
    display_name = "Mock（测试）"

    def __init__(
        self,
        shop: ShopContext,
        client: PlatformClient | None = None,
        config: MockConfig | None = None,
    ) -> None:
        super().__init__(shop=shop, client=client)  # type: ignore[arg-type]
        self.config = config or MockConfig()
        self._rng = random.Random(self.config.seed)

        #: 调用计数（测试断言用）
        self.call_counts: dict[str, int] = {}

        #: 异步提交状态
        self._submissions: dict[str, dict[str, Any]] = {}

        #: 已创建的 listing（内存态，供 fetch 回读）
        self._listings: dict[str, ListingSnapshot] = {}

    # ========================================================
    # 元信息
    # ========================================================

    @classmethod
    def capabilities(cls) -> CapabilityMatrix:
        from adapters.capabilities import MOCK_CAPABILITIES

        return CapabilityMatrix(Platform.MOCK.value, MOCK_CAPABILITIES)

    async def health_check(self) -> HealthStatus:
        self._tick("health_check")
        await self._maybe_fail("health_check")
        return HealthStatus(
            ok=True,
            platform=self.platform,
            token_expires_at=utc_now() + timedelta(days=30),
            quota_remaining_ratio=Decimal("0.85"),
            message="Mock 适配器运行正常",
            latency_ms=self.config.latency_ms,
        )

    # ========================================================
    # 内部：故障注入
    # ========================================================

    def _tick(self, method: str) -> int:
        """记录调用次数并返回当前次数。"""
        count = self.call_counts.get(method, 0) + 1
        self.call_counts[method] = count
        return count

    async def _maybe_fail(self, method: str) -> None:
        """按配置注入故障。

        执行顺序有意为之：先检查限流次数，再检查强制错误，
        最后才是随机错误率 —— 这样测试可以用最明确的方式控制行为。
        """
        if self.config.latency_ms:
            await asyncio.sleep(self.config.latency_ms / 1000)

        count = self.call_counts.get(method, 0)

        # 1. 前 N 次限流（用于测试退避与最终成功）
        if self.config.rate_limit_times and count <= self.config.rate_limit_times:
            raise AdapterError(
                category=ErrorCategory.RATE_LIMITED,
                message=f"Mock 注入限流（第 {count} 次）",
                platform=self.platform,
                raw_code="Throttled",
                http_status=429,
                retry_after=1,
                context={"method": method},
            )

        # 2. 强制错误
        if self.config.force_error is not None:
            raise AdapterError(
                category=self.config.force_error,
                message=f"Mock 注入错误：{self.config.force_error.label}",
                platform=self.platform,
                raw_code="MOCK_INJECTED",
                context={"method": method},
            )

        # 3. 随机错误率（确定性模式下用固定序列）
        if self.config.error_rate > 0 and self._rng.random() < self.config.error_rate:
            raise AdapterError(
                category=ErrorCategory.SERVER_ERROR,
                message="Mock 随机注入的服务端错误",
                platform=self.platform,
                http_status=500,
                context={"method": method},
            )

    # ========================================================
    # 类目
    # ========================================================

    async def fetch_category_tree(
        self, *, parent_id: str | None = None, force_refresh: bool = False
    ) -> AdapterResult[list[CategoryNode]]:
        self._tick("fetch_category_tree")
        await self._maybe_fail("fetch_category_tree")

        roots = [
            CategoryNode(category_id="home", name="家居用品", is_leaf=False),
            CategoryNode(category_id="electronics", name="消费电子", is_leaf=False),
            CategoryNode(category_id="apparel", name="服饰鞋包", is_leaf=False),
        ]
        if parent_id is None:
            return AdapterResult(data=roots, raw={"mock": True, "level": "root"})

        children = {
            "home": [CategoryNode(category_id="home_kitchen", name="厨房用品", parent_id="home", is_leaf=True)],
            "electronics": [
                CategoryNode(category_id="elec_audio", name="音频设备", parent_id="electronics", is_leaf=True)
            ],
            "apparel": [CategoryNode(category_id="app_women", name="女装", parent_id="apparel", is_leaf=True)],
        }
        return AdapterResult(
            data=children.get(parent_id, []),
            raw={"mock": True, "parent_id": parent_id},
        )

    async def fetch_category_schema(
        self, *, category_id: str, product_type: str | None = None
    ) -> AdapterResult[CategorySchema]:
        self._tick("fetch_category_schema")
        await self._maybe_fail("fetch_category_schema")

        schema = CategorySchema(
            category_id=category_id,
            schema_version="mock-v1",
            product_type=product_type or "PRODUCT",
            required_fields=["title", "brand", "price", "quantity"],
            attributes=[
                {"name": "title", "type": "string", "max_length": 200, "required": True},
                {"name": "brand", "type": "string", "required": True},
                {"name": "price", "type": "decimal", "required": True},
                {"name": "quantity", "type": "integer", "required": True},
                {"name": "color", "type": "enum", "values": ["black", "white", "blue"]},
            ],
            raw={"mock": True, "category_id": category_id},
        )
        return AdapterResult(data=schema, raw=schema.raw)

    async def check_listing_eligibility(
        self,
        *,
        category_id: str,
        brand: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> AdapterResult[EligibilityResult]:
        self._tick("check_listing_eligibility")
        await self._maybe_fail("check_listing_eligibility")

        # Mock 规则：受限类目需审批
        restricted = {"grocery", "health", "baby"}
        if category_id in restricted:
            result = EligibilityResult(
                eligible=True,
                reasons=[],
                requires_approval_for=["类目审核"],
                raw={"mock": True},
            )
        else:
            result = EligibilityResult(eligible=True, reasons=[], raw={"mock": True})
        return AdapterResult(data=result, raw=result.raw)

    # ========================================================
    # Listing 读
    # ========================================================

    async def fetch_listings(
        self, *, identifiers: list[str], identifier_type: IdentifierType
    ) -> AdapterResult[list[ListingSnapshot]]:
        self._tick("fetch_listings")
        await self._maybe_fail("fetch_listings")

        snapshots: list[ListingSnapshot] = []
        for ident in identifiers:
            cached = self._listings.get(ident)
            if cached is not None:
                snapshots.append(cached)
                continue
            snapshots.append(
                ListingSnapshot(
                    platform_sku=ident,
                    platform_item_id=f"ITEM-{ident}",
                    status="ACTIVE",
                    title=f"Mock 商品 {ident}",
                    price=money("19.99"),
                    currency="USD",
                    quantity=100,
                    category_id="home_kitchen",
                    updated_at=utc_now(),
                    raw={"mock": True, "sku": ident},
                )
            )
        return AdapterResult(
            data=snapshots,
            raw={"mock": True, "count": len(snapshots)},
            partial=False,
        )

    async def search_listings(
        self, *, filters: dict[str, Any] | None = None, page_token: str | None = None
    ) -> AdapterResult[list[ListingSnapshot]]:
        self._tick("search_listings")
        await self._maybe_fail("search_listings")

        page = int(page_token or "1")
        total_pages = 3
        size = self.config.page_size

        items = [
            ListingSnapshot(
                platform_sku=f"SKU-{page}-{i:03d}",
                status="ACTIVE",
                title=f"Mock 商品 {page}-{i}",
                price=money("9.99"),
                currency="USD",
                quantity=50,
                raw={"mock": True},
            )
            for i in range(size)
        ]
        has_more = page < total_pages
        return AdapterResult(
            data=items,
            next_token=str(page + 1) if has_more else None,
            has_more=has_more,
            raw={"mock": True, "page": page},
        )

    # ========================================================
    # Listing 写（默认 ASYNC，用于测试轮询链路）
    # ========================================================

    async def create_listing(
        self,
        *,
        draft: ListingDraft,
        idempotency_key: str,
        validate_only: bool = False,
    ) -> AdapterResult[ListingSnapshot] | AdapterResult[AsyncSubmission]:
        self._tick("create_listing")
        await self._maybe_fail("create_listing")

        # 校验：必填字段
        missing = [
            f
            for f, v in (("title", draft.title), ("category_id", draft.category_id))
            if not v
        ]
        if missing:
            raise ValidationError(
                category=ErrorCategory.VALIDATION_FAILED,
                message=f"缺少必填字段：{'、'.join(missing)}",
                platform=self.platform,
                raw_code="InvalidInput",
                field_errors=[FieldError(field=f, message="必填字段为空") for f in missing],
            )

        # 注入结构漂移（测试 SchemaDriftError 的触发）
        if self.config.inject_schema_drift:
            raise SchemaDriftError(
                category=ErrorCategory.SCHEMA_DRIFT,
                message="Mock 注入的结构漂移：响应缺少 expected 字段",
                platform=self.platform,
                expected_shape=["submission_id"],
                actual_sample={"unexpected": "shape"},
            )

        if validate_only:
            return AdapterResult(
                data=ListingSnapshot(
                    platform_sku=draft.internal_sku,
                    status="VALIDATED",
                    title=draft.title,
                    raw={"mock": True, "validate_only": True},
                ),
                raw={"mock": True},
                warnings=["Mock 使用本地 schema 校验（平台不支持 validate_only）"],
            )

        submission_id = f"SUB-{idempotency_key[:12]}"
        self._submissions[submission_id] = {
            "polls": 0,
            "total": self.config.submission_polls_until_done,
            "sku": draft.internal_sku,
            "draft": draft,
        }

        return AdapterResult(
            data=AsyncSubmission(
                submission_id=submission_id,
                expected_ready_at=utc_now() + timedelta(seconds=30),
                poll_hint_seconds=5,
                raw={"mock": True},
            ),
            raw={"mock": True, "submission_id": submission_id},
            request_id=f"REQ-{submission_id}",
        )

    async def update_listing(
        self,
        *,
        listing_ref: ListingRef,
        patch: ListingPatch,
        idempotency_key: str,
    ) -> AdapterResult[ListingSnapshot] | AdapterResult[AsyncSubmission]:
        self._tick("update_listing")
        await self._maybe_fail("update_listing")

        sku = listing_ref.platform_sku or listing_ref.platform_item_id or "UNKNOWN"
        snap = ListingSnapshot(
            platform_sku=sku,
            status="ACTIVE",
            title=patch.title or f"Mock 商品 {sku}",
            price=patch.price or money("19.99"),
            currency="USD",
            quantity=patch.quantity if patch.quantity is not None else 100,
            updated_at=utc_now(),
            raw={"mock": True, "patch": {"price": str(patch.price) if patch.price else None}},
        )
        self._listings[sku] = snap
        return AdapterResult(data=snap, raw=snap.raw)

    async def delete_listing(
        self, *, listing_ref: ListingRef, idempotency_key: str
    ) -> AdapterResult[None] | AdapterResult[AsyncSubmission]:
        self._tick("delete_listing")
        await self._maybe_fail("delete_listing")

        sku = listing_ref.platform_sku or listing_ref.platform_item_id or "UNKNOWN"
        self._listings.pop(sku, None)
        return AdapterResult(data=None, raw={"mock": True, "deleted": sku})

    async def update_price(
        self, *, updates: list[PriceUpdate], idempotency_key: str
    ) -> AdapterResult[BatchResult] | AdapterResult[AsyncSubmission]:
        self._tick("update_price")
        await self._maybe_fail("update_price")

        total = len(updates)
        fail_count = min(self.config.partial_failure_count, total)
        succeeded = total - fail_count

        result = BatchResult(
            total=total,
            succeeded=succeeded,
            failed=fail_count,
            success_ids=[
                u.listing_ref.platform_sku or "?"
                for u in updates[:succeeded]
            ],
            failures=[
                {
                    "identifier": u.listing_ref.platform_sku or "?",
                    "error_code": "PriceTooLow",
                    "message": "价格低于平台允许的最低售价",
                }
                for u in updates[succeeded:]
            ],
            raw={"mock": True, "total": total},
        )

        if fail_count > 0:
            # 部分成功：抛出携带明细的异常，业务层必须逐行处理。
            # 不直接返回 result 是刻意的 —— 强迫调用方显式处理部分失败，
            # 否则很容易当成"整体成功"上报。
            raise PartialSuccessError(
                category=ErrorCategory.PARTIAL_SUCCESS,
                message=f"批量改价部分成功：{succeeded}/{total} 成功",
                platform=self.platform,
                succeeded=result.success_ids,
                failed=result.failures,
            )

        return AdapterResult(data=result, raw=result.raw)

    async def poll_submission(
        self, *, submission_id: str
    ) -> AdapterResult[dict[str, Any]]:
        self._tick("poll_submission")
        await self._maybe_fail("poll_submission")

        state = self._submissions.get(submission_id)
        if state is None:
            raise AdapterError(
                category=ErrorCategory.NOT_FOUND,
                message=f"未找到提交任务 {submission_id}",
                platform=self.platform,
                raw_code="NotFound",
            )

        state["polls"] += 1
        done = state["polls"] >= state["total"]

        if not done:
            return AdapterResult(
                data={
                    "submission_id": submission_id,
                    "status": SubmissionStatus.PROCESSING.value,
                    "processed": state["polls"],
                    "total": state["total"],
                    "rows": [],
                },
                raw={"mock": True},
            )

        # 完成：按 partial_failure_count 生成逐行结果
        fail_count = self.config.partial_failure_count
        rows: list[dict[str, Any]] = [
            {"identifier": state["sku"], "status": "ACCEPTED", "error_code": None}
        ]
        for i in range(fail_count):
            rows.append(
                {
                    "identifier": f"{state['sku']}-V{i + 1}",
                    "status": "INVALID",
                    "error_code": "InvalidInput",
                    "message": "变体属性缺失",
                }
            )

        return AdapterResult(
            data={
                "submission_id": submission_id,
                "status": SubmissionStatus.DONE.value,
                "processed": state["total"],
                "total": state["total"],
                "rows": rows,
                "succeeded": len([r for r in rows if r["status"] == "ACCEPTED"]),
                "failed": len([r for r in rows if r["status"] != "ACCEPTED"]),
            },
            raw={"mock": True},
            partial=fail_count > 0,
        )

    # ========================================================
    # 库存
    # ========================================================

    async def fetch_inventory(
        self, *, identifiers: list[str]
    ) -> AdapterResult[list[InventorySnapshot]]:
        self._tick("fetch_inventory")
        await self._maybe_fail("fetch_inventory")

        data = [
            InventorySnapshot(
                platform_sku=s,
                available=100 + i * 10,
                reserved=5,
                inbound=0,
                raw={"mock": True, "sku": s},
            )
            for i, s in enumerate(identifiers)
        ]
        return AdapterResult(data=data, raw={"mock": True, "count": len(data)})

    async def update_inventory(
        self, *, updates: list[InventoryUpdate], idempotency_key: str
    ) -> AdapterResult[BatchResult] | AdapterResult[AsyncSubmission]:
        self._tick("update_inventory")
        await self._maybe_fail("update_inventory")

        fail_count = min(self.config.partial_failure_count, len(updates))
        succeeded = len(updates) - fail_count
        return AdapterResult(
            data=BatchResult(
                total=len(updates),
                succeeded=succeeded,
                failed=fail_count,
                success_ids=[u.platform_sku for u in updates[:succeeded]],
                failures=[
                    {"identifier": u.platform_sku, "error_code": "InvalidInput", "message": "库存值非法"}
                    for u in updates[succeeded:]
                ],
                raw={"mock": True},
            ),
            raw={"mock": True},
            partial=fail_count > 0,
        )

    # ========================================================
    # 订单
    # ========================================================

    async def fetch_orders(
        self,
        *,
        time_range: TimeRange,
        statuses: list[str] | None = None,
        page_token: str | None = None,
    ) -> AdapterResult[list[OrderSnapshot]]:
        self._tick("fetch_orders")
        await self._maybe_fail("fetch_orders")

        page = int(page_token or "1")
        size = self.config.page_size
        total_pages = 2

        orders: list[OrderSnapshot] = []
        for i in range(size):
            idx = (page - 1) * size + i
            order_id = f"MOCK-ORD-{idx:06d}"
            # 注意 PII 处理：买家信息用哈希，不存明文（TDD-02 §8）
            orders.append(
                OrderSnapshot(
                    platform_order_id=order_id,
                    order_status=OrderStatus.UNSHIPPED,
                    buyer_hash=pseudonymize(f"buyer-{idx}", salt="mock-tenant"),
                    buyer_region="US-CA",
                    item_total=money("29.98"),
                    shipping_total=money("0"),
                    discount_total=money("0"),
                    tax_total=money("2.40"),
                    grand_total=money("32.38"),
                    currency="USD",
                    order_time=utc_now() - timedelta(hours=idx + 1),
                    fulfillment_channel="FBM",
                    items=[
                        OrderItemSnapshot(
                            platform_sku=f"SKU-{idx % 5:03d}",
                            title=f"Mock 商品 {idx % 5}",
                            quantity=2,
                            unit_price=money("14.99"),
                            item_total=money("29.98"),
                            raw={"mock": True},
                        )
                    ],
                    raw={"mock": True, "order_id": order_id},
                )
            )

        has_more = page < total_pages
        return AdapterResult(
            data=orders,
            next_token=str(page + 1) if has_more else None,
            has_more=has_more,
            raw={"mock": True, "page": page, "count": len(orders)},
        )

    async def fetch_order_detail(
        self, *, order_ids: list[str]
    ) -> AdapterResult[list[OrderSnapshot]]:
        self._tick("fetch_order_detail")
        await self._maybe_fail("fetch_order_detail")

        data = [
            OrderSnapshot(
                platform_order_id=oid,
                order_status=OrderStatus.SHIPPED,
                buyer_hash=pseudonymize(f"buyer-{oid}", salt="mock-tenant"),
                buyer_region="US-NY",
                item_total=money("49.99"),
                grand_total=money("54.00"),
                currency="USD",
                order_time=utc_now() - timedelta(days=1),
                ship_time=utc_now() - timedelta(hours=12),
                raw={"mock": True, "order_id": oid},
            )
            for oid in order_ids
        ]
        return AdapterResult(data=data, raw={"mock": True, "count": len(data)})

    async def confirm_shipment(
        self, *, shipments: list[ShipmentConfirmation], idempotency_key: str
    ) -> AdapterResult[BatchResult]:
        self._tick("confirm_shipment")
        await self._maybe_fail("confirm_shipment")

        return AdapterResult(
            data=BatchResult(
                total=len(shipments),
                succeeded=len(shipments),
                failed=0,
                success_ids=[s.platform_order_id for s in shipments],
                raw={"mock": True},
            ),
            raw={"mock": True},
        )

    # ========================================================
    # 结算与财务
    # ========================================================

    async def fetch_settlements(
        self, *, time_range: TimeRange, page_token: str | None = None
    ) -> AdapterResult[list[SettlementBatch]]:
        self._tick("fetch_settlements")
        await self._maybe_fail("fetch_settlements")

        batches = [
            SettlementBatch(
                settlement_id=f"STL-{time_range.start:%Y%m%d}-{i}",
                period_start=time_range.start,
                period_end=time_range.end,
                total_amount=money("12500.00"),
                currency="USD",
                raw={"mock": True},
            )
            for i in range(2)
        ]
        return AdapterResult(data=batches, raw={"mock": True})

    async def fetch_fees(
        self,
        *,
        settlement_id: str | None = None,
        time_range: TimeRange | None = None,
        page_token: str | None = None,
    ) -> AdapterResult[list[FeeRecord]]:
        self._tick("fetch_fees")
        await self._maybe_fail("fetch_fees")

        fee_types = [
            ("REFERRAL_FEE", "4.50"),
            ("FBA_FULFILLMENT_FEE", "5.20"),
            ("STORAGE_FEE", "0.35"),
            ("AD_SPEND", "2.10"),
        ]
        records = [
            FeeRecord(
                fee_type=ft,
                amount=money(amt),
                currency="USD",
                settlement_id=settlement_id,
                platform_order_id=f"MOCK-ORD-{i:06d}",
                fee_time=utc_now() - timedelta(days=i),
                raw={"mock": True},
            )
            for i, (ft, amt) in enumerate(fee_types)
        ]
        return AdapterResult(data=records, raw={"mock": True})

    async def request_report(
        self, *, report_type: ReportType, params: dict[str, Any], idempotency_key: str
    ) -> AdapterResult[AsyncSubmission]:
        self._tick("request_report")
        await self._maybe_fail("request_report")

        report_id = f"RPT-{report_type.value}-{idempotency_key[:8]}"
        return AdapterResult(
            data=AsyncSubmission(
                submission_id=report_id,
                poll_hint_seconds=5,
                raw={"mock": True},
            ),
            raw={"mock": True},
        )

    async def fetch_report(self, *, report_id: str) -> AdapterResult[ReportPayload]:
        self._tick("fetch_report")
        await self._maybe_fail("fetch_report")

        payload = ReportPayload(
            report_id=report_id,
            report_type=ReportType.SETTLEMENT,
            rows=[
                {
                    "settlement_id": report_id,
                    "order_id": f"MOCK-ORD-{i:06d}",
                    "sku": f"SKU-{i % 5:03d}",
                    "quantity": 1,
                    "item_total": "29.99",
                    "referral_fee": "-4.50",
                    "fulfillment_fee": "-5.20",
                    "net_amount": "20.29",
                    "currency": "USD",
                }
                for i in range(10)
            ],
            generated_at=utc_now(),
        )
        return AdapterResult(data=payload, raw={"mock": True})

    # ========================================================
    # 售后
    # ========================================================

    async def fetch_refunds(
        self, *, time_range: TimeRange, page_token: str | None = None
    ) -> AdapterResult[list[RefundRecord]]:
        self._tick("fetch_refunds")
        await self._maybe_fail("fetch_refunds")

        records = [
            RefundRecord(
                platform_refund_id=f"MOCK-RF-{i:04d}",
                platform_order_id=f"MOCK-ORD-{i:06d}",
                amount=money("29.99"),
                currency="USD",
                refund_type="FULL",
                reason_code="SIZE_TOO_SMALL",
                status="REFUNDED",
                refunded_at=utc_now() - timedelta(days=i),
                raw={"mock": True},
            )
            for i in range(3)
        ]
        return AdapterResult(data=records, raw={"mock": True})

    async def fetch_returns(
        self, *, time_range: TimeRange, page_token: str | None = None
    ) -> AdapterResult[list[ReturnRecord]]:
        self._tick("fetch_returns")
        await self._maybe_fail("fetch_returns")

        records = [
            ReturnRecord(
                platform_return_id=f"MOCK-RT-{i:04d}",
                platform_order_id=f"MOCK-ORD-{i:06d}",
                platform_sku=f"SKU-{i % 5:03d}",
                quantity=1,
                reason_code="DEFECTIVE",
                restock_status="UNSELLABLE",
                returned_at=utc_now() - timedelta(days=i),
                raw={"mock": True},
            )
            for i in range(2)
        ]
        return AdapterResult(data=records, raw={"mock": True})

    async def execute_refund(
        self, *, refund: RefundRecord, idempotency_key: str
    ) -> AdapterResult[RefundRecord]:
        """执行退款。

        Mock 故意声明 REFUND_CREATE = UNSUPPORTED（模拟 Amazon），
        因此这里会抛 UnsupportedCapabilityError —— 用于验证
        "生成人工待办"的降级路径。
        """
        self._tick("execute_refund")
        self._require(
            Capability.REFUND_CREATE,
            fallback="已生成人工退款待办，请到平台后台手动处理",
        )
        raise NotImplementedError  # pragma: no cover —— _require 已抛错

    # ========================================================
    # 消息
    # ========================================================

    async def send_message(
        self,
        *,
        order_id: str,
        template: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> AdapterResult[MessageRecord]:
        self._tick("send_message")
        await self._maybe_fail("send_message")

        record = MessageRecord(
            platform_message_id=f"MSG-{idempotency_key[:10]}",
            platform_order_id=order_id,
            direction="OUTBOUND",
            subject=template,
            content=str(payload.get("body", "")),
            sent_at=utc_now(),
            raw={"mock": True},
        )
        return AdapterResult(data=record, raw=record.raw)

    async def fetch_messages(
        self,
        *,
        order_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> AdapterResult[list[MessageRecord]]:
        """读取买家消息。

        Mock 故意声明 MESSAGE_READ = UNSUPPORTED（模拟 Amazon），
        调用会抛 UnsupportedCapabilityError。
        """
        self._tick("fetch_messages")
        self._require(
            Capability.MESSAGE_READ,
            fallback="该平台不支持读取历史消息，建议配置平台消息转发到邮箱",
        )
        raise NotImplementedError  # pragma: no cover

    # ========================================================
    # 通知
    # ========================================================

    async def subscribe_notifications(
        self, *, callback: NotificationCallback
    ) -> AdapterResult[SubscriptionResult]:
        self._tick("subscribe_notifications")
        await self._maybe_fail("subscribe_notifications")

        result = SubscriptionResult(
            ok=True,
            subscription_id=f"MOCK-SUB-{self.shop.shop_id}",
            topics=list(callback.topics),
            message="Mock 订阅成功",
            raw={"mock": True},
        )
        return AdapterResult(data=result, raw=result.raw)

    # ========================================================
    # 测试辅助
    # ========================================================

    def reset(self) -> None:
        """重置调用计数与内存状态。测试之间隔离用。"""
        self.call_counts.clear()
        self._submissions.clear()
        self._listings.clear()
        self._rng = random.Random(self.config.seed)

    def call_count(self, method: str) -> int:
        """查询某方法的调用次数（断言"没有重复调用"用）。"""
        return self.call_counts.get(method, 0)
