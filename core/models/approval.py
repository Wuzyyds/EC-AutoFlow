"""审批、审计、状态转换、领域事件。

四张 append-only 表（ADR-007 §3.9）：
    `approval_actions`    审批动作流水
    `audit_events`        业务审计
    `state_transitions`   状态转换事件
    `credential_audit_logs`（在 shop.py）凭据审计

**为什么 append-only 用触发器而不是 REVOKE**（ADR-007 §3.9）：
    MySQL 没有 PUBLIC 角色概念，REVOKE 必须针对具体用户；
    且即使 REVOKE 了应用账号，root 和 DBA 仍可修改。
    触发器对**所有用户包括 root 生效**，是更可靠的强制手段。
    触发器 DDL 由 ops/generate_constraints.py 生成。

`domain_events` 是事务性 outbox（TDD-04 §3.4）：
    为什么不用 `task.delay()` 直接发：
        它在事务提交前发出，若事务随后回滚，任务已经跑了，
        形成"幽灵事件"。
    Outbox 保证"状态变更"与"事件派发"的原子性，
    且不需要引入 Kafka（符合 TDD-01 的不引入清单）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.constants import (
    ActorType,
    ApprovalAction,
    ApprovalRiskLevel,
    ApprovalStatus,
    ApprovalType,
    EventStatus,
    RollbackStatus,
    SuggestionSource,
)
from core.db import Base
from core.models.mixins import TimestampMixin
from core.models.types import (
    BigIntCol,
    BigIntColNullable,
    BigIntPK,
    IntCol,
    JsonCol,
    JsonColNullable,
    MoneyColNullable,
    TimestampCol,
    TimestampColNullable,
)

__all__ = [
    "Approval",
    "ApprovalActionLog",
    "AuditEvent",
    "StateTransition",
    "DomainEvent",
]


class Approval(Base, TimestampMixin):
    """审批单。

    关键设计（TDD-04 §3.1）：
        `status` 的 APPROVED 与 EXECUTED **必须分离** ——
        "人已同意" ≠ "事已办成"。合并会丢失
        "审批通过但执行失败"的追踪。这是审批系统最常见的设计错误。

    职责分离：`chk_separation_of_duties` 约束保证
        发起人不得为审批人（数据库层强制，见 ops/generate_constraints.py）。
    """

    __tablename__ = "approvals"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    approval_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    approval_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="LISTING_PUBLISH / PRICE_UPDATE / REFUND_EXECUTE / ...",
    )

    risk_level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ApprovalRiskLevel.MEDIUM.value,
        server_default=ApprovalRiskLevel.MEDIUM.value,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ApprovalStatus.PENDING.value,
        server_default=ApprovalStatus.PENDING.value,
    )

    shop_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 变更前后值（PRD 3.4 要求）
    payload_before: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)
    payload_after: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)

    #: 建议来源（审计需区分"人提的"还是"AI 建议的"）
    suggestion_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SuggestionSource.HUMAN.value,
        server_default=SuggestionSource.HUMAN.value,
    )
    ai_confidence: Mapped[object | None] = mapped_column(MoneyColNullable, nullable=True)
    rule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 幂等键（同一请求重复提交返回首次结果）
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    amount: Mapped[object | None] = mapped_column(MoneyColNullable, nullable=True)
    quantity: Mapped[int | None] = mapped_column(IntCol, nullable=True)

    #: 流程
    requested_by: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    request_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    approved_by: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 执行
    executed_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    execution_result: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)
    rollback_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=RollbackStatus.NONE.value
    )

    #: 执行重试次数（配合状态机的 retry_execute 上限）
    retry_count: Mapped[int] = mapped_column(IntCol, nullable=False, default=0, server_default="0")

    #: 审批超时时间（定时任务据此置为 EXPIRED）
    expires_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uk_approvals_idempotency"),
        Index("idx_approvals_pending", "tenant_id", "status", "risk_level", "created_at"),
        Index("idx_approvals_resource", "tenant_id", "resource_type", "resource_id"),
        Index("idx_approvals_expiry", "expires_at"),
        Index("idx_approvals_requester", "requested_by"),
        {"comment": "审批单"},
    )


class ApprovalActionLog(Base):
    """审批动作流水（**append-only**）。

    表名用 `approval_actions`，类名加 Log 后缀避免与
    core.constants.ApprovalAction 枚举重名。
    """

    __tablename__ = "approval_actions"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    approval_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    action: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="SUBMIT / APPROVE / REJECT / CANCEL / EXPIRE / EXECUTE / ...",
    )

    actor_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    actor_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ActorType.USER.value,
        server_default=ActorType.USER.value,
    )

    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_approval_actions_approval", "approval_id", "created_at"),
        {"comment": "审批动作流水（append-only）"},
    )


class AuditEvent(Base):
    """业务审计（**append-only**）。

    与 `api_call_logs` 职责分离：
        这里记**业务事实**（谁改了价、谁审批了什么），
        那里记**技术元数据**（调了哪个接口、耗时多少）。
        混在一张表里会导致字段冗余且查询低效。

    `reason` 对配置类变更是必填 ——
    不写原因，三个月后没人知道为什么阈值是 50 而不是 30。
    """

    __tablename__ = "audit_events"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="如 config.changed / price.updated"
    )

    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 变更前后（配置变更时必填）
    before_value: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)
    after_value: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    actor_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    actor_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ActorType.USER.value, server_default=ActorType.USER.value
    )

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    shop_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_audit_events_type", "tenant_id", "event_type", "created_at"),
        Index("idx_audit_events_resource", "tenant_id", "resource_type", "resource_id"),
        Index("idx_audit_events_actor", "actor_id", "created_at"),
        Index("idx_audit_events_trace", "trace_id"),
        {"comment": "业务审计（append-only）"},
    )


class StateTransition(Base):
    """状态转换事件（**append-only**）。

    为什么单独建表而不复用 `audit_events`：
        `audit_events` 记业务事实，本表记状态流转。
        两者粒度不同，混在一起查询会很难受。

    实用价值：可以回答"上架平均在 PENDING_APPROVAL 停留多久"
    这类运营问题，直接支撑 PRD 3.4 的审批效率度量。
    """

    __tablename__ = "state_transitions"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    #: 状态机名：listing / approval / sync / order / refund
    machine: Mapped[str] = mapped_column(String(64), nullable=False)

    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    from_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)

    actor_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ActorType.SYSTEM.value, server_default=ActorType.SYSTEM.value
    )
    actor_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: guard 求值上下文摘要
    context: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_transitions_entity", "machine", "entity_id", "created_at"),
        Index("idx_transitions_trace", "trace_id"),
        Index("idx_transitions_state", "tenant_id", "machine", "to_state", "created_at"),
        {"comment": "状态转换事件（append-only）"},
    )


class DomainEvent(Base):
    """领域事件（事务性 Outbox）。

    保证"状态变更"与"事件派发"的原子性（TDD-04 §3.4）。

    消费流程：
        1. 业务事务内：改状态 + 插入本表记录（同一事务）
        2. 事务提交后：worker 扫描 status=PENDING 的记录
        3. 处理成功 → DONE；失败 → FAILED + attempts++
        4. attempts 超限 → 人工介入
    """

    __tablename__ = "domain_events"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="如 approval.approved / listing.submitted"
    )

    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    payload: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=EventStatus.PENDING.value,
        server_default=EventStatus.PENDING.value,
    )

    attempts: Mapped[int] = mapped_column(IntCol, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 延迟重试（失败后的退避）
    available_at: Mapped[datetime] = mapped_column(
        TimestampCol, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    processed_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_domain_events_pending", "status", "available_at"),
        Index("idx_domain_events_aggregate", "aggregate_type", "aggregate_id"),
        Index("idx_domain_events_type", "tenant_id", "event_type", "created_at"),
        {"comment": "领域事件（Outbox）"},
    )
