"""Amazon SP-API 适配器（Phase 1：骨架 + 认证 + 只读）。

交付边界（TDD-01 §5.3）：
    ✅ 能力矩阵声明
    ✅ 认证与健康检查
    ✅ 只读方法（类目、Listing、订单、库存、报表请求）
    ❌ 写方法（Phase 1 明确不做，抛 NotImplementedError）

为什么 Phase 1 不做写操作：
    PRD v1.1 的完整愿景（自动上架、自动改价、自动退款）风险极高，
    在适配器契约与数据模型未被真实数据验证前就开写操作，
    一旦模型错了会产生**真实的生产事故**（错价、误退款）。
    正确顺序是：先用 Mock 跑通全链路 → 拿到授权用真实只读数据验证 →
    再逐步开放写操作。

**三个必须记住的 Amazon 硬事实**（详见 adapters/amazon/AGENTS.md）：
    1. 无卖家侧执行退款 API（REFUND_CREATE = UNSUPPORTED）
    2. 无历史消息读取 API（MESSAGE_READ = UNSUPPORTED）
    3. Feeds 是异步的，"提交成功" ≠ "上架成功"
"""

from __future__ import annotations

from typing import Any, ClassVar

from adapters.amazon.client import AmazonClient
from adapters.amazon.error_map import AMAZON_FEED_ROW_STATUS_MAP, map_amazon_error
from adapters.base import PlatformAdapter, PlatformClient, ShopContext
from adapters.capabilities import AMAZON_CAPABILITIES, Capability, CapabilityMatrix
from adapters.errors import AdapterError, ErrorCategory, translate_errors
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
    SubscriptionResult,
    TimeRange,
)
from core.constants import OrderStatus, Platform
from core.money import money
from core.timeutil import parse_iso

__all__ = ["AmazonAdapter"]

#: Amazon 的 order_status 是"平台语义"，需要映射到我们的 OrderStatus。
#:
#: 未映射的状态会记入 platform_raw 并触发"需补映射"告警 ——
#: 不能静默丢弃，否则订单会凭空消失。
AMAZON_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "Pending": OrderStatus.PENDING,
    "PendingAvailability": OrderStatus.PENDING,
    "Unshipped": OrderStatus.UNSHIPPED,
    "PartiallyShipped": OrderStatus.PARTIALLY_SHIPPED,
    "Shipped": OrderStatus.SHIPPED,
    "Delivered": OrderStatus.DELIVERED,
    "Canceled": OrderStatus.CANCELED,
    "Unfulfillable": OrderStatus.CANCELED,
    "InvoiceUnconfirmed": OrderStatus.PENDING,
}

#: Amazon 单次批量查询的上限（Catalog Items 是 20，Listings 是 20）
AMAZON_BATCH_LIMIT = 20


