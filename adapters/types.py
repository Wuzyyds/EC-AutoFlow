"""适配器层的数据传输对象（DTO）。

设计原则：
    1. **全部是纯 dataclass**，不依赖 SQLAlchemy（adapters 不碰 ORM）
    2. **金额一律 Decimal**（TDD-01 原则 3）
    3. **时间一律 aware UTC**（TDD-01 原则 4）
    4. 每个返回值都携带 `raw`（原始报文），实现"规范化数据可重建"（原则 5）

命名与领域模型的区别：
    这里的 `*Snapshot` 表示"平台侧的瞬时状态"，是**只读投影**；
    数据库里的 `*` 表是"我方视角的持久化实体"，含租户、审计等字段。
    两者不可混用 —— 转换发生在 service 层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Generic, TypeVar

from core.constants import OrderStatus
from core.money import ZERO
from core.timeutil import utc_now

__all__ = [
    "AdapterResult",
    "AsyncSubmission",
    "BatchResult",
    "SubmissionStatus",
    "HealthStatus",
    "TimeRange",
    "IdentifierType",
    "ReportType",
    "CategoryNode",
    "CategorySchema",
    "EligibilityResult",
    "ListingSnapshot",
    "ListingRef",
    "ListingDraft",
    "ListingPatch",
    "PriceUpdate",
    "InventorySnapshot",
    "InventoryUpdate",
    "OrderSnapshot",
    "OrderItemSnapshot",
    "ShipmentConfirmation",
    "SettlementBatch",
    "FeeRecord",
    "RefundRecord",
    "ReturnRecord",
    "MessageRecord",
    "AdCampaign",
    "AdMetricRow",
    "ReportPayload",
    "SubscriptionResult",
]

T = TypeVar("T")


# ============================================================
# 通用包装
# ============================================================


@dataclass
class AdapterResult(Generic[T]):
    """适配器统一返回。

    业务层不接触原始 HTTP 响应，只接触这里定义的结构。
    """

    data: T

    #: 原始报文 → 写入 raw_payloads 表（原则 5：原始数据不可变）
    raw: dict[str, Any] | list[Any] | None = None

    #: 平台请求 ID（排障用，报工单时平台会要）
    request_id: str | None = None

    #: 获取时间
    fetched_at: datetime = field(default_factory=utc_now)

    #: 分页游标
    next_token: str | None = None
    has_more: bool = False

    #: **部分成功**标志。
    #:
    #: Amazon Feed 的典型返回是"1000 行中 3 行失败"。
    #: 这不是成功也不是失败 —— 业务层必须知道，
    #: 否则会上报"上架成功"而实际有 3 个 SKU 没上。
    partial: bool = False

    #: 非致命警告（如"平台不支持 validate_only，已用本地校验代替"）
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        if self.data is None:
            return True
        if isinstance(self.data, (list, tuple, dict, str)):
            return len(self.data) == 0
        return False


@dataclass
class AsyncSubmission:
    """异步写入的提交凭证（Amazon Feed / TikTok 批量任务）。"""

    #: Feed ID / task_id
    submission_id: str

    submitted_at: datetime = field(default_factory=utc_now)

    #: 预计就绪时间（用于调度轮询节奏）
    expected_ready_at: datetime | None = None

    #: 建议轮询间隔（秒）
    poll_hint_seconds: int = 60

    raw: dict[str, Any] | None = None


class SubmissionStatus(StrEnum):
    """异步提交的处理状态。"""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


@dataclass
class BatchResult:
    """批量操作结果（逐条明细）。"""

    total: int = 0
    succeeded: int = 0
    failed: int = 0

    #: 成功项的标识列表
    success_ids: list[str] = field(default_factory=list)

    #: 失败项明细：[{"identifier": "...", "error_code": "...", "message": "..."}]
    failures: list[dict[str, Any]] = field(default_factory=list)

    raw: dict[str, Any] | None = None

    @property
    def all_succeeded(self) -> bool:
        return self.failed == 0 and self.total > 0

    @property
    def partial(self) -> bool:
        """是否部分成功 —— 业务层必须逐行处理这种情况。"""
        return self.succeeded > 0 and self.failed > 0


@dataclass
class HealthStatus:
    """连通性与授权健康状态。"""

    ok: bool
    platform: str = ""
    #: Token 过期时间（若平台返回）
    token_expires_at: datetime | None = None
    #: 剩余配额比例（0–1，若平台返回）
    quota_remaining_ratio: Decimal | None = None
    message: str = ""
    latency_ms: int | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class TimeRange:
    """时间区间（左闭右开）。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError(f"时间区间非法：start={self.start} >= end={self.end}")

    @property
    def days(self) -> int:
        return max((self.end - self.start).days, 1)


class IdentifierType(StrEnum):
    """查询标识的类型。不同平台对"用什么标识查商品"要求不同。"""

    PLATFORM_SKU = "PLATFORM_SKU"
    PLATFORM_ITEM_ID = "PLATFORM_ITEM_ID"
    INTERNAL_SKU = "INTERNAL_SKU"
    ASIN = "ASIN"
    UPC = "UPC"


