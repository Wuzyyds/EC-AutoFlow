"""状态机集合。

五个核心状态机（TDD-04）：

| 状态机 | 状态数 | 驱动方 | 说明 |
|---|---|---|---|
| Listing | 12 | 本系统 | 上架流程，最复杂 |
| Approval | 8 | 本系统 | 审批流程，含职责分离 |
| Sync | 4 | 本系统 | 同步游标，无终态 |
| Order | 7 | **平台** | 只读镜像，不主动流转 |
| Refund | 6 | 混合 | 依平台能力决定能否自动执行 |

实现顺序建议（TDD-04 §9）：
    Approval → Listing → Sync → Refund → Order
理由：Approval 没有外部依赖、没有异步轮询、没有能力分支，
是最干净的框架试验场。先做 Listing 容易把框架问题与业务问题混在一起排查。
"""

from __future__ import annotations

from domain.state_machines.approval import (
    APPROVAL_TRANSITIONS,
    ApprovalContext,
    ApprovalStateMachine,
)
from domain.state_machines.base import (
    GuardFailedError,
    InvalidTransitionError,
    MachineReport,
    StateMachine,
    Transition,
    TransitionResult,
)
from domain.state_machines.listing import (
    LISTING_TRANSITIONS,
    ListingContext,
    ListingStateMachine,
)
from domain.state_machines.order import (
    ORDER_ANOMALY_ALLOWED,
    ORDER_TRANSITIONS,
    OrderContext,
    OrderStateMachine,
    TransitionVerdict,
)
from domain.state_machines.refund import (
    DEFAULT_AUTO_REFUND_LIMIT,
    DEFAULT_FRAUD_THRESHOLD,
    REFUND_TRANSITIONS,
    RefundContext,
    RefundStateMachine,
)
from domain.state_machines.sync import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    SYNC_TRANSITIONS,
    SyncContext,
    SyncStateMachine,
)

__all__ = [
    # 基类与通用类型
    "StateMachine",
    "Transition",
    "TransitionResult",
    "InvalidTransitionError",
    "GuardFailedError",
    "MachineReport",
    # 上架
    "ListingStateMachine",
    "ListingContext",
    "LISTING_TRANSITIONS",
    # 审批
    "ApprovalStateMachine",
    "ApprovalContext",
    "APPROVAL_TRANSITIONS",
    # 同步
    "SyncStateMachine",
    "SyncContext",
    "SYNC_TRANSITIONS",
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    # 订单
    "OrderStateMachine",
    "OrderContext",
    "ORDER_TRANSITIONS",
    "ORDER_ANOMALY_ALLOWED",
    "TransitionVerdict",
    # 退款
    "RefundStateMachine",
    "RefundContext",
    "REFUND_TRANSITIONS",
    "DEFAULT_AUTO_REFUND_LIMIT",
    "DEFAULT_FRAUD_THRESHOLD",
    # 注册表
    "ALL_MACHINES",
    "validate_all_machines",
]


#: 全部状态机。批量校验与文档生成用。
ALL_MACHINES: tuple[type[StateMachine], ...] = (
    ListingStateMachine,
    ApprovalStateMachine,
    SyncStateMachine,
    OrderStateMachine,
    RefundStateMachine,
)


def validate_all_machines() -> list[MachineReport]:
    """校验全部状态机定义的一致性。

    应在单元测试中调用 —— 状态机定义写错是"运行时才暴露、
    且症状诡异"的典型场景，必须靠静态检查拦截。
    """
    return [
        MachineReport(machine=m.machine_name, problems=m.validate_definition())
        for m in ALL_MACHINES
    ]
