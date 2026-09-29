# TDD-03 平台适配器契约

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0（设计评审稿） |
| 创建日期 | 2026-09-28 |
| 上游文档 | `PRD-AI全链路电商自动化平台-v1.1.md`、`TDD-01-技术设计总纲.md`、`TDD-02-数据模型设计.md` |
| 文档编号 | TDD-03 |
| 严重度 | **阻塞级**（适配器接口定错，所有业务代码要返工） |
| 状态 | **待评审，未开工** |

---

## 0. 这份文档要解决什么问题

PRD v1.1 第 10 章写了"用适配器隔离平台差异"，但只到"策略"层面。真正写代码时会立刻卡在：

- `PlatformAdapter` 到底有哪些方法？参数是什么类型？
- Amazon 的 429 和其他平台的限流，业务层怎么统一处理？
- 有的平台能批量改价，有的只能单个改，接口怎么设计才不尴尬？
- 授权还没下来，怎么让业务代码先跑起来？

本文档给出**可直接编码的答案**。核心产出三件：**接口定义**、**错误分类**、**能力矩阵**。

---

## 1. 设计原则（本册专属）

| # | 原则 | 说明 |
|---|---|---|
| A1 | **接口按业务能力分组，不按平台 API 分组** | 不出现 `get_listings_item()` 这种直译 API 的方法名；业务层不该知道平台有几种 API |
| A2 | **能力差异用能力矩阵声明，不用接口分裂** | 所有适配器实现同一套接口；不支持的能力通过 `capabilities()` 声明，调用时返回 `UnsupportedCapabilityError` |
| A3 | **原始报文必须回传** | 每个返回值含 `raw` 字段（原始 JSON），存入 `raw_payloads`，实现"规范化数据可重建" |
| A4 | **错误必须分类，不能透传原始码** | 业务层只看 `AdapterError.category`，映射表在各自 `error_map.py` |
| A5 | **所有写操作显式传幂等键** | 接口签名强制要求，无默认值 |
| A6 | **适配器无状态** | 不缓存跨请求状态；状态存数据库或 Redis，保证可水平扩展 |
| A7 | **限流在客户端内建** | 业务层不感知限流；429 由 `client.py` 内部退避处理，超限才上抛 |

### 1.1 关于"接口粒度"的关键判断

**这是一个必须现在拍板的问题**：接口太细 → 每加一个平台要写 50 个方法；接口太粗 → 业务代码被迫写平台判断。

**本设计的取舍：中粒度 + 能力矩阵**。

具体做法：

- 接口方法约 **30 个**（按业务能力分组，见第 3 节）
- 每个方法**只接受规范化后的入参**，返回**规范化后的结果**
- 平台特有的额外参数走 `platform_extras: dict` 逃生舱（明确标注、受限使用）
- 不支持的能力不删方法，而是声明能力并在运行时抛 `UnsupportedCapabilityError`

**为什么不给每个平台定义不同接口**：业务层（`services/`）会变成一堆 `if isinstance(adapter, AmazonAdapter)`，违反原则 1。

---

## 2. 适配器能力矩阵

### 2.1 能力声明机制

```python
# adapters/capabilities.py
from enum import StrEnum


class Capability(StrEnum):
    """平台能力枚举。适配器声明支持哪些，业务层据此决策。"""

    # ===== 读：商品与刊登 =====
    LISTING_READ = "listing.read"
    LISTING_SEARCH = "listing.search"
    CATEGORY_TREE_READ = "category.tree.read"
    CATEGORY_SCHEMA_READ = "category.schema.read"      # 属性定义
    LISTING_ELIGIBILITY_CHECK = "listing.eligibility.check"  # 类目/品牌授权校验

    # ===== 读：订单与履约 =====
    ORDER_READ = "order.read"
    ORDER_SEARCH = "order.search"
    SHIPMENT_READ = "shipment.read"

    # ===== 读：库存 =====
    INVENTORY_READ = "inventory.read"

    # ===== 读：财务 =====
    SETTLEMENT_READ = "settlement.read"                # 结算批次
    FEE_READ = "fee.read"                              # 费用明细
    REIMBURSEMENT_READ = "reimbursement.read"          # 平台赔付

    # ===== 写：商品与刊登 =====
    LISTING_CREATE = "listing.create"
    LISTING_UPDATE = "listing.update"
    LISTING_DELETE = "listing.delete"
    LISTING_PRICE_UPDATE = "listing.price.update"
    LISTING_BULK_CREATE = "listing.bulk.create"        # 批量（Feed / 批量接口）
    PRICING_RULE_WRITE = "pricing.rule.write"          # 平台侧自动定价

    # ===== 写：库存 =====
    INVENTORY_UPDATE = "inventory.update"
    INVENTORY_BULK_UPDATE = "inventory.bulk.update"

    # ===== 写：订单与售后 =====
    ORDER_SHIP_CONFIRM = "order.ship.confirm"          # 回传发货
    ORDER_CANCEL = "order.cancel"
    REFUND_CREATE = "refund.create"
    REFUND_READ = "refund.read"
    RETURN_READ = "return.read"
    MESSAGE_SEND = "message.send"                      # 给买家发消息
    MESSAGE_READ = "message.read"                      # 读买家消息（多数平台不支持）

    # ===== 广告（Phase 2 预留）=====
    AD_CAMPAIGN_READ = "ad.campaign.read"
    AD_CAMPAIGN_WRITE = "ad.campaign.write"
    AD_REPORT_READ = "ad.report.read"

    # ===== 订阅与通知 =====
    NOTIFICATION_SUBSCRIBE = "notification.subscribe"  # Webhook / SQS 推送
    NOTIFICATION_READ = "notification.read"            # 轮询通知

    # ===== 通用 =====
    BULK_OPERATION = "bulk.operation"                  # 平台支持任何形式的批量
    SANDBOX_AVAILABLE = "sandbox.available"            # 有沙箱环境
```

### 2.2 四类能力状态

PRD v1.1 的 10.2.1 提到能力矩阵，这里给出**精确的语义定义**，避免"支持/不支持"这种二元说法掩盖大量灰色地带：

| 状态 | 枚举值 | 语义 | 业务层应如何应对 |
|---|---|---|---|
| 原生支持 | `NATIVE` | 有专用 API，可实时读写 | 直接调用 |
| 异步支持 | `ASYNC` | 有 API 但结果异步（如 Amazon Feed） | 提交后轮询 `submission_id` |
| 推送支持 | `WEBHOOK` | 平台主动推送，无需轮询 | 注册订阅 + 消费队列 |
| 文件导入 | `FILE_IMPORT` | 只能上传/下载文件（Excel/CSV） | 生成文件 → 人工上传 → 回收结果 |
| 手动 | `MANUAL` | 无 API，需人工在后台操作 | 系统生成待办，人工执行后回填 |
| 不支持 | `UNSUPPORTED` | 无任何途径 | 功能对该平台关闭 |