class ReportType(StrEnum):
    """报表类型（各平台命名不同，这里归一化）。"""

    SETTLEMENT = "SETTLEMENT"
    FEES = "FEES"
    REFUNDS = "REFUNDS"
    RETURNS = "RETURNS"
    INVENTORY = "INVENTORY"
    LISTINGS = "LISTINGS"
    ORDERS = "ORDERS"
    REIMBURSEMENTS = "REIMBURSEMENTS"
    AD_PERFORMANCE = "AD_PERFORMANCE"


# ============================================================
# 类目与商品
# ============================================================


@dataclass
class CategoryNode:
    """类目树节点。"""

    category_id: str
    name: str
    parent_id: str | None = None
    is_leaf: bool = False
    children: list[CategoryNode] = field(default_factory=list)
    raw: dict[str, Any] | None = None


@dataclass
class CategorySchema:
    """类目属性定义（Amazon 的 Product Type Definition）。

    必须版本化 —— Amazon 的 schema 会变，
    缓存不版本化会导致"昨天能上架今天不行"。
    """

    category_id: str
    schema_version: str
    product_type: str | None = None

    #: 必填字段名列表
    required_fields: list[str] = field(default_factory=list)

    #: 完整属性定义：[{"name": "...", "type": "...", "enum": [...], ...}]
    attributes: list[dict[str, Any]] = field(default_factory=list)

    fetched_at: datetime = field(default_factory=utc_now)
    raw: dict[str, Any] | None = None


@dataclass
class EligibilityResult:
    """上架资格校验结果。"""

    eligible: bool
    reasons: list[str] = field(default_factory=list)
    #: 需要额外审批的项（品牌授权、类目审核等）
    requires_approval_for: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None


@dataclass
class ListingSnapshot:
    """平台侧 Listing 的瞬时状态（只读投影）。"""

    platform_sku: str | None = None
    platform_item_id: str | None = None
    parent_item_id: str | None = None

    status: str = ""
    title: str | None = None

    price: Decimal | None = None
    currency: str | None = None
    quantity: int | None = None

    category_id: str | None = None
    updated_at: datetime | None = None

    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ListingRef:
    """定位一个 Listing 的最小信息。"""

    platform_sku: str | None = None
    platform_item_id: str | None = None

    def __post_init__(self) -> None:
        if not self.platform_sku and not self.platform_item_id:
            raise ValueError("ListingRef 至少需要 platform_sku 或 platform_item_id 之一")


@dataclass
class ListingDraft:
    """待上架商品的规范化描述。

    适配器负责把这份规范化数据翻译成各平台要求的格式。
    """

    internal_sku: str
    title: str
    category_id: str
    product_type: str | None = None

    description: str | None = None
    brand: str | None = None

    price: Decimal | None = None
    currency: str | None = None
    quantity: int | None = None

    #: 类目属性键值对
    attributes: dict[str, Any] = field(default_factory=dict)

    #: 图片 URL 列表（顺序即平台展示顺序）
    images: list[str] = field(default_factory=list)

    #: 变体关系
    parent_sku: str | None = None
    variant_attributes: dict[str, str] = field(default_factory=dict)

    #: 平台特有字段逃生舱。
    #:
    #: 格式：{"amazon": {"some_weird_field": "value"}}
    #:
    #: **使用约束**（写进 adapters/AGENTS.md）：
    #:   - 只允许在适配器层读取，业务层不得构造
    #:   - 每个使用的 extras key 必须在 TDD-03 附录登记
    #:   - **Phase 1 禁止使用**（所有字段必须规范化）
    #:
    #: 为什么保留它：平台总有奇怪的特有字段。没有逃生舱，
    #: 要么加字段污染模型，要么改接口。但必须严格限制，
    #: 否则它变成"什么都往里塞"的垃圾桶。
    platform_extras: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ListingPatch:
    """增量更新描述。None 表示该字段不改。"""

    title: str | None = None
    description: str | None = None
    price: Decimal | None = None
    quantity: int | None = None
    attributes: dict[str, Any] | None = None
    images: list[str] | None = None


@dataclass(frozen=True)
class PriceUpdate:
    """单条改价指令。"""

    listing_ref: ListingRef
    price: Decimal
    currency: str


@dataclass
class InventorySnapshot:
    """库存瞬时状态。"""

    platform_sku: str
    available: int = 0
    reserved: int = 0
    inbound: int = 0
    raw: dict[str, Any] | None = None

    @property
    def sellable(self) -> int:
        """可售数量（可用 - 预留）。"""
        return max(self.available - self.reserved, 0)


@dataclass(frozen=True)
class InventoryUpdate:
    """单条库存更新指令。"""

    platform_sku: str
    quantity: int
    #: 是否为"全量设置"（True）还是"增量调整"（False）
    absolute: bool = True


