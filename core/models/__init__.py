"""ORM 模型集合。

导入本模块即注册全部表到 `core.db.Base.metadata`，
Alembic 的 autogenerate 依赖这一点。

表分组（共 40+ 张）：

| 分组 | 文件 | 表 |
|---|---|---|
| 租户与权限 | `tenant.py` | tenants / users / roles / user_roles / permissions / role_permissions |
| 店铺与凭据 | `shop.py` | shops / shop_credentials / credential_audit_logs |
| 商品与刊登 | `product.py` | products / product_variants / product_attributes / product_listings / platform_categories / category_schemas / sku_mappings / price_history / pricing_rules |
| 订单与售后 | `order.py` | orders / order_items / order_fees / refunds / return_records |
| 财务 | `finance.py` | exchange_rates / accounting_periods / cost_items / cost_rules / settlement_records / settlement_fees / profit_snapshots |
| 审批与审计 | `approval.py` | approvals / approval_actions / audit_events / state_transitions / domain_events |
| 基础设施 | `infra.py` | config_items / approval_rules / scoring_rubrics / feature_flags / platform_capabilities / raw_payloads / task_runs / sync_cursors / sync_watermarks / api_call_logs / alert_rules / alerts |

**与 TDD-02 的差异**：本实现纳入了 TDD-03/04/05 陆续发现需要补充的
8 张表（platform_capabilities / state_transitions / domain_events /
config_items / approval_rules / scoring_rubrics / feature_flags /
kill_switch 相关）与 3 个字段，详见各文件的说明。
"""

from __future__ import annotations

from core.db import Base
from core.models.approval import (
    Approval,
    ApprovalActionLog,
    AuditEvent,
    DomainEvent,
    StateTransition,
)
from core.models.finance import (
    AccountingPeriod,
    CostItem,
    CostRule,
    ExchangeRate,
    ProfitSnapshot,
    SettlementFee,
    SettlementRecord,
)
from core.models.infra import (
    Alert,
    AlertRule,
    ApiCallLog,
    ApprovalRule,
    ConfigItem,
    FeatureFlag,
    PlatformCapability,
    RawPayload,
    ScoringRubric,
    SyncCursor,
    SyncWatermark,
    TaskRun,
)
from core.models.mixins import NoteMixin, SoftDeleteMixin, TenantMixin, TimestampMixin
from core.models.order import Order, OrderFee, OrderItem, Refund, ReturnRecord
from core.models.product import (
    CategorySchema,
    PlatformCategory,
    PriceHistory,
    PricingRule,
    Product,
    ProductAttribute,
    ProductListing,
    ProductVariant,
    SKUMapping,
)
from core.models.shop import CredentialAuditLog, Shop, ShopCredential
from core.models.tenant import (
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)

__all__ = [
    "Base",
    # Mixins
    "TimestampMixin",
    "TenantMixin",
    "SoftDeleteMixin",
    "NoteMixin",
    # 租户与权限
    "Tenant",
    "User",
    "Role",
    "UserRole",
    "Permission",
    "RolePermission",
    # 店铺与凭据
    "Shop",
    "ShopCredential",
    "CredentialAuditLog",
    # 商品与刊登
    "Product",
    "ProductVariant",
    "ProductAttribute",
    "ProductListing",
    "PlatformCategory",
    "CategorySchema",
    "SKUMapping",
    "PriceHistory",
    "PricingRule",
    # 订单与售后
    "Order",
    "OrderItem",
    "OrderFee",
    "Refund",
    "ReturnRecord",
    # 财务
    "ExchangeRate",
    "AccountingPeriod",
    "CostItem",
    "CostRule",
    "SettlementRecord",
    "SettlementFee",
    "ProfitSnapshot",
    # 审批与审计
    "Approval",
    "ApprovalActionLog",
    "AuditEvent",
    "StateTransition",
    "DomainEvent",
    # 基础设施
    "ConfigItem",
    "ApprovalRule",
    "ScoringRubric",
    "FeatureFlag",
    "PlatformCapability",
    "RawPayload",
    "TaskRun",
    "SyncCursor",
    "SyncWatermark",
    "ApiCallLog",
    "AlertRule",
    "Alert",
]


def table_names() -> list[str]:
    """全部已注册的表名（供运维脚本与测试使用）。"""
    return sorted(Base.metadata.tables)


def table_summary() -> dict[str, int]:
    """表名 → 列数（供健康检查输出）。"""
    return {name: len(t.columns) for name, t in sorted(Base.metadata.tables.items())}