**这与 PRD 的对应关系**：PRD 10.2.1 写的 `native/async/webhook/file_import/manual/unsupported`，本册保持完全一致，并补充了"业务层应对"列——这一列才是开发真正需要的东西。

### 2.3 平台能力矩阵（Phase 1 实况）

**必须说明的事实边界**：下表基于官方文档公开信息整理，**标注 ⚠️ 的项需在拿到授权后实测确认**。本表不是承诺，是开发假设。

| 能力 | Amazon (SP-API) | TikTok Shop | Temu | Shopee | Lazada | 速卖通 | Shopify |
|---|---|---|---|---|---|---|---|
| 商品/Listing 读 | NATIVE | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE |
| 类目树 | NATIVE | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE(集合) |
| 属性 schema 动态获取 | NATIVE(PTD) | NATIVE | UNSUPPORTED ⚠️ | FILE_IMPORT ⚠️ | FILE_IMPORT ⚠️ | UNSUPPORTED ⚠️ | NATIVE |
| 上架创建 | ASYNC(Feeds) | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE |
| 批量上架 | ASYNC(Feeds) | ASYNC | NATIVE ⚠️ | ASYNC | ASYNC | NATIVE ⚠️ | ASYNC(Bulk) |
| 价格更新 | NATIVE / ASYNC | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE |
| 库存更新 | NATIVE / ASYNC | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE |
| 订单读 | NATIVE | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE |
| 订单搜索 | NATIVE(Limited) | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE |
| 发货回传 | NATIVE | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE |
| 结算/费用读 | NATIVE(Reports) | NATIVE | MANUAL ⚠️ | NATIVE | NATIVE | NATIVE ⚠️ | NATIVE |
| 退款读 | NATIVE(Reports) | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE |
| 退款写（执行） | UNSUPPORTED ¹ | NATIVE | NATIVE ⚠️ | NATIVE | NATIVE | NATIVE | NATIVE |
| 买家消息发送 | NATIVE(Messaging) | NATIVE | UNSUPPORTED ⚠️ | NATIVE | NATIVE | NATIVE ⚠️ | NATIVE |
| 买家消息读取 | **UNSUPPORTED ²** | NATIVE | UNSUPPORTED ⚠️ | NATIVE ⚠️ | NATIVE ⚠️ | UNSUPPORTED ⚠️ | NATIVE |
| Webhook 订阅 | NATIVE(SNS/SQS) | NATIVE | UNSUPPORTED ⚠️ | NATIVE | NATIVE | UNSUPPORTED ⚠️ | NATIVE |
| 广告 API | NATIVE(独立授权) | NATIVE | MANUAL ⚠️ | NATIVE | NATIVE | NATIVE ⚠️ | NATIVE |
| 沙箱环境 | 有限 ³ | 有 | UNSUPPORTED ⚠️ | 有 | 有 | 有 | 有 |

**必须解释的三条**：

1. **¹ Amazon 卖家侧无"由卖家直接发起退款"的 API**。退款通常由平台处理，卖家侧只能读退款记录。所以 `REFUND_CREATE` 对 Amazon 是 `UNSUPPORTED`，业务层应生成"人工去后台处理"的待办。**这是 PRD 12.5"退换货自动化"在 Amazon 上必须降级的地方，需要评审确认。**

2. **² Amazon Messaging API 不支持通用历史消息读取**。官方只提供 `getMessagingActionsForOrder`（查可执行动作）与发送消息，**没有"拉取买家和卖家会话全文"的接口**。因此 `MESSAGE_READ` 在 Amazon 上是 `UNSUPPORTED`——这一点在 PRD v1.1 已修正，本册固化。买家的消息只能通过 Amazon 转发到注册邮箱，走邮件解析（属于 `FILE_IMPORT` 变体，Phase 2 再评估）。

3. **³ Amazon SP-API 沙箱覆盖不全**：部分接口有静态沙箱，多数只有生产环境。这是 ADR-005（Mock 适配器）的另一个理由。

**关于"⚠️ 待确认"的诚实交代**：Temu、速卖通、Shopee 的部分能力我无法从公开文档确认到接口级别。上表标注 ⚠️ 的项，**必须在技术预研阶段用真实账号实测**。在实测完成前，这些项对开发而言等同于"未知"，不能作为排期依据。

### 2.4 能力矩阵的使用方式

```python
# services/listing_service.py 的正确写法
async def publish_listing(adapter: PlatformAdapter, listing: ListingDraft) -> PublishResult:
    caps = adapter.capabilities()

    # 1. 能力检查前置，而不是调用后处理异常
    if caps.get(Capability.LISTING_CREATE) == CapabilityState.UNSUPPORTED:
        raise BusinessError(
            code="PLATFORM_NOT_SUPPORT_CREATE",
            message=f"{adapter.platform} 不支持通过 API 上架",
            action="已生成人工上架待办",
        )

    # 2. 异步平台走不同编排路径
    if caps.get(Capability.LISTING_CREATE) == CapabilityState.ASYNC:
        return await _publish_via_async_flow(adapter, listing)

    return await adapter.create_listing(listing)
```

**关键点**：业务层的分支条件是**能力**，不是**平台名**。这样新增平台时，只要它声明同样的能力，编排逻辑自动复用。

### 2.5 能力矩阵的存储

能力矩阵**既在代码中声明，也落库**：

- **代码声明**（`adapters/{platform}/adapter.py` 中的 `capabilities()`）：权威来源，随代码版本走
- **数据库缓存**（`platform_capabilities` 表）：供前端展示与任务调度决策，避免前端 import Python 代码

**为什么不只用代码**：前端要展示"TikTok 支持自动退款，Amazon 需要人工"，这个信息要么走 API 暴露，要么落库。落库更简单，且能在启动时做一致性校验。

```sql
-- 补 TDD-02 的遗漏表（TDD-02 未列，本册要求新增，需评审确认）
CREATE TABLE platform_capabilities (
    id                BIGSERIAL PRIMARY KEY,
    platform          VARCHAR(32) NOT NULL,
    capability        VARCHAR(64) NOT NULL,
    state             VARCHAR(32) NOT NULL
                      CHECK (state IN ('native','async','webhook','file_import',
                                        'manual','unsupported')),
    -- 运行期覆盖：临时降级（如平台故障）不改进代码
    disabled_until    TIMESTAMPTZ,
    disabled_reason   TEXT,
    -- 元数据
    notes             TEXT,
    source_ref        VARCHAR(512),      -- 官方文档链接（可追溯）
    verified_at       TIMESTAMPTZ,       -- 实测确认时间（⚠️ 项必须填）
    code_version      VARCHAR(64),       -- 声明该能力的代码版本
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, capability)
);
```

