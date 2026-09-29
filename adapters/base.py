"""平台适配器抽象契约（TDD-03 §3）。

核心设计（TDD-01 原则 1）：
    平台差异**只在适配器内**。业务代码出现 `if platform == "amazon"`
    即视为设计缺陷。

接口粒度的取舍（TDD-03 §1.1）：
    中粒度（约 30 个方法）+ 能力矩阵。

    - 太细（每平台一套接口）→ 业务层变成一堆 isinstance 判断
    - 太粗（只有几个通用方法）→ 业务层被迫写平台判断
    - 中粒度 + 能力声明 → 业务层按**能力**分支，新增平台自动复用编排

方法返回联合类型（`AdapterResult[X] | AdapterResult[AsyncSubmission]`）
而非统一类型的原因（TDD-03 §3.3 取舍 1）：
    NATIVE 和 ASYNC 的后续编排完全不同 —— 前者拿到结果就结束，
    后者要存 submission_id 并轮询。强行统一只是把判断从前置移到后置，
    没有消除。显式联合类型让 mypy 能做穷尽性检查。

硬性约束（写进 adapters/AGENTS.md）：
    1. 所有方法返回 AdapterResult，携带 raw 报文
    2. 所有写方法必须显式传 idempotency_key
    3. **不允许抛出 AdapterError 以外的异常**（由 translate_errors 强制）
    4. 适配器无状态（不缓存跨请求数据）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol

from adapters.capabilities import Capability, CapabilityMatrix
from adapters.errors import (
    ErrorCategory,
    UnsupportedCapabilityError,
)
from adapters.types import (
    AdCampaign,
    AdapterResult,
    AdMetricRow,
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
from core.constants import Platform

__all__ = [
    "ShopContext",
    "PlatformClient",
    "NotificationCallback",
    "PlatformAdapter",
]


# ============================================================
# 上下文与依赖
# ============================================================


@dataclass(frozen=True, slots=True)
class ShopContext:
    """店铺上下文。适配器所需的最小环境信息。

    **不含凭据** —— 凭据由 PlatformClient 持有，
    且只在构造请求头时短暂解密（TDD-06 §2.1：
    "凭据只在内存中存在最小时间"）。
    """

    shop_id: int
    tenant_id: int
    platform: str
    region: str = "OTHER"
    #: IANA 时区名，用于报表的"店铺本地日"计算
    timezone: str = "UTC"
    #: 平台市场标识（Amazon 的 marketplaceId 等）
    marketplace_id: str | None = None

    @property
    def is_real_platform(self) -> bool:
        return self.platform != Platform.MOCK.value


class PlatformClient(Protocol):
    """带限流与重试的 HTTP 客户端协议。

    适配器**必须**通过 client 发请求，禁止裸用 httpx ——
    否则会绕过限流控制（TDD-01 §3.3）。
    """

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """发起请求，返回解析后的 JSON。

        Raises:
            AdapterError: 所有错误已转换为统一异常。
        """
        ...

    async def download(self, url: str) -> bytes:
        """下载文件（报表、Feed 文档）。"""
        ...

    async def upload(self, url: str, content: bytes) -> None:
        """上传文件（Feed 文档）。"""
        ...


@dataclass(frozen=True, slots=True)
class NotificationCallback:
    """Webhook / SQS 回调配置。"""

    url: str
    secret: str | None = None
    #: 订阅的 topic 列表（平台命名）
    topics: tuple[str, ...] = ()


# ============================================================
# 抽象基类
# ============================================================


class PlatformAdapter(ABC):
    """所有平台适配器的统一契约。

    实现约束：
        - 无状态（不缓存跨请求数据），保证可水平扩展
        - 所有写操作必须支持 idempotency_key
        - 所有异常必须是 AdapterError 子类
        - 所有返回必须携带 raw 报文
    """

    #: 平台标识（对应 core.constants.Platform 的值）
    platform: ClassVar[str] = ""

    #: 展示名
    display_name: ClassVar[str] = ""

    def __init__(self, shop: ShopContext, client: PlatformClient) -> None:
        self.shop = shop
        self.client = client
        self._caps: CapabilityMatrix | None = None

    # ========================================================
    # 元信息
    # ========================================================

    @classmethod
    @abstractmethod
    def capabilities(cls) -> CapabilityMatrix:
        """声明本平台支持的能力。

        必须**完整覆盖** Capability 枚举（未声明的按 UNSUPPORTED 处理，
        但契约测试会检查是否显式声明，避免"忘记写"被当成"不支持"）。
        """

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """连通性与授权有效性检查。

        必须实现 —— Token 过期是最常见的生产事故，
        平台不会主动通知，只能靠定时检查发现。
        """

    @property
    def caps(self) -> CapabilityMatrix:
        """能力矩阵（缓存实例）。"""
        if self._caps is None:
            self._caps = type(self).capabilities()
        return self._caps

    def _require(self, cap: Capability, *, fallback: str = "") -> None:
        """断言能力可用，否则抛 UnsupportedCapabilityError。

        这是"能力驱动"的落点：业务层不需要知道平台名，
        适配器自己会告诉它"这个能力我做不到，建议这样降级"。
        """
        state = self.caps.get(cap)
        if not state.callable:
            raise UnsupportedCapabilityError(
                category=ErrorCategory.UNSUPPORTED_CAPABILITY,
                message=(
                    f"{self.display_name or self.platform} 不支持 {cap.value}"
                    f"（当前状态：{state.label}）"
                ),
                platform=self.platform,
                capability=cap.value,
                suggested_fallback=fallback
                or ("生成人工待办" if state.needs_human else "该功能对本平台关闭"),
            )

    def _not_implemented(self, method: str) -> None:
        """Phase 1 占位方法。"""
        raise UnsupportedCapabilityError(
            category=ErrorCategory.NOT_IMPLEMENTED,
            message=f"{self.platform}.{method} 尚未实现（Phase 2 内容）",
            platform=self.platform,
            capability=method,
            suggested_fallback="该功能计划在 Phase 2 提供",
        )

    # ========================================================
    # 商品与类目
    # ========================================================

    @abstractmethod
    async def fetch_category_tree(
        self, *, parent_id: str | None = None, force_refresh: bool = False
    ) -> AdapterResult[list[CategoryNode]]:
        """拉取类目树。parent_id 为空时返回根节点。"""

    @abstractmethod
    async def fetch_category_schema(
        self, *, category_id: str, product_type: str | None = None
    ) -> AdapterResult[CategorySchema]:
        """拉取类目属性定义（必填项、枚举、格式）。

        Amazon 对应 Product Type Definitions API。
        平台不支持时抛 UnsupportedCapabilityError。
        """

    async def check_listing_eligibility(
        self,
        *,
        category_id: str,
        brand: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> AdapterResult[EligibilityResult]:
        """上架资格校验（类目是否开放、品牌是否需授权、是否需审核）。"""
        self._require(Capability.LISTING_ELIGIBILITY_CHECK)
        raise NotImplementedError

    # ========================================================
    # Listing 读
    # ========================================================

    @abstractmethod
    async def fetch_listings(
        self, *, identifiers: list[str], identifier_type: IdentifierType
    ) -> AdapterResult[list[ListingSnapshot]]:
        """按标识批量查询 Listing。

        单次上限由适配器内部切片处理 —— 业务层不需要知道
        平台是 20 个一批还是 50 个一批。
        """

    async def search_listings(
        self,
        *,
        filters: dict[str, Any] | None = None,
        page_token: str | None = None,
    ) -> AdapterResult[list[ListingSnapshot]]:
        """条件搜索 Listing。

        平台不支持搜索时，适配器内部可用 fetch 变通。
        """
        self._require(Capability.LISTING_SEARCH)
        raise NotImplementedError

    # ========================================================
    # Listing 写
    # ========================================================

    @abstractmethod
    async def create_listing(
        self,
        *,
        draft: ListingDraft,
        idempotency_key: str,
        validate_only: bool = False,
    ) -> AdapterResult[ListingSnapshot] | AdapterResult[AsyncSubmission]:
        """创建 Listing。

        返回类型取决于能力：
            NATIVE → AdapterResult[ListingSnapshot]
            ASYNC  → AdapterResult[AsyncSubmission]（需轮询）

        validate_only=True 时只校验不提交。平台支持时用平台校验，
        不支持时用本地 schema 校验，并在 warnings 中标注。
        """

    @abstractmethod
    async def update_listing(
        self,
        *,
        listing_ref: ListingRef,
        patch: ListingPatch,
        idempotency_key: str,
    ) -> AdapterResult[ListingSnapshot] | AdapterResult[AsyncSubmission]:
        """增量更新。patch 中为 None 的字段表示不改。"""

    @abstractmethod
    async def delete_listing(
        self, *, listing_ref: ListingRef, idempotency_key: str
    ) -> AdapterResult[None] | AdapterResult[AsyncSubmission]:
        """下架/删除。

        注意：多数平台是"下架"而非真删除，
        语义差异在适配器内处理，业务层统一按"删除"理解。
        """

    @abstractmethod
    async def update_price(
        self, *, updates: list[PriceUpdate], idempotency_key: str
    ) -> AdapterResult[BatchResult] | AdapterResult[AsyncSubmission]:
        """批量改价。

        为什么是批量接口：跨境改价经常几百上千条。
        如果接口是单条，业务层要自己循环，而"分片、限流、
        部分失败处理、断点续传"这些逻辑每个调用方都要重写一遍。
        """

    @abstractmethod
    async def poll_submission(
        self, *, submission_id: str
    ) -> AdapterResult[dict[str, Any]]:
        """查询异步提交状态。

        返回结构需含逐行结果 —— **部分失败必须能定位到具体行**，
        否则无法生成有意义的待办。
        """

    # ========================================================
    # 库存
    # ========================================================

    @abstractmethod
    async def fetch_inventory(
        self, *, identifiers: list[str]
    ) -> AdapterResult[list[InventorySnapshot]]:
        """查询库存。"""

    @abstractmethod
    async def update_inventory(
        self, *, updates: list[InventoryUpdate], idempotency_key: str
    ) -> AdapterResult[BatchResult] | AdapterResult[AsyncSubmission]:
        """更新库存（批量）。"""

    # ========================================================
    # 订单
    # ========================================================

    @abstractmethod
    async def fetch_orders(
        self,
        *,
        time_range: TimeRange,
        statuses: list[str] | None = None,
        page_token: str | None = None,
    ) -> AdapterResult[list[OrderSnapshot]]:
        """按时间范围拉订单（增量同步主路径）。"""

    @abstractmethod
    async def fetch_order_detail(
        self, *, order_ids: list[str]
    ) -> AdapterResult[list[OrderSnapshot]]:
        """拉订单明细（含金额拆分）。

        注意：返回结构中的买家信息已按 PII 规则处理（TDD-02 §8），
        明文地址不经过本系统。
        """

    @abstractmethod
    async def confirm_shipment(
        self, *, shipments: list[ShipmentConfirmation], idempotency_key: str
    ) -> AdapterResult[BatchResult]:
        """回传发货信息（运单号、承运商）。"""

    # ========================================================
    # 结算与财务
    # ========================================================

    async def fetch_settlements(
        self, *, time_range: TimeRange, page_token: str | None = None
    ) -> AdapterResult[list[SettlementBatch]]:
        """拉结算批次。平台无此接口时抛 UnsupportedCapabilityError。"""
        self._require(Capability.SETTLEMENT_READ)
        raise NotImplementedError

    async def fetch_fees(
        self,
        *,
        settlement_id: str | None = None,
        time_range: TimeRange | None = None,
        page_token: str | None = None,
    ) -> AdapterResult[list[FeeRecord]]:
        """拉费用明细（佣金、配送、仓储、广告扣费）。"""
        self._require(Capability.FEE_READ)
        raise NotImplementedError

    async def request_report(
        self, *, report_type: ReportType, params: dict[str, Any], idempotency_key: str
    ) -> AdapterResult[AsyncSubmission]:
        """请求异步报表。

        Amazon 的结算/退款/费用主要靠这个（Reports API 是异步的）。
        """
        self._require(Capability.REPORT_REQUEST)
        raise NotImplementedError

    async def fetch_report(self, *, report_id: str) -> AdapterResult[ReportPayload]:
        """获取报表结果。

        未就绪时抛 AdapterError(category=SERVICE_UNAVAILABLE) 表示可重试。
        """
        self._require(Capability.REPORT_REQUEST)
        raise NotImplementedError

    # ========================================================
    # 售后
    # ========================================================

    async def fetch_refunds(
        self, *, time_range: TimeRange, page_token: str | None = None
    ) -> AdapterResult[list[RefundRecord]]:
        """拉退款记录。"""
        self._require(Capability.REFUND_READ)
        raise NotImplementedError

    async def fetch_returns(
        self, *, time_range: TimeRange, page_token: str | None = None
    ) -> AdapterResult[list[ReturnRecord]]:
        """拉退货记录（含退货原因码、是否可再售）。"""
        self._require(Capability.RETURN_READ)
        raise NotImplementedError

    async def execute_refund(
        self, *, refund: RefundRecord, idempotency_key: str
    ) -> AdapterResult[RefundRecord]:
        """执行退款。

        **重要**：Amazon 不支持此能力（`REFUND_CREATE` = UNSUPPORTED），
        调用会抛 UnsupportedCapabilityError。
        业务层捕获后必须生成人工待办，而不是当成失败。

        这是 PRD 12.5"退换货自动化"在 Amazon 上必须降级的地方。
        """
        self._require(
            Capability.REFUND_CREATE,
            fallback="已生成人工退款待办，请到平台后台手动处理",
        )
        raise NotImplementedError

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
        """向买家发送消息（限平台允许的模板类型）。"""
        self._require(Capability.MESSAGE_SEND)
        raise NotImplementedError

    async def fetch_messages(
        self,
        *,
        order_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> AdapterResult[list[MessageRecord]]:
        """读取买家消息。

        **重要**：Amazon 不支持此能力（官方无历史消息读取 API），
        调用会抛 UnsupportedCapabilityError。
        Amazon 的买家消息只能通过平台转发到注册邮箱，走邮件解析。
        """
        self._require(
            Capability.MESSAGE_READ,
            fallback="该平台不支持读取历史消息，建议配置平台消息转发到邮箱",
        )
        raise NotImplementedError

    # ========================================================
    # 广告（Phase 2 预留）
    # ========================================================

    async def fetch_ad_campaigns(self, **kwargs: Any) -> AdapterResult[list[AdCampaign]]:
        """拉取广告活动。Phase 2 内容。"""
        self._not_implemented("fetch_ad_campaigns")

    async def fetch_ad_reports(
        self,
        *,
        report_type: ReportType,
        time_range: TimeRange,
        **kwargs: Any,
    ) -> AdapterResult[list[AdMetricRow]]:
        """拉取广告报表。Phase 2 内容。"""
        self._not_implemented("fetch_ad_reports")

    # ========================================================
    # 通知
    # ========================================================

    async def subscribe_notifications(
        self, *, callback: NotificationCallback
    ) -> AdapterResult[SubscriptionResult]:
        """注册 Webhook / SQS 订阅。"""
        self._require(Capability.NOTIFICATION_SUBSCRIBE)
        raise NotImplementedError

    # ========================================================
    # 辅助
    # ========================================================

    def describe(self) -> dict[str, Any]:
        """适配器能力摘要（排障与前端展示用）。"""
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "shop_id": self.shop.shop_id,
            "region": self.shop.region,
            "capabilities": self.caps.as_dict(),
            "capability_summary": self.caps.summary(),
        }

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} shop_id={self.shop.shop_id} "
            f"region={self.shop.region}>"
        )


# 便于类型标注：某些平台返回的 raw 结构
RawDict = dict[str, Any]

# 时间类型别名（便于阅读）
Timestamp = datetime
