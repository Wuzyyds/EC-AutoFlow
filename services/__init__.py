"""业务编排层。

**唯一允许跨层编排的地方**（TDD-01 §3.2）：

    apps/ → services/ → domain/ · repositories/ · adapters/

服务层的三条纪律：

1. **事务边界在这里**
   仓储只 `flush()`，提交由 `async_session_scope()` / `sync_session_scope()` 统一处理。
   仓储自己提交会让"多步操作"无法整体回滚。

2. **外部副作用不进业务事务**
   通知、平台 HTTP 调用要么放在事务之后，要么走 Outbox 事件。
   把一次 HTTP 调用放进事务，一个超时就能拖垮整个事务并触发回滚。

3. **状态机只算不记**
   `resolve_or_raise()` 只负责判定转换是否合法并给出目标状态；
   落库与留痕由服务层完成 —— 只有服务层知道事务边界与业务语义。

模块一览：

| 服务 | 职责 |
|---|---|
| `ApprovalService` | 审批全流程（提交/审批/驳回/超时/执行/重试） |
| `SyncService` | 订单等资源的增量同步与游标管理 |
| `ListingService` | 上架流程（Phase 1 终止在待审批） |
| `FinanceService` | 关账校验与贡献利润计算 |
| `ReportService` | 日报与库存预警 |
| `NotificationService` | 预警分级推送（外部副作用，不持会话） |
"""

from services.approval_service import ApprovalService, generate_approval_no
from services.base import (
    ServiceContext,
    build_event,
    publish_event,
    publish_event_sync,
    record_transition,
    record_transition_sync,
)
from services.finance_service import FinanceService, ProfitBreakdown
from services.listing_service import ListingService
from services.notification_service import NotificationResult, NotificationService
from services.report_service import DailySummary, LowStockItem, ReportService
from services.sync_service import OrderSyncOutcome, SyncService

__all__ = [
    # 基础设施
    "ServiceContext",
    "build_event",
    "publish_event",
    "publish_event_sync",
    "record_transition",
    "record_transition_sync",
    # 审批
    "ApprovalService",
    "generate_approval_no",
    # 同步
    "SyncService",
    "OrderSyncOutcome",
    # 上架
    "ListingService",
    # 财务
    "FinanceService",
    "ProfitBreakdown",
    # 报表
    "ReportService",
    "DailySummary",
    "LowStockItem",
    # 通知
    "NotificationService",
    "NotificationResult",
]