**`disabled_until` 的用途**：平台某接口临时故障时，可以只标记该能力不可用（降级为 manual），而不用改代码发版。这是一个性价比很高的设计。

---

## 3. 适配器接口定义

### 3.1 统一返回结构

所有方法返回 `AdapterResult`，携带重试与溯源信息：

```python
# adapters/base.py
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class AdapterResult(Generic[T]):
    """适配器统一返回。业务层不接触原始 HTTP 响应。"""

    data: T
    raw: dict | list | None = None          # 原始报文 → raw_payloads
    request_id: str | None = None           # 平台请求 ID（排障用）
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 分页
    next_token: str | None = None           # 下一页游标
    has_more: bool = False
    # 数据质量
    partial: bool = False                   # 部分成功（如 Feed 部分行失败）
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AsyncSubmission:
    """异步写入的提交凭证（Amazon Feed / TikTok 批量任务）。"""

    submission_id: str                      # Feed ID / task_id
    submitted_at: datetime
    expected_ready_at: datetime | None = None
    poll_hint_seconds: int = 60             # 建议轮询间隔
    raw: dict | None = None
```

**为什么 `raw` 是 `dict | list | None` 而不是 `Any`**：规范化过程本身就是从 JSON 结构提取。若某个平台返回非 JSON（如 CSV 报表），适配器负责先解析为 JSON 结构，业务层拿到的始终是结构化数据。

**为什么需要 `partial` 和 `warnings`**：Amazon Feed 的典型返回是"1000 行中 3 行失败"。这不是成功也不是失败，业务层必须知道，否则会上报"上架成功"而实际有 3 个 SKU 没上。

### 3.2 抽象基类

```python
# adapters/base.py
from abc import ABC, abstractmethod


class PlatformAdapter(ABC):
    """所有平台适配器的统一契约。

    实现约束：
    - 无状态（不缓存跨请求数据）
    - 所有写操作必须支持 idempotency_key
    - 所有异常必须是 AdapterError 子类
    - 所有返回必须携带 raw 报文
    """

    platform: ClassVar[str]
    display_name: ClassVar[str]

    def __init__(self, shop: ShopContext, client: PlatformClient) -> None:
        self.shop = shop
        self.client = client

    # ==================== 元信息 ====================

    @classmethod
    @abstractmethod
    def capabilities(cls) -> dict[Capability, CapabilityState]:
        """声明本平台支持的能力。必须完整覆盖 Capability 枚举。"""

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """连通性与授权有效性检查。返回剩余配额、Token 有效期等。"""

    # ==================== 商品与类目 ====================

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

    @abstractmethod
    async def check_listing_eligibility(
        self, *, category_id: str, brand: str | None = None, attributes: dict
    ) -> AdapterResult[EligibilityResult]:
        """上架资格校验（类目是否开放、品牌是否需授权、是否需审核）。"""

    # ==================== Listing 读 ====================

    @abstractmethod
    async def fetch_listings(
        self, *, identifiers: list[str], identifier_type: IdentifierType
    ) -> AdapterResult[list[ListingSnapshot]]:
        """按标识批量查询 Listing。单次上限由适配器内部切片处理。"""

    @abstractmethod
    async def search_listings(
        self, *, filters: ListingFilter, page_token: str | None = None
    ) -> AdapterResult[list[ListingSnapshot]]:
        """条件搜索 Listing。平台不支持搜索时可用 fetch 变通（适配器内部处理）。"""

    # ==================== Listing 写 ====================

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
        - NATIVE → AdapterResult[ListingSnapshot]
        - ASYNC  → AdapterResult[AsyncSubmission]（需轮询）

        validate_only=True 时只校验不提交（平台支持时用平台校验，
        不支持时用本地 schema 校验，结果标注 in `warnings`）。
        """

    @abstractmethod
    async def update_listing(
        self, *, listing_ref: ListingRef, patch: ListingPatch, idempotency_key: str
    ) -> AdapterResult[ListingSnapshot] | AdapterResult[AsyncSubmission]:
        """增量更新。patch 为 None 的字段表示不改。"""

    @abstractmethod
    async def delete_listing(
        self, *, listing_ref: ListingRef, idempotency_key: str
    ) -> AdapterResult[None] | AdapterResult[AsyncSubmission]:
        """下架/删除。注意：多数平台是"下架"而非真删除，语义差异在适配器内处理。"""

    @abstractmethod
    async def update_price(
        self, *, updates: list[PriceUpdate], idempotency_key: str
    ) -> AdapterResult[BatchResult] | AdapterResult[AsyncSubmission]:
        """批量改价。单条也可走此接口（适配器内部决定逐条还是批处理）。"""

    @abstractmethod
    async def poll_submission(
        self, *, submission_id: str
    ) -> AdapterResult[SubmissionStatus]:
        """查询异步提交状态。状态含逐行结果（部分失败必须能定位到行）。"""

    # ==================== 库存 ====================

    @abstractmethod
    async def fetch_inventory(
        self, *, identifiers: list[str]
    ) -> AdapterResult[list[InventorySnapshot]]:
        """查询库存。"""

    @abstractmethod
    async def update_inventory(
        self, *, updates: list[InventoryUpdate], idempotency_key: str
    ) -> AdapterResult[BatchResult] | AdapterResult[AsyncSubmission]:
        """更新库存。"""

    # ==================== 订单 ====================

    @abstractmethod
    async def fetch_orders(
        self, *, time_range: TimeRange, statuses: list[OrderStatus] | None = None,
        page_token: str | None = None,
    ) -> AdapterResult[list[OrderSnapshot]]:
        """按时间范围拉订单（增量同步主路径）。"""

    @abstractmethod
    async def fetch_order_detail(
        self, *, order_ids: list[str]
    ) -> AdapterResult[list[OrderSnapshot]]:
        """拉订单明细（含地址、金额拆分）。注意 PII 处理（TDD-02 第 8 节）。"""

    @abstractmethod
    async def confirm_shipment(
        self, *, shipments: list[ShipmentConfirmation], idempotency_key: str
    ) -> AdapterResult[BatchResult]:
        """回传发货信息（运单号、承运商）。"""

    # ==================== 结算与财务 ====================

    @abstractmethod
    async def fetch_settlements(
        self, *, time_range: TimeRange, page_token: str | None = None
    ) -> AdapterResult[list[SettlementBatch]]:
        """拉结算批次。平台无此接口时抛 UnsupportedCapabilityError。"""

    @abstractmethod
    async def fetch_fees(
        self, *, settlement_id: str | None = None, time_range: TimeRange | None = None,
        page_token: str | None = None,
    ) -> AdapterResult[list[FeeRecord]]:
        """拉费用明细（佣金、配送、仓储、广告扣费）。"""

    @abstractmethod
    async def request_report(
        self, *, report_type: ReportType, params: dict, idempotency_key: str
    ) -> AdapterResult[AsyncSubmission]:
        """请求异步报表（Amazon 结算/退款主要靠这个）。"""

    @abstractmethod
    async def fetch_report(
        self, *, report_id: str
    ) -> AdapterResult[ReportPayload]:
        """获取报表结果。未就绪时抛 ReportNotReadyError（可重试）。"""

    # ==================== 售后 ====================

    @abstractmethod
    async def fetch_refunds(
        self, *, time_range: TimeRange, page_token: str | None = None
    ) -> AdapterResult[list[RefundRecord]]:
        """拉退款记录。"""

    @abstractmethod
    async def fetch_returns(
        self, *, time_range: TimeRange, page_token: str | None = None
    ) -> AdapterResult[list[ReturnRecord]]:
        """拉退货记录（含退货原因码、是否可再售）。"""

    @abstractmethod
    async def execute_refund(
        self, *, refund: RefundRequest, idempotency_key: str
    ) -> AdapterResult[RefundRecord]:
        """执行退款。

        重要：Amazon 不支持此能力，必须抛 UnsupportedCapabilityError。
        业务层捕获后生成人工待办。
        """

    # ==================== 消息 ====================

    @abstractmethod
    async def send_message(
        self, *, order_id: str, template: MessageTemplate, payload: dict,
        idempotency_key: str,
    ) -> AdapterResult[MessageRecord]:
        """向买家发送消息（限平台允许的模板类型）。"""

    @abstractmethod
    async def fetch_messages(
        self, *, order_id: str | None = None, time_range: TimeRange | None = None
    ) -> AdapterResult[list[MessageRecord]]:
        """读取买家消息。

        Amazon 不支持（官方无历史消息读取 API），抛 UnsupportedCapabilityError。
        """

    # ==================== 广告（Phase 2 预留）====================

    @abstractmethod
    async def fetch_ad_campaigns(self, **kwargs) -> AdapterResult[list[AdCampaign]]:
        raise NotImplementedError("Phase 2")

    @abstractmethod
    async def fetch_ad_reports(
        self, *, report_type: ReportType, time_range: TimeRange, **kwargs
    ) -> AdapterResult[list[AdMetricRow]]:
        raise NotImplementedError("Phase 2")

    # ==================== 通知 ====================

    @abstractmethod
    async def subscribe_notifications(
        self, *, topics: list[str], callback: NotificationCallback
    ) -> AdapterResult[SubscriptionResult]:
        """注册 Webhook / SQS 订阅。"""
```