@register(Platform.AMAZON)
class AmazonAdapter(PlatformAdapter):
    """Amazon SP-API 适配器。"""

    platform: ClassVar[str] = Platform.AMAZON.value
    display_name: ClassVar[str] = "亚马逊"

    def __init__(self, shop: ShopContext, client: PlatformClient) -> None:
        super().__init__(shop=shop, client=client)
        self._amz: AmazonClient = client  # type: ignore[assignment]

    # ========================================================
    # 元信息
    # ========================================================

    @classmethod
    def capabilities(cls) -> CapabilityMatrix:
        return CapabilityMatrix(Platform.AMAZON.value, AMAZON_CAPABILITIES)

    @translate_errors(Platform.AMAZON.value)
    async def health_check(self) -> HealthStatus:
        """连通性与授权检查。

        用 marketplaceParticipations 作为探针 —— 它是最轻量的
        已认证接口，能同时验证：令牌有效、区域正确、权限足够。
        """
        try:
            body = await self._amz.request(
                "GET", "/sellers/v1/marketplaceParticipations"
            )
        except AdapterError as exc:
            return HealthStatus(
                ok=False,
                platform=self.platform,
                message=f"{exc.category.label}：{exc.message}",
                raw={"category": exc.category.value, "raw_code": exc.raw_code},
            )

        participations = body.get("payload", [])
        return HealthStatus(
            ok=True,
            platform=self.platform,
            message=f"已授权 {len(participations)} 个市场",
            raw={"payload": participations},
        )

    # ========================================================
    # 类目
    # ========================================================

    @translate_errors(Platform.AMAZON.value)
    async def fetch_category_tree(
        self, *, parent_id: str | None = None, force_refresh: bool = False
    ) -> AdapterResult[list[CategoryNode]]:
        """拉取类目树。

        Amazon 没有"一次性拉全树"的接口，需要从根节点递归。
        本方法只返回一层 —— 递归由 service 层控制深度与频率，
        因为全量树有上万节点，一次性拉完会触发限流。
        """
        if not self.shop.marketplace_id:
            raise AdapterError(
                category=ErrorCategory.PRECONDITION_FAILED,
                message="缺少 marketplace_id，无法查询类目",
                platform=self.platform,
                action="检查店铺配置是否包含 marketplace_id",
            )

        body = await self._amz.request(
            "GET",
            "/catalog/2022-04-01/categories",
            params={
                "marketplaceId": self.shop.marketplace_id,
                **({"parentCategoryId": parent_id} if parent_id else {}),
            },
        )

        nodes: list[CategoryNode] = []
        for item in body.get("payload", {}).get("categories", []):
            nodes.append(
                CategoryNode(
                    category_id=str(item.get("categoryId", "")),
                    name=str(item.get("categoryName", "")),
                    parent_id=parent_id,
                    is_leaf=not item.get("children"),
                    raw=item,
                )
            )
        return AdapterResult(data=nodes, raw=body)

    @translate_errors(Platform.AMAZON.value)
    async def fetch_category_schema(
        self, *, category_id: str, product_type: str | None = None
    ) -> AdapterResult[CategorySchema]:
        """拉取类目属性定义（Product Type Definitions API）。

        **必须动态获取，不能硬编码** —— 属性 schema 会变，
        缓存不版本化会导致"昨天能上架今天不行"。
        """
        if not self.shop.marketplace_id:
            raise AdapterError(
                category=ErrorCategory.PRECONDITION_FAILED,
                message="缺少 marketplace_id，无法查询属性定义",
                platform=self.platform,
            )

        body = await self._amz.request(
            "GET",
            f"/definitions/2020-09-01/productTypes/{product_type or category_id}",
            params={"marketplaceIds": self.shop.marketplace_id},
        )

        # Amazon 返回的 schema 是 JSON Schema 结构，取 required 与 properties
        schema = body.get("schema", {}) if isinstance(body.get("schema"), dict) else body
        required = list(schema.get("required", []))
        properties = schema.get("properties", {})

        attributes = [
            {
                "name": name,
                "type": (spec.get("type") if isinstance(spec, dict) else None),
                "required": name in required,
            }
            for name, spec in (properties.items() if isinstance(properties, dict) else [])
        ]

        return AdapterResult(
            data=CategorySchema(
                category_id=category_id,
                product_type=product_type,
                schema_version=str(body.get("schemaVersion", "unknown")),
                required_fields=required,
                attributes=attributes,
                raw=body,
            ),
            raw=body,
        )

    # ========================================================
    # Listing 读
    # ========================================================

    @translate_errors(Platform.AMAZON.value)
    async def fetch_listings(
        self, *, identifiers: list[str], identifier_type: IdentifierType
    ) -> AdapterResult[list[ListingSnapshot]]:
        """按 SKU 批量查询 Listing。

        自动切片：Amazon 单次上限 20 个 SKU。
        业务层不需要知道这个限制 —— 这是适配器的职责。
        """
        if identifier_type is not IdentifierType.PLATFORM_SKU:
            raise AdapterError(
                category=ErrorCategory.VALIDATION_FAILED,
                message="Amazon 的 Listings Items API 仅支持按 sellerSku 查询",
                platform=self.platform,
                context={"identifier_type": identifier_type.value},
                action="请先通过 SKU 映射把标识转换为 platform_sku",
            )

        snapshots: list[ListingSnapshot] = []
        warnings: list[str] = []
        raw_all: list[dict[str, Any]] = []

        for chunk in _chunked(identifiers, AMAZON_BATCH_LIMIT):
            body = await self._amz.request(
                "GET",
                f"/listings/2021-08-01/items/{self._seller_id()}",
                params={
                    "marketplaceIds": self.shop.marketplace_id or "",
                    "skus": ",".join(chunk),
                    "includedData": "summaries,attributes,offers",
                },
            )
            raw_all.append(body)
            for item in body.get("items", []):
                snapshots.append(_transform_listing_item(item))

        return AdapterResult(
            data=snapshots,
            raw={"batches": raw_all},
            warnings=warnings,
        )

    # ========================================================
    # 订单
    # ========================================================

    @translate_errors(Platform.AMAZON.value)
    async def fetch_orders(
        self,
        *,
        time_range: TimeRange,
        statuses: list[str] | None = None,
        page_token: str | None = None,
    ) -> AdapterResult[list[OrderSnapshot]]:
        """按时间范围拉订单（增量同步主路径）。

        注意：Amazon 的 getOrders 要求时间跨度不超过 31 天，
        且 CreatedAfter/CreatedBefore 有精度要求。
        """
        from core.timeutil import iso_z

        params: dict[str, Any] = {
            "MarketplaceIds": self.shop.marketplace_id or "",
            "CreatedAfter": iso_z(time_range.start),
            "CreatedBefore": iso_z(time_range.end),
            "MaxResultsPerPage": 100,
        }
        if statuses:
            params["OrderStatuses"] = ",".join(statuses)
        if page_token:
            params["NextToken"] = page_token

        body = await self._amz.request("GET", "/orders/v0/orders", params=params)

        payload = body.get("payload", {})
        orders = [_transform_order(o) for o in payload.get("Orders", [])]

        return AdapterResult(
            data=orders,
            raw=body,
            next_token=payload.get("NextToken"),
            has_more=bool(payload.get("NextToken")),
        )

    @translate_errors(Platform.AMAZON.value)
    async def fetch_order_detail(
        self, *, order_ids: list[str]
    ) -> AdapterResult[list[OrderSnapshot]]:
        """拉订单明细（含订单行）。

        Amazon 需要逐单调用 getOrderItems，无法批量 ——
        因此本方法对 order_ids 串行处理，且**必须限量**，
        否则会瞬间打满限流配额。
        """
        if len(order_ids) > 50:  # noqa: PLR2004
            raise AdapterError(
                category=ErrorCategory.VALIDATION_FAILED,
                message=f"单次查询订单明细不得超过 50 单（请求了 {len(order_ids)}）",
                platform=self.platform,
                action="请分批调用",
            )

        results: list[OrderSnapshot] = []
        raw_all: list[dict[str, Any]] = []

        for order_id in order_ids:
            body = await self._amz.request("GET", f"/orders/v0/orders/{order_id}")
            raw_all.append(body)
            order_payload = body.get("payload", {})
            snapshot = _transform_order(order_payload)

            # 再拉订单行
            items_body = await self._amz.request(
                "GET", f"/orders/v0/orders/{order_id}/orderItems"
            )
            raw_all.append(items_body)
            order_items = items_body.get("payload", {}).get("OrderItems", [])
            snapshot.items = [_transform_order_item(i) for i in order_items]
            results.append(snapshot)

        return AdapterResult(data=results, raw={"batches": raw_all})

    # ========================================================
    # 库存
    # ========================================================

    @translate_errors(Platform.AMAZON.value)
    async def fetch_inventory(
        self, *, identifiers: list[str]
    ) -> AdapterResult[list[InventorySnapshot]]:
        """查询 FBA 库存。

        注意：Amazon 的库存分 FBA 与 FBM 两套。
        本方法查 FBA（FBA Inventory API）；FBM 需要从 Listing 的
        fulfillmentAvailability 读取，属于不同接口。
        """
        snapshots: list[InventorySnapshot] = []
        raw_all: list[dict[str, Any]] = []

        for chunk in _chunked(identifiers, AMAZON_BATCH_LIMIT):
            body = await self._amz.request(
                "GET",
                "/fba/inventory/v1/summaries",
                params={
                    "marketplaceIds": self.shop.marketplace_id or "",
                    "sellerSkus": ",".join(chunk),
                    "granularityType": "Marketplace",
                    "granularityId": self.shop.marketplace_id or "",
                },
            )
            raw_all.append(body)
            for item in body.get("payload", {}).get("inventorySummaries", []):
                detail = item.get("inventoryDetails", {})
                snapshots.append(
                    InventorySnapshot(
                        platform_sku=str(item.get("sellerSku", "")),
                        available=int(detail.get("fulfillableQuantity", 0) or 0),
                        reserved=int(detail.get("reservedQuantity", {}).get("totalReservedQuantity", 0) or 0),
                        inbound=int(detail.get("inboundWorkingQuantity", 0) or 0),
                        raw=item,
                    )
                )

        return AdapterResult(data=snapshots, raw={"batches": raw_all})

    # ========================================================
    # 报表（财务数据主要靠这个）
    # ========================================================

    @translate_errors(Platform.AMAZON.value)
    async def request_report(
        self, *, report_type: ReportType, params: dict[str, Any], idempotency_key: str
    ) -> AdapterResult[AsyncSubmission]:
        """请求异步报表。

        结算、退款、费用数据在 Amazon 上都只能通过 Reports API 获取，
        而 Reports 是**异步**的：创建 → 轮询 → 下载。
        """
        amazon_report_type = _to_amazon_report_type(report_type)

        body = await self._amz.request(
            "POST",
            "/reports/2021-06-30/reports",
            json_body={
                "reportType": amazon_report_type,
                "marketplaceIds": [self.shop.marketplace_id] if self.shop.marketplace_id else [],
                **params,
            },
        )

        report_id = str(body.get("reportId", ""))
        return AdapterResult(
            data=AsyncSubmission(
                submission_id=report_id,
                poll_hint_seconds=60,
                raw=body,
            ),
            raw=body,
            request_id=report_id,
        )

    @translate_errors(Platform.AMAZON.value)
    async def fetch_report(self, *, report_id: str) -> AdapterResult[ReportPayload]:
        """获取报表结果。

        未就绪时抛 SERVICE_UNAVAILABLE（可重试），
        而不是返回空数据 —— 返回空数据会让调用方误以为"没有数据"。
        """
        body = await self._amz.request("GET", f"/reports/2021-06-30/reports/{report_id}")
        status = body.get("processingStatus")

        if status in ("IN_QUEUE", "IN_PROGRESS"):
            raise AdapterError(
                category=ErrorCategory.SERVICE_UNAVAILABLE,
                message=f"报表 {report_id} 尚未就绪（{status}）",
                platform=self.platform,
                retryable=True,
                context={"report_id": report_id, "processing_status": status},
            )
        if status in ("CANCELLED", "FATAL"):
            raise AdapterError(
                category=ErrorCategory.BUSINESS_REJECTED,
                message=f"报表 {report_id} 处理失败（{status}）",
                platform=self.platform,
                context={"report_id": report_id, "processing_status": status},
            )

        document_id = body.get("reportDocumentId")
        if not document_id:
            raise AdapterError(
                category=ErrorCategory.SCHEMA_DRIFT,
                message=f"报表 {report_id} 已 DONE 但缺少 reportDocumentId",
                platform=self.platform,
            )

        doc = await self._amz.request(
            "GET", f"/reports/2021-06-30/documents/{document_id}"
        )
        url = doc.get("url")
        if not url:
            raise AdapterError(
                category=ErrorCategory.SCHEMA_DRIFT,
                message="报表文档响应缺少 url",
                platform=self.platform,
            )

        content = await self._amz.download(str(url))
        text = content.decode("utf-8", errors="replace")

        return AdapterResult(
            data=ReportPayload(
                report_id=report_id,
                report_type=report_type_hint(body),
                raw_content=text,
                generated_at=parse_iso(body["createdTime"]) if body.get("createdTime") else None,
            ),
            raw={"document": doc, "status": body},
        )

    # ========================================================
    # 写操作（Phase 1 明确不做）
    # ========================================================

    async def create_listing(
        self, *, draft: ListingDraft, idempotency_key: str, validate_only: bool = False
    ) -> AdapterResult[ListingSnapshot] | AdapterResult[AsyncSubmission]:
        """创建 Listing —— Phase 1 不实现。

        Feeds 四步链路（createFeedDocument → createFeed → 轮询 →
        取 processingReport）已在 TDD-03 定义，待 Phase 2 落地。
        """
        self._not_implemented("create_listing")

    async def update_listing(
        self, *, listing_ref: ListingRef, patch: ListingPatch, idempotency_key: str
    ) -> AdapterResult[ListingSnapshot] | AdapterResult[AsyncSubmission]:
        self._not_implemented("update_listing")

    async def delete_listing(
        self, *, listing_ref: ListingRef, idempotency_key: str
    ) -> AdapterResult[None] | AdapterResult[AsyncSubmission]:
        self._not_implemented("delete_listing")

    async def update_price(
        self, *, updates: list[PriceUpdate], idempotency_key: str
    ) -> AdapterResult[BatchResult] | AdapterResult[AsyncSubmission]:
        self._not_implemented("update_price")

    async def update_inventory(
        self, *, updates: list[InventoryUpdate], idempotency_key: str
    ) -> AdapterResult[BatchResult] | AdapterResult[AsyncSubmission]:
        self._not_implemented("update_inventory")

    async def confirm_shipment(
        self, *, shipments: list[ShipmentConfirmation], idempotency_key: str
    ) -> AdapterResult[BatchResult]:
        self._not_implemented("confirm_shipment")

    async def poll_submission(
        self, *, submission_id: str
    ) -> AdapterResult[dict[str, Any]]:
        """查询 Feed 处理状态。

        这个是只读的，Phase 1 可以实现 —— 但需要先有 createFeed，
        所以一并留到 Phase 2。
        """
        self._not_implemented("poll_submission")

    async def execute_refund(
        self, *, refund: RefundRecord, idempotency_key: str
    ) -> AdapterResult[RefundRecord]:
        """执行退款 —— **Amazon 永久不支持**。

        这不是"Phase 1 没做"，而是平台就没有这个 API。
        调用会抛 UnsupportedCapabilityError，业务层必须
        生成人工待办。
        """
        self._require(
            Capability.REFUND_CREATE,
            fallback="已生成人工退款待办，请到 Seller Central 手动处理",
        )
        raise NotImplementedError  # pragma: no cover

    # ========================================================
    # 内部
    # ========================================================

    def _seller_id(self) -> str:
        """卖家 ID。

        注意：这里的 seller_id 在 Amazon 语境下指 **sellerId**，
        从授权信息中获得。缺失时应尽早报错而不是拼出错误 URL。
        """
        seller_id = getattr(self.shop, "seller_id", None) or self.shop.marketplace_id
        if not seller_id:
            raise AdapterError(
                category=ErrorCategory.PRECONDITION_FAILED,
                message="缺少 sellerId，无法调用 Listings Items API",
                platform=self.platform,
            )
        return str(seller_id)


