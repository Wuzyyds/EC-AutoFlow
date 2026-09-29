"""数据访问层。

分层约定（TDD-01 §3.2）：

    `repositories/` 依赖 `core/` 与数据模型，**不得**被 `domain/` 依赖。
    依赖方向：`apps/ → services/ → repositories/ → core/`

三条全局约束（详见 `base.py`）：

1. **租户过滤统一注入**（ADR-006）
   模型含 `tenant_id` 时，构造仓储**必须**传租户 ID，否则抛
   `ConfigurationError`。业务代码不得手写 `where tenant_id = ...`。

2. **async / sync 双套接口**（ADR-002）
   API 层用 `BaseRepository`（AsyncSession），
   Celery worker 用 `SyncBaseRepository`（Session）。

3. **原生 SQL 必须参数化**（ADR-003）
   复杂报表查询允许用原生 SQL，但必须经 `text()` 绑定参数，
   禁止字符串拼接。

用法：

    from repositories import OrderRepository

    async with async_session_scope() as session:
        repo = OrderRepository(session, tenant_id=1)
        orders = await repo.list_created_since(since, shop_id=5)
"""

from repositories.approval import (
    ApprovalActionLogRepository,
    ApprovalRepository,
    AuditEventRepository,
    DomainEventRepository,
    StateTransitionRepository,
    SyncApprovalRepository,
    SyncDomainEventRepository,
)
from repositories.base import MAX_LIMIT, BaseRepository, SyncBaseRepository
from repositories.finance import (
    AccountingPeriodRepository,
    CostItemRepository,
    CostRuleRepository,
    ExchangeRateRepository,
    ProfitSnapshotRepository,
    SettlementFeeRepository,
    SettlementRecordRepository,
    SyncAccountingPeriodRepository,
    SyncExchangeRateRepository,
    SyncSettlementRecordRepository,
)
from repositories.infra import (
    AlertRepository,
    AlertRuleRepository,
    ConfigItemRepository,
    RawPayloadRepository,
    SyncCursorRepository,
    SyncSyncCursorRepository,
    SyncSyncWatermarkRepository,
    SyncWatermarkRepository,
    TaskRunRepository,
)
from repositories.order import (
    OrderFeeRepository,
    OrderItemRepository,
    OrderRepository,
    RefundRepository,
    ReturnRecordRepository,
    SyncOrderFeeRepository,
    SyncOrderItemRepository,
    SyncOrderRepository,
    SyncRefundRepository,
    SyncReturnRecordRepository,
)
from repositories.product import (
    CategorySchemaRepository,
    PlatformCategoryRepository,
    PriceHistoryRepository,
    PricingRuleRepository,
    ProductListingRepository,
    ProductRepository,
    SKUMappingRepository,
    SyncCategorySchemaRepository,
    SyncProductListingRepository,
    SyncProductRepository,
    SyncSKUMappingRepository,
)
from repositories.shop import (
    CredentialAuditLogRepository,
    ShopCredentialRepository,
    ShopRepository,
    SyncCredentialAuditLogRepository,
    SyncShopCredentialRepository,
    SyncShopRepository,
)
from repositories.tenant import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
    SyncPermissionRepository,
    SyncRolePermissionRepository,
    SyncRoleRepository,
    SyncTenantRepository,
    SyncUserRepository,
    SyncUserRoleRepository,
    TenantRepository,
    UserRepository,
    UserRoleRepository,
)

__all__ = [
    "MAX_LIMIT",
    "BaseRepository",
    "SyncBaseRepository",
    # 租户与权限
    "TenantRepository",
    "UserRepository",
    "RoleRepository",
    "PermissionRepository",
    "UserRoleRepository",
    "RolePermissionRepository",
    "SyncTenantRepository",
    "SyncUserRepository",
    "SyncRoleRepository",
    "SyncPermissionRepository",
    "SyncUserRoleRepository",
    "SyncRolePermissionRepository",
    # 店铺与凭据
    "ShopRepository",
    "ShopCredentialRepository",
    "CredentialAuditLogRepository",
    "SyncShopRepository",
    "SyncShopCredentialRepository",
    "SyncCredentialAuditLogRepository",
    # 商品与刊登
    "ProductRepository",
    "ProductListingRepository",
    "SKUMappingRepository",
    "PlatformCategoryRepository",
    "CategorySchemaRepository",
    "PriceHistoryRepository",
    "PricingRuleRepository",
    "SyncProductRepository",
    "SyncProductListingRepository",
    "SyncSKUMappingRepository",
    "SyncCategorySchemaRepository",
    # 订单与售后
    "OrderRepository",
    "OrderItemRepository",
    "OrderFeeRepository",
    "RefundRepository",
    "ReturnRecordRepository",
    "SyncOrderRepository",
    "SyncOrderItemRepository",
    "SyncOrderFeeRepository",
    "SyncRefundRepository",
    "SyncReturnRecordRepository",
    # 财务
    "ExchangeRateRepository",
    "AccountingPeriodRepository",
    "CostItemRepository",
    "CostRuleRepository",
    "SettlementRecordRepository",
    "SettlementFeeRepository",
    "ProfitSnapshotRepository",
    "SyncExchangeRateRepository",
    "SyncAccountingPeriodRepository",
    "SyncSettlementRecordRepository",
    # 审批与审计
    "ApprovalRepository",
    "ApprovalActionLogRepository",
    "AuditEventRepository",
    "StateTransitionRepository",
    "DomainEventRepository",
    "SyncApprovalRepository",
    "SyncDomainEventRepository",
    # 基础设施
    "ConfigItemRepository",
    "AlertRuleRepository",
    "AlertRepository",
    "RawPayloadRepository",
    "TaskRunRepository",
    "SyncCursorRepository",
    "SyncWatermarkRepository",
    "SyncSyncCursorRepository",
    "SyncSyncWatermarkRepository",
]