### 3.3 关键接口的取舍说明

**取舍 1：为什么 `create_listing` 返回联合类型而不是统一类型？**

因为 `NATIVE` 和 `ASYNC` 的后续编排完全不同：`NATIVE` 拿到结果就结束；`ASYNC` 要存 `submission_id` 并轮询。如果强行统一成一个类型（比如都返回带 `submission_id` 的结构），`NATIVE` 场景下 `submission_id` 为 `None`，调用方仍要判断——**只是把判断从前置移到了后置，没有消除**。显式联合类型更好，`mypy` 能做穷尽性检查。

**取舍 2：为什么 `update_price` 和 `update_inventory` 是批量接口？**

跨境平台上架/改价经常是几百上千条。如果接口是单条，业务层就要自己循环，而"分片、限流、部分失败处理、断点续传"这些逻辑每个调用方都要重写一遍。批量接口 + 适配器内部切片是正确抽象。

**取舍 3：为什么 `health_check` 是必需的？**

ADR 与 PRD 都要求"授权有效性监控"。Token 过期是最常见的生产事故——平台不会主动告诉你。必须有统一方法让定时任务检查所有店铺。

**取舍 4：`platform_extras` 逃生舱出现的位置**

```python
@dataclass(slots=True)
class ListingDraft:
    # ... 规范化字段 ...
    platform_extras: dict[str, dict] = field(default_factory=dict)
    """平台特有字段，格式 {platform: {key: value}}。

    使用约束（写进 AGENTS.md）：
    - 只允许在适配器层读取，业务层不得构造
    - 每个使用的 extras key 必须在 TDD-03 附录登记
    - Phase 2 前禁止使用（Phase 1 所有字段必须规范化）
    """
```

**为什么需要它**：平台总有奇怪的特有字段。没有逃生舱，要么加字段污染模型，要么改接口。但必须严格限制，否则它变成"什么都往里塞"的垃圾桶。**Phase 1 禁止使用**是刻意的自我约束。

---

## 4. 统一错误分类

### 4.1 错误分类体系

这是本册第二个核心产出。**业务层只认 `category`，不认平台原始码。**

```python
# adapters/errors.py
from enum import StrEnum


class ErrorCategory(StrEnum):
    """统一错误分类。每个分类对应明确的处理策略。"""

    # ===== 认证与授权（不可重试，需人工介入）=====
    AUTH_INVALID = "auth.invalid"                  # 凭据无效
    AUTH_EXPIRED = "auth.expired"                  # Token 过期
    AUTH_INSUFFICIENT_SCOPE = "auth.scope"         # 权限不足
    AUTH_REFRESH_FAILED = "auth.refresh_failed"    # 刷新失败

    # ===== 限流与配额（可重试，退避）=====
    RATE_LIMITED = "rate.limited"                  # 429
    QUOTA_EXCEEDED = "quota.exceeded"              # 日配额用尽（重试无用）
    CONCURRENCY_LIMIT = "rate.concurrency"         # 并发限制

    # ===== 客户端错误（不可重试，需修数据）=====
    VALIDATION_FAILED = "request.validation"       # 参数不合法
    SCHEMA_MISMATCH = "request.schema"             # 属性 schema 不匹配
    NOT_FOUND = "request.not_found"                # 资源不存在
    CONFLICT = "request.conflict"                  # 冲突（SKU 已存在等）
    IDEMPOTENCY_CONFLICT = "request.idempotency"   # 幂等键冲突（参数不同）
    PRECONDITION_FAILED = "request.precondition"   # 前置条件不满足

    # ===== 业务规则拒绝（不可重试，需人工判断）=====
    BUSINESS_REJECTED = "business.rejected"        # 平台业务规则拒绝
    ELIGIBILITY_DENIED = "business.eligibility"    # 资格不足（品牌/类目授权）
    COMPLIANCE_REQUIRED = "business.compliance"    # 需合规材料

    # ===== 服务端错误（可重试）=====
    SERVER_ERROR = "server.error"                  # 5xx
    TIMEOUT = "server.timeout"
    SERVICE_UNAVAILABLE = "server.unavailable"     # 平台维护
    DEPENDENCY_FAILED = "server.dependency"        # 平台内部依赖故障

    # ===== 部分成功（特殊，需逐行处理）=====
    PARTIAL_SUCCESS = "partial.success"            # 批量部分失败

    # ===== 数据质量（不可重试，需调查）=====
    MALFORMED_RESPONSE = "data.malformed"          # 返回结构异常
    SCHEMA_DRIFT = "data.schema_drift"             # 平台返回结构变了

    # ===== 不支持（不可重试）=====
    UNSUPPORTED_CAPABILITY = "unsupported.capability"
    NOT_IMPLEMENTED = "unsupported.not_implemented"  # Phase 1 占位

    # ===== 未知 =====
    UNKNOWN = "unknown"                            # 未映射的错误码（需补映射）
```