# ============================================================
# 转换函数（原始报文 → 规范化结构）
# ============================================================


def _chunked(items: list[str], size: int) -> list[list[str]]:
    """切片。适配器内部处理平台的单次上限，业务层无需关心。"""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _transform_listing_item(item: dict[str, Any]) -> ListingSnapshot:
    """Amazon Listing item → ListingSnapshot。"""
    summaries = item.get("summaries", [{}])
    summary = summaries[0] if summaries else {}
    offers = item.get("offers", [])
    offer = offers[0] if offers else {}

    price_val = None
    currency = None
    price_obj = offer.get("price") if isinstance(offer, dict) else None
    if isinstance(price_obj, dict):
        price_val = money(price_obj["amount"]) if price_obj.get("amount") else None
        currency = price_obj.get("currency")

    return ListingSnapshot(
        platform_sku=str(item.get("sku", "")),
        platform_item_id=summary.get("asin"),
        status=str(summary.get("status", ["UNKNOWN"])[0])
        if isinstance(summary.get("status"), list)
        else str(summary.get("status", "UNKNOWN")),
        title=summary.get("itemName"),
        price=price_val,
        currency=currency,
        quantity=offer.get("quantity") if isinstance(offer, dict) else None,
        raw=item,
    )


def _transform_order(order: dict[str, Any]) -> OrderSnapshot:
    """Amazon Order → OrderSnapshot。

    **PII 处理**：不取 BuyerEmail / ShippingAddress 明文。
    买家标识用 OrderId 派生哈希；地区从 ShippingAddress 的
    CountryCode/StateOrRegion 取（已聚合，非 PII）。
    """
    from core.security.masking import pseudonymize

    raw_status = str(order.get("OrderStatus", ""))
    mapped = AMAZON_ORDER_STATUS_MAP.get(raw_status, OrderStatus.PENDING)

    addr = order.get("ShippingAddress", {}) if isinstance(order.get("ShippingAddress"), dict) else {}
    region_parts = [addr.get("CountryCode"), addr.get("StateOrRegion")]
    region = "-".join(str(p) for p in region_parts if p) or None

    return OrderSnapshot(
        platform_order_id=str(order.get("AmazonOrderId", "")),
        order_status=mapped,
        buyer_hash=pseudonymize(str(order.get("AmazonOrderId", ""))),
        buyer_region=region,
        item_total=money(order.get("OrderTotal", {}).get("Amount", 0))
        if isinstance(order.get("OrderTotal"), dict)
        else money(0),
        currency=(order.get("OrderTotal", {}).get("CurrencyCode", "USD"))
        if isinstance(order.get("OrderTotal"), dict)
        else "USD",
        order_time=parse_iso(order["PurchaseDate"]) if order.get("PurchaseDate") else None,
        fulfillment_channel=order.get("FulfillmentChannel"),
        raw={**order, "raw_order_status": raw_status},
    )