# ============================================================
# 订单
# ============================================================


@dataclass
class OrderItemSnapshot:
    """订单行。"""

    platform_sku: str | None = None
    title: str | None = None
    quantity: int = 1
    unit_price: Decimal = ZERO
    item_total: Decimal = ZERO
    raw: dict[str, Any] | None = None


@dataclass
class OrderSnapshot:
    """订单瞬时状态。

    注意 PII 处理（TDD-02 §8）：
        本结构**不携带买家明文**。适配器在解析时就要做脱敏 ——
        能哈希的哈希（buyer_hash），只做地域分析的只留 region，
        必须留存的加密后放 buyer_encrypted。
        明文地址**不经过本系统**。
    """

    platform_order_id: str
    order_status: OrderStatus | str = OrderStatus.PENDING

    #: 买家标识的不可逆哈希
    buyer_hash: str | None = None
    #: 仅保留地区（用于分析）
    buyer_region: str | None = None
    #: 必须留存的收货信息（已加密），受 30 天删除规则约束
    buyer_encrypted: bytes | None = None

    item_total: Decimal = ZERO
    shipping_total: Decimal = ZERO
    discount_total: Decimal = ZERO
    tax_total: Decimal = ZERO
    grand_total: Decimal = ZERO
    currency: str = "USD"

    order_time: datetime | None = None
    ship_time: datetime | None = None
    settle_time: datetime | None = None

    fulfillment_channel: str | None = None
    items: list[OrderItemSnapshot] = field(default_factory=list)

    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ShipmentConfirmation:
    """发货回传。"""

    platform_order_id: str
    carrier: str
    tracking_number: str
    ship_time: datetime | None = None


# ============================================================
# 财务
# ============================================================


@dataclass
class SettlementBatch:
    """平台结算批次。"""

    settlement_id: str
    period_start: datetime | None = None
    period_end: datetime | None = None
    total_amount: Decimal = ZERO
    currency: str = "USD"
    raw: dict[str, Any] | None = None


@dataclass
class FeeRecord:
    """费用明细（佣金、配送、仓储、广告扣费等）。"""

    fee_type: str
    amount: Decimal = ZERO
    currency: str = "USD"

    platform_order_id: str | None = None
    platform_sku: str | None = None
    settlement_id: str | None = None
    fee_time: datetime | None = None

    description: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class RefundRecord:
    """退款记录。"""

    platform_refund_id: str
    platform_order_id: str | None = None
    amount: Decimal = ZERO
    currency: str = "USD"
    refund_type: str = "FULL"
    reason_code: str | None = None
    status: str = ""
    refunded_at: datetime | None = None
    raw: dict[str, Any] | None = None


@dataclass
class ReturnRecord:
    """退货记录。"""

    platform_return_id: str
    platform_order_id: str | None = None
    platform_sku: str | None = None
    quantity: int = 1
    reason_code: str | None = None
    restock_status: str | None = None
    returned_at: datetime | None = None
    raw: dict[str, Any] | None = None


@dataclass
class MessageRecord:
    """买家消息。"""

    platform_message_id: str
    platform_order_id: str | None = None
    direction: str = "INBOUND"  # INBOUND / OUTBOUND
    subject: str | None = None
    #: 消息正文。**含 PII，落库前必须加密**（TDD-02）。
    content: str | None = None
    sent_at: datetime | None = None
    raw: dict[str, Any] | None = None


@dataclass
class ReportPayload:
    """异步报表结果。"""

    report_id: str
    report_type: ReportType | str = ""
    #: 解析后的行数据
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: 原始内容（CSV 文本或 JSON）
    raw_content: str | None = None
    generated_at: datetime | None = None


# ============================================================
# 广告（Phase 2 预留）
# ============================================================


@dataclass
class AdCampaign:
    """广告活动。"""

    campaign_id: str
    name: str = ""
    status: str = ""
    daily_budget: Decimal | None = None
    currency: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class AdMetricRow:
    """广告指标行（按日/按活动/按关键词）。"""

    date: datetime | None = None
    campaign_id: str | None = None
    impressions: int = 0
    clicks: int = 0
    spend: Decimal = ZERO
    sales: Decimal = ZERO
    orders: int = 0
    currency: str = "USD"
    raw: dict[str, Any] | None = None

    @property
    def acos(self) -> Decimal:
        """ACOS = 广告花费 / 广告销售额。"""
        if self.sales == ZERO:
            return ZERO
        return self.spend / self.sales

    @property
    def roas(self) -> Decimal:
        """ROAS = 广告销售额 / 广告花费。"""
        if self.spend == ZERO:
            return ZERO
        return self.sales / self.spend


# ============================================================
# 通知订阅
# ============================================================


@dataclass
class SubscriptionResult:
    """Webhook / SQS 订阅结果。"""

    ok: bool
    subscription_id: str | None = None
    topics: list[str] = field(default_factory=list)
    message: str = ""
    raw: dict[str, Any] | None = None