### 4.2 错误处理策略表

**这张表决定重试逻辑，是开发的核心依据**：

| category | 可重试 | 重试策略 | 是否告警 | 业务层动作 |
|---|---|---|---|---|
| `AUTH_INVALID` | ❌ | — | 🔴 立即 | 店铺标记异常，暂停该店所有任务 |
| `AUTH_EXPIRED` | ⚠️ 一次 | 先刷新 Token 再重试一次 | 🟡 失败后 | 刷新成功则继续；失败标记异常 |
| `AUTH_INSUFFICIENT_SCOPE` | ❌ | — | 🔴 立即 | 提示缺少权限，需重新授权 |
| `AUTH_REFRESH_FAILED` | ⚠️ 有限 | 指数退避 3 次 | 🔴 立即 | 暂停任务，通知运维 |
| `RATE_LIMITED` | ✅ | 指数退避 + 抖动，尊重 `Retry-After` | 🟡 连续 5 次 | 无需处理（透明） |
| `QUOTA_EXCEEDED` | ❌ | 计算配额重置时间，延迟到该时间 | 🟠 每日一次 | 任务改期 |
| `CONCURRENCY_LIMIT` | ✅ | 短退避 3 次 | 🟢 | 无需处理 |
| `VALIDATION_FAILED` | ❌ | — | 🟢（批量时 🟠） | 记录错误，跳过该条，继续其余 |
| `SCHEMA_MISMATCH` | ❌ | — | 🟠 | 重新拉取 schema，重新校验 |
| `NOT_FOUND` | ❌ | — | 🟢 | 标记本地记录为已删除 |
| `CONFLICT` | ❌ | — | 🟠 | 人工介入（SKU 冲突） |
| `IDEMPOTENCY_CONFLICT` | ❌ | — | 🔴 立即 | **严重**：同幂等键不同参数，代码 bug |
| `PRECONDITION_FAILED` | ❌ | — | 🟠 | 检查前置状态（如订单未发货不能发货） |
| `BUSINESS_REJECTED` | ❌ | — | 🟠 | 生成待办，人工处理 |
| `ELIGIBILITY_DENIED` | ❌ | — | 🟠 | 生成待办（需申请授权） |
| `COMPLIANCE_REQUIRED` | ❌ | — | 🟠 | 生成待办（需提交材料） |
| `SERVER_ERROR` | ✅ | 指数退避，最多 5 次 | 🟠 最终失败 | 无需处理 |
| `TIMEOUT` | ✅ | 指数退避，最多 3 次 | 🟡 最终失败 | 无需处理（幂等键防重复） |
| `SERVICE_UNAVAILABLE` | ✅ | 长退避（5min 起） | 🟠 | 任务改期 |
| `DEPENDENCY_FAILED` | ✅ | 指数退避 3 次 | 🟠 | 无需处理 |
| `PARTIAL_SUCCESS` | ❌ | — | 🟠 | **逐行处理成功/失败**，失败行生成待办 |
| `MALFORMED_RESPONSE` | ⚠️ 一次 | 重试一次 | 🔴 立即 | 保存原始报文，人工分析 |
| `SCHEMA_DRIFT` | ❌ | — | 🔴 立即 | 冻结该接口，需人工适配 |
| `UNSUPPORTED_CAPABILITY` | ❌ | — | 🟢（预期内） | 走降级路径（生成待办/文件导入） |
| `NOT_IMPLEMENTED` | ❌ | — | 🟢（预期内） | Phase 1 占位，不应在生产触发 |
| `UNKNOWN` | ⚠️ 一次 | 重试一次 | 🔴 立即 | **必须补映射** |

**关键设计：`IDEMPOTENCY_CONFLICT` 和 `SCHEMA_DRIFT` 是红色告警**。前者意味着代码有 bug（同键不同参数），后者意味着平台改了接口。两者都不能静默处理。

### 4.3 异常类层次

```python
# adapters/errors.py


class AdapterError(Exception):
    """适配器异常基类。所有平台异常必须转换为此类或其子类。"""

    def __init__(
        self,
        *,
        category: ErrorCategory,
        message: str,
        platform: str,
        raw_code: str | None = None,       # 平台原始错误码
        raw_message: str | None = None,    # 平台原始错误描述
        http_status: int | None = None,
        retryable: bool | None = None,     # None 时取 category 默认值
        retry_after: int | None = None,    # 秒（来自 Retry-After）
        request_id: str | None = None,
        context: dict | None = None,       # 业务上下文（shop_id、listing_id 等）
        raw_payload: dict | None = None,   # 原始响应报文
    ) -> None:
        ...

    @property
    def retryable(self) -> bool:
        """按 category 得出默认可重试性（可被显式覆盖）。"""

    def to_dict(self) -> dict:
        """序列化，用于日志、审计、前端展示。必须脱敏。"""


# ---- 具体子类（便于业务层精确捕获）----

class AuthenticationError(AdapterError):
    """认证类。category ∈ {AUTH_INVALID, AUTH_EXPIRED, AUTH_INSUFFICIENT_SCOPE,
    AUTH_REFRESH_FAILED}"""


class RateLimitError(AdapterError):
    """限流类。category ∈ {RATE_LIMITED, QUOTA_EXCEEDED, CONCURRENCY_LIMIT}"""

class ValidationError(AdapterError):
    """校验类。category ∈ {VALIDATION_FAILED, SCHEMA_MISMATCH, PRECONDITION_FAILED}
    携带 field_errors: list[FieldError]"""

class UnsupportedCapabilityError(AdapterError):
    """能力不支持。携带 capability: Capability 与 suggested_fallback: str"""

class PartialSuccessError(AdapterError):
    """部分成功。携带 succeeded / failed 明细，业务层必须逐行处理"""

class SchemaDriftError(AdapterError):
    """平台返回结构与预期不符。携带 expected_shape / actual_sample"""

class PlatformServerError(AdapterError):
    """平台服务端错误。可重试"""


# ---- 异常转换装饰器 ----

def translate_errors(platform: str):
    """装饰适配器方法，把所有异常转换为 AdapterError。

    职责：
    1. httpx 异常 → TIMEOUT / SERVER_ERROR
    2. JSON 解析失败 → MALFORMED_RESPONSE
    3. 平台错误码 → 查 error_map → 对应 category
    4. 未映射错误码 → UNKNOWN（并打红色告警日志）
    5. 保底：任何未捕获异常 → UNKNOWN（不允许裸异常逃逸）
    """
```