def _transform_order_item(item: dict[str, Any]) -> OrderItemSnapshot:
    price_obj = item.get("ItemPrice") if isinstance(item.get("ItemPrice"), dict) else {}
    return OrderItemSnapshot(
        platform_sku=item.get("SellerSKU"),
        title=item.get("Title"),
        quantity=int(item.get("QuantityOrdered", 1) or 1),
        unit_price=money(price_obj.get("Amount", 0)) if price_obj.get("Amount") else money(0),
        item_total=money(price_obj.get("Amount", 0)) if price_obj.get("Amount") else money(0),
        raw=item,
    )


def _to_amazon_report_type(report_type: ReportType) -> str:
    """我们的报表类型 → Amazon reportType 枚举值。"""
    mapping = {
        ReportType.SETTLEMENT: "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE",
        ReportType.FEES: "GET_FBA_ESTIMATED_FBA_FEES_TXT_DATA",
        ReportType.REFUNDS: "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE",
        ReportType.RETURNS: "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA",
        ReportType.INVENTORY: "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA",
        ReportType.LISTINGS: "GET_MERCHANT_LISTINGS_ALL_DATA",
        ReportType.ORDERS: "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
        ReportType.REIMBURSEMENTS: "GET_FBA_REIMBURSEMENTS_DATA",
        ReportType.AD_PERFORMANCE: "GET_FLAT_FILE_ADS_REPORT",
    }
    return mapping.get(report_type, "GET_MERCHANT_LISTINGS_ALL_DATA")


def report_type_hint(body: dict[str, Any]) -> str:
    """从响应里推断报表类型（Amazon 不一定回传）。"""
    return str(body.get("reportType", ""))