**硬性要求**：适配器方法**不允许抛出 `AdapterError` 以外的异常**。这条写进 `adapters/AGENTS.md`，由 Codex 生成的代码必须遵守。

### 4.4 错误映射实现（Amazon 示例）

```python
# adapters/amazon/error_map.py
"""Amazon SP-API 错误码映射。

数据来源：官方错误响应文档 + 实测。
未列出的错误码 → UNKNOWN + 红色告警（强制补映射）。
"""

AMAZON_ERROR_MAP: dict[str, ErrorCategory] = {
    # --- 认证 ---
    "Unauthorized": ErrorCategory.AUTH_INVALID,
    "InvalidAccessToken": ErrorCategory.AUTH_EXPIRED,
    "AccessDenied": ErrorCategory.AUTH_INSUFFICIENT_SCOPE,
    "InvalidSignature": ErrorCategory.AUTH_INVALID,

    # --- 限流 ---
    "Throttled": ErrorCategory.RATE_LIMITED,
    "RequestThrottled": ErrorCategory.RATE_LIMITED,
    "QuotaExceeded": ErrorCategory.QUOTA_EXCEEDED,

    # --- 校验 ---
    "InvalidInput": ErrorCategory.VALIDATION_FAILED,
    "InvalidParameterValue": ErrorCategory.VALIDATION_FAILED,
    "MissingParameter": ErrorCategory.VALIDATION_FAILED,
    "InvalidMarketplaceId": ErrorCategory.VALIDATION_FAILED,

    # --- 资源 ---
    "NotFound": ErrorCategory.NOT_FOUND,
    "ResourceNotFound": ErrorCategory.NOT_FOUND,
    "Conflict": ErrorCategory.CONFLICT,

    # --- 业务 ---
    "InvalidListing": ErrorCategory.BUSINESS_REJECTED,
    "RestrictedProduct": ErrorCategory.ELIGIBILITY_DENIED,
    "BrandNotAuthorized": ErrorCategory.ELIGIBILITY_DENIED,
    "CategoryNotOpen": ErrorCategory.ELIGIBILITY_DENIED,
    "ApprovalRequired": ErrorCategory.COMPLIANCE_REQUIRED,

    # --- 服务端 ---
    "InternalFailure": ErrorCategory.SERVER_ERROR,
    "InternalServerError": ErrorCategory.SERVER_ERROR,
    "ServiceUnavailable": ErrorCategory.SERVICE_UNAVAILABLE,
}

# Feed 处理报告中的逐行状态（与 HTTP 错误码不同体系）
AMAZON_FEED_ROW_STATUS_MAP: dict[str, ErrorCategory] = {
    "ACCEPTED": None,  # 成功，无错误
    "INVALID": ErrorCategory.VALIDATION_FAILED,
    "FATAL": ErrorCategory.BUSINESS_REJECTED,
    "WARNING": None,   # 有警告但成功
}
```

**Amazon 特有的坑（必须写进 `adapters/amazon/AGENTS.md`）**：

1. **两套 Header**：SP-API 用 `x-amz-access-token`；Ads API 用 `Amazon-Advertising-API-ClientId` + `Amazon-Advertising-API-Scope`。混用会 401。
2. **Feeds 是异步的**：`createFeedDocument` → `createFeed` → 轮询 `getFeed` 到 `DONE` → `getFeedDocument` 拿 processingReport。**四步缺一不可**，"提交成功"≠"上架成功"。
3. **区域 Base URL 三套**：NA / EU / FE 各自域名，必须按 `shop.region` 路由。
4. **Product Type Definitions 必用**：属性 schema 不能硬编码，必须动态拉取并按 `schema_version` 版本化缓存。
5. **429 会带 `Retry-After`**：必须尊重该值，不能用自己的退避策略硬顶。

---

## 5. Mock 适配器（Phase 1 核心交付）

### 5.1 设计目标

**这不是"假数据占位"**，它承担四个真实职责（ADR-005）：

1. 锁定接口契约
2. 测试真实沙箱测不了的异常分支
3. 验证数据模型
4. 支撑端到端演示

### 5.2 Fixture 组织方式

```text
adapters/mock/fixtures/
├── amazon/
│   ├── category_tree.json              # 官方文档示例结构
│   ├── category_schema_beauty.json     # Product Type Definition 真实响应
│   ├── feed_submit_response.json
│   ├── feed_processing_report_partial.json   # 关键：含失败行
│   ├── orders_page_1.json
│   ├── orders_page_2.json
│   ├── settlement_report.csv
│   ├── error_429.json
│   ├── error_401.json
│   └── error_500.json
├── tiktok/
│   └── ...
└── scenarios/
    ├── happy_path.yaml                 # 场景编排：正常上架全流程
    ├── partial_failure.yaml            # 批量部分失败
    ├── rate_limit_storm.yaml           # 连续限流
    ├── token_expiry.yaml               # Token 中途过期
    ├── schema_drift.yaml               # 返回结构突变
    └── slow_response.yaml              # 超时
```

### 5.3 Fixture 来源规范（关键约束）

**硬性要求**：

| 来源 | 允许 | 说明 |
|---|---|---|
| 官方 API 文档示例 | ✅ | 第一版使用，标注来源 URL |
| 官方 SDK / Postman 集合 | ✅ | 标注版本 |
| 真实响应（脱敏） | ✅ | 拿到授权后**必须做一轮替换** |
| 自己编造 | ❌ | 会掩盖真实结构差异 |

每个 fixture 文件头部必须有元数据：

```json
{
  "_meta": {
    "source": "https://developer-docs.amazon.com/sp-api/docs/...",
    "source_type": "official_doc",
    "captured_at": "2026-09-28",
    "platform_version": "SP-API 2024-xx",
    "verified_with_real_response": false,
    "notes": "结构来自官方示例，字段类型已核对；数量级为示意"
  },
  "payload": { }
}
```

**`verified_with_real_response: false` 是必须诚实标记的字段**。评审时能一眼看出哪些是"纸上结构"。拿到授权后，逐字段比对并将该字段改为 `true`。

### 5.4 场景驱动的 Mock

静态 fixture 不足以测编排逻辑。Mock 需要支持**场景脚本**：

```yaml
# adapters/mock/fixtures/scenarios/rate_limit_storm.yaml
name: "连续限流后恢复"
description: "验证退避策略与最终成功"
platform: amazon
steps:
  - method: fetch_orders
    times: 3
    response: error_429
    retry_after: 2
  - method: fetch_orders
    times: 1
    response: orders_page_1
assert:
  total_calls: 4
  final_state: success
  backoff_observed: [2, 4, 8]
```

```yaml
# adapters/mock/fixtures/scenarios/partial_failure.yaml
name: "批量上架部分失败"
description: "100 条提交，97 成功 3 失败，验证逐行处理"
platform: amazon
steps:
  - method: create_listing
    response: feed_submit_response      # 返回 feedId
  - method: poll_submission
    response: feed_processing_report_partial
assert:
  partial: true
  succeeded_count: 97
  failed_count: 3
  failed_rows_have_error_code: true
  generated_todos: 3                     # 3 条人工待办
```

**为什么用 YAML 而不是纯 Python**：业务/产品人员也能读懂场景，评审时能直接看"这个系统遇到限流会怎么办"。

### 5.5 Mock 适配器的故障注入能力

```python
# adapters/mock/adapter.py
class MockAdapter(PlatformAdapter):
    """Mock 适配器。

    支持的故障注入（通过配置或场景触发）：
    - 延迟：模拟慢响应
    - 错误率：随机失败
    - 限流：触发 429 并带 Retry-After
    - 部分成功：批量接口返回混合结果
    - 结构漂移：返回缺字段/多字段的响应
    - 分页边界：返回空页、超大页、重复游标
    """

    def __init__(self, shop, client, config: MockConfig) -> None:
        ...
```

**必测的异常分支清单（写进 TDD-06 测试矩阵）**：

| 分支 | 为什么必须测 |
|---|---|
| 429 + Retry-After | 退避策略正确性 |
| 401 Token 过期 | 刷新链路 |
| 500 连续失败 | 熔断与 DLQ |
| 部分成功 | 逐行处理正确性 |
| 空结果 | 空指针防护 |
| 分页丢失/重复 | 同步完整性 |
| 结构漂移 | 告警触发 |
| 超时 | 幂等键防重复提交 |

---

## 6. 适配器注册与解析

```python
# adapters/registry.py
_ADAPTERS: dict[str, type[PlatformAdapter]] = {}


def register(platform: str):
    def wrapper(cls: type[PlatformAdapter]) -> type[PlatformAdapter]:
        cls.platform = platform
        _ADAPTERS[platform] = cls
        return cls
    return wrapper


def get_adapter_class(platform: str) -> type[PlatformAdapter]:
    """按平台名取适配器类。未知平台抛 ValueError。"""


async def build_adapter(shop: ShopContext, env: str) -> PlatformAdapter:
    """构建适配器实例。

    规则：
    - env in ("local", "test") → MockAdapter（强制，防误出网）
    - env == "sandbox" → 真实适配器 + 沙箱 Base URL
    - shop.credentials 缺失/过期 → 抛 AuthenticationError
    """
```

**`local`/`test` 强制用 Mock 的理由**：与 TDD-01 的 4.1 节呼应——CI 必须禁止出网。这不是"建议"，是硬约束，通过 `build_adapter` 统一强制，业务层无法绕过。

---

## 7. 契约测试策略

### 7.1 为什么必须做契约测试

Mock 与真实的差异是真实存在的风险。缓解方式是**同一套契约测试跑在两个实现上**：

```python
# tests/contract/test_adapter_contract.py
"""适配器契约测试：所有适配器必须通过同一套测试。

运行方式：
- 对 MockAdapter：CI 中每次跑
- 对 AmazonAdapter：注入真实沙箱凭据后跑（sandbox 环境）
"""


class TestPlatformAdapterContract:
    """契约要求，所有适配器实现必须满足。"""

    async def test_capabilities_cover_all_enum(self, adapter):
        """能力声明必须完整覆盖 Capability 枚举，不允许遗漏"""

    async def test_all_methods_return_adapter_result(self, adapter):
        """所有方法返回 AdapterResult"""

    async def test_all_errors_are_adapter_error(self, adapter):
        """注入各类错误，验证全部转换为 AdapterError"""

    async def test_raw_payload_always_present(self, adapter):
        """raw 字段必须非空（原则 A3）"""

    async def test_write_methods_require_idempotency_key(self, adapter):
        """写方法签名必须有 idempotency_key 参数"""

    async def test_unsupported_capability_raises(self, adapter):
        """声明 UNSUPPORTED 的能力，调用时必须抛 UnsupportedCapabilityError"""

    async def test_pagination_reaches_all_pages(self, adapter):
        """分页遍历不丢数据、不死循环"""

    async def test_timeout_is_retryable(self, adapter):
        """超时被正确分类为可重试"""
```

**关键**：`test_all_errors_are_adapter_error` 是防止"裸异常逃逸"的守门测试。任何新的适配器如果不遵守，CI 直接失败。

### 7.2 Mock 与真实的比对测试

拿到授权后，执行一轮**影子比对**：

```python
# tests/contract/test_shadow_compare.py
"""影子比对：同一请求分别打 Mock 与真实，逐字段比对结构。

目的：发现 Mock fixture 与真实响应的结构差异。
产出：差异报告（字段缺失、类型不符、枚举值新增）。
"""

async def test_shadow_compare_fetch_listings(mock_adapter, real_adapter):
    mock_result = await mock_adapter.fetch_listings(...)
    real_result = await real_adapter.fetch_listings(...)

    diff = compare_structure(mock_result.data, real_result.data)
    assert diff.is_compatible, f"结构不一致：{diff.details}"
```

**这是 Mock 策略的闭环**。没有这一步，Mock 会长期偏离真实，最终变成"测试全绿但生产全挂"。

---

## 8. 新增平台的操作清单

**这是本册的实用价值所在**——新增一个平台应该做什么，有明确清单：

| 步骤 | 动作 | 产出 |
|---|---|---|
| 1 | 实测能力矩阵，填 `platform_capabilities` 的 ⚠️ 项 | 确认的矩阵 |
| 2 | 收集官方响应样本到 `fixtures/{platform}/` | 带 `_meta` 的 fixture |
| 3 | 实现 `adapters/{platform}/adapter.py` | 通过契约测试 |
| 4 | 实现 `error_map.py` | 覆盖已知所有错误码 |
| 5 | 实现 `transform.py`（原始 → 规范化） | 字段映射文档 |
| 6 | 跑契约测试 | 全绿 |
| 7 | 跑影子比对（若已有授权） | 差异报告 |
| 8 | 在 `platform_capabilities` 落库 | 前端可展示 |
| 9 | 更新本册第 2.3 节矩阵表 | 文档同步 |

**第 9 步容易被忽略**。文档与代码不同步是这类项目的通病，必须写进清单。

---

## 9. 与 PRD v1.1 的差异说明

| 项 | PRD v1.1 | 本设计 | 理由 |
|---|---|---|---|
| 适配器接口 | 泛化签名 | 精确到参数类型与联合返回类型 | 泛化签名无法编码 |
| 能力状态 | 6 种（native/async/...） | 保持 6 种，**补充业务层应对策略** | PRD 只定义状态，未定义用法 |
| Amazon 退款 | 列为自动化能力 | **明确 UNSUPPORTED** | 官方无卖家侧执行退款 API |
| Amazon 消息读取 | 已在 v1.1 修正 | 固化 UNSUPPORTED + 邮件兜底 | 官方无历史消息 API |
| 错误处理 | 提"统一错误分类" | 26 个分类 + 策略表 + 异常层次 | 落地细节 |
| Mock fixture 来源 | 未提及 | 规定四类允许来源 + 强制元数据 | 防止编造数据 |
| 契约测试 | 未提及 | 列为必做 + 影子比对 | Mock 风险的缓解措施 |
| `platform_capabilities` 表 | 未列 | 新增（TDD-02 遗漏） | 前端需能力信息 |
| `platform_extras` | 未提及 | 受控逃生舱，Phase 1 禁用 | 应对平台怪异字段 |

---

## 10. 评审检查清单

### 必须拍板（阻塞开发）

- [ ] **Q1** 第 3.2 节的 30 个接口方法，粒度是否合适？有无缺失或冗余？（**Q1 不解决无法开始写适配器**）
- [ ] **Q2** 第 2.3 节能力矩阵中标注 ⚠️ 的项，谁能负责实测？何时完成？
- [ ] **Q3** **Amazon 不能执行退款**（`REFUND_CREATE` = UNSUPPORTED），PRD 12.5 的"退换货自动化"在 Amazon 上降级为"人工待办 + 自动分类"，是否接受？
- [ ] **Q4** `platform_extras` 逃生舱是否同意保留？（代价：有被滥用的风险；收益：应对平台怪异字段）
- [ ] **Q5** Mock fixture 用"官方文档示例"起步，是否接受？（代价：结构可能与真实有差异，需后续替换）

### 建议确认

- [ ] 26 个错误分类是否覆盖你们的实际排障需求？有无遗漏场景？
- [ ] 错误策略表中"是否告警"的级别划分是否合理？
- [ ] 是否同意"适配器方法不允许抛 AdapterError 以外的异常"这条硬约束？
- [ ] 契约测试 + 影子比对的工作量是否纳入排期？

### 需外部输入

- [ ] 首批平台中，哪些已有授权？哪些在申请？（决定 Mock 替换的节奏）
- [ ] 是否有平台方技术对接人可咨询 ⚠️ 项？
- [ ] 是否需要 Phase 1 就支持某个平台的特殊字段（决定 `platform_extras` 是否解禁）？

---

## 附录 A：接口方法总览（30 个）

| 分组 | 方法 | 能力依赖 |
|---|---|---|
| 元信息 | `capabilities()` | — |
| 元信息 | `health_check()` | — |
| 类目 | `fetch_category_tree()` | `CATEGORY_TREE_READ` |
| 类目 | `fetch_category_schema()` | `CATEGORY_SCHEMA_READ` |
| 类目 | `check_listing_eligibility()` | `LISTING_ELIGIBILITY_CHECK` |
| Listing 读 | `fetch_listings()` | `LISTING_READ` |
| Listing 读 | `search_listings()` | `LISTING_SEARCH` |
| Listing 写 | `create_listing()` | `LISTING_CREATE` |
| Listing 写 | `update_listing()` | `LISTING_UPDATE` |
| Listing 写 | `delete_listing()` | `LISTING_DELETE` |
| Listing 写 | `update_price()` | `LISTING_PRICE_UPDATE` |
| Listing 写 | `poll_submission()` | `LISTING_CREATE` / `ASYNC` |
| 库存 | `fetch_inventory()` | `INVENTORY_READ` |
| 库存 | `update_inventory()` | `INVENTORY_UPDATE` |
| 订单 | `fetch_orders()` | `ORDER_READ` |
| 订单 | `fetch_order_detail()` | `ORDER_READ` |
| 订单 | `confirm_shipment()` | `ORDER_SHIP_CONFIRM` |
| 财务 | `fetch_settlements()` | `SETTLEMENT_READ` |
| 财务 | `fetch_fees()` | `FEE_READ` |
| 财务 | `request_report()` | `REPORT`（隐含） |
| 财务 | `fetch_report()` | `REPORT`（隐含） |
| 售后 | `fetch_refunds()` | `REFUND_READ` |
| 售后 | `fetch_returns()` | `RETURN_READ` |
| 售后 | `execute_refund()` | `REFUND_CREATE` |
| 消息 | `send_message()` | `MESSAGE_SEND` |
| 消息 | `fetch_messages()` | `MESSAGE_READ` |
| 广告 | `fetch_ad_campaigns()` | `AD_CAMPAIGN_READ`（Phase 2） |
| 广告 | `fetch_ad_reports()` | `AD_REPORT_READ`（Phase 2） |
| 通知 | `subscribe_notifications()` | `NOTIFICATION_SUBSCRIBE` |

**Phase 1 实现范围**：除广告两个方法（抛 `NotImplementedError`）外，其余 28 个方法在 Mock 中完整实现；Amazon 适配器实现只读部分 + `poll_submission`。

---

## 附录 B：本册对 TDD-02 的补充要求

本册在编写过程中发现 TDD-02 遗漏一张表，**需回写 TDD-02**：

| 表名 | 用途 | 优先级 |
|---|---|---|
| `platform_capabilities` | 能力矩阵落库，供前端展示与调度决策 | 中 |

**建议**：TDD-02 评审时一并确认此表，保持两册一致。

---

## 附录 C：本册待补充项

| 项 | 说明 | 何时补充 |
|---|---|---|
| TikTok / Temu / 速卖通的错误码映射 | 需官方文档或实测 | 拿到授权后 |
| 各平台限流配额具体数值 | 官方文档分散，需整理 | 实测阶段 |
| 广告 API 详细契约 | Phase 2 内容 | Phase 2 立项时 |
| 各平台沙箱可用范围 | 需实测 | 技术预研阶段 |
| Webhook 签名验证实现 | 各平台机制不同 | 实现时补充到本册 |
