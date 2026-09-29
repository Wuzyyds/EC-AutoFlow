"""服务层共享基础设施。

服务层是**唯一**允许跨层编排的地方（TDD-01 §3.2）：

    apps/ → services/ → domain/ · repositories/ · adapters/

本模块提供三样所有服务都要用的东西：

1. **`ServiceContext`** —— 把租户、操作者、trace 打成一个包传递。
   否则每个方法签名都要拖一串参数，迟早有人漏传 `tenant_id` ——
   而漏传在仓储层会直接抛异常（见 `repositories/base.py`），
   与其让调用方在运行时踩坑，不如从签名上就逼它传。

2. **`build_event()` / `publish_event()`** —— Outbox 写入。
   **禁止用 `task.delay()` 直接发消息**：事务回滚时消息已经投递出去，
   但数据没提交，worker 处理时找不到对象 —— 这是"幽灵事件"，
   报错信息完全无法归因。Outbox 是唯一能保证事件与状态同生共死的做法。

3. **`record_transition()`** —— 状态流转留痕。
   状态机算出的结果必须落库，否则出问题时无法还原
   "谁、在什么时候、基于什么把状态从 A 改成了 B"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from core.constants import ActorType
from core.models import DomainEvent, StateTransition

__all__ = [
    "ServiceContext",
    "build_event",
    "publish_event",
    "publish_event_sync",
    "record_transition",
    "record_transition_sync",
]


@dataclass(frozen=True, slots=True)
class ServiceContext:
    """服务调用上下文。

    `tenant_id` 必填且无默认值 —— 服务层的任何操作都必须明确租户，
    与仓储层的强制校验形成双保险。
    """

    #: 租户 ID。必填。
    tenant_id: int

    #: 操作者 ID。系统/定时任务为 None。
    actor_id: int | None = None

    #: 操作者类型：USER / SYSTEM / PLATFORM。
    #: 审计必须能区分"人干的"和"系统干的"。
    actor_type: str = ActorType.USER.value

    #: 调用链追踪 ID（贯穿 API → service → worker）。
    trace_id: str | None = None

    #: 客户端 IP（审计用，仅记 IP 不记其他）。
    client_ip: str | None = None

    #: 操作者角色（状态机 guard 需要，如审批人角色校验）。
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_system(self) -> bool:
        """是否为系统操作（定时任务、后台补偿）。"""
        return self.actor_type == ActorType.SYSTEM.value

    def to_audit_context(self) -> dict[str, Any]:
        """转为审计事件的 context 字段。

        只放标识类信息，**不放任何业务数据** ——
        业务数据可能含 PII，而审计表是全量留存的。
        """
        return {
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "trace_id": self.trace_id,
            "client_ip": self.client_ip,
        }


# ============================================================
# Outbox 事件
# ============================================================


def build_event(
    ctx: ServiceContext,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: int,
    payload: dict[str, Any] | None = None,
) -> DomainEvent:
    """构建领域事件对象（未入库）。

    单独拆出来是为了让同步与异步两条路径共用同一份构造逻辑，
    避免两边字段填得不一致。
    """
    return DomainEvent(
        tenant_id=ctx.tenant_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload or {},
        trace_id=ctx.trace_id,
    )


async def publish_event(
    session: AsyncSession,
    ctx: ServiceContext,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: int,
    payload: dict[str, Any] | None = None,
) -> DomainEvent:
    """写入领域事件（**必须与业务变更在同一事务内**）。

    Args:
        session: 当前业务事务的会话。传独立会话会破坏原子性。
        ctx: 服务上下文。
        event_type: 事件类型，如 `approval.approved`。
        aggregate_type: 聚合类型，如 `Approval`。
        aggregate_id: 聚合 ID。
        payload: 事件负载（**不得含凭据或未脱敏 PII**）。
    """
    event = build_event(
        ctx,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    return event


def publish_event_sync(
    session: Session,
    ctx: ServiceContext,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: int,
    payload: dict[str, Any] | None = None,
) -> DomainEvent:
    """写入领域事件（同步版本，供 Celery worker 使用）。"""
    event = build_event(
        ctx,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
    )
    session.add(event)
    session.flush()
    return event


# ============================================================
# 状态流转留痕
# ============================================================


def _build_transition(
    ctx: ServiceContext,
    *,
    machine: str,
    entity_type: str,
    entity_id: int,
    from_state: str | None,
    to_state: str,
    event: str,
    reason: str | None,
    context: dict[str, Any] | None,
) -> StateTransition:
    return StateTransition(
        tenant_id=ctx.tenant_id,
        machine=machine,
        entity_type=entity_type,
        entity_id=entity_id,
        from_state=from_state,
        to_state=to_state,
        event=event,
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        reason=reason,
        context=context,
        trace_id=ctx.trace_id,
    )


async def record_transition(
    session: AsyncSession,
    ctx: ServiceContext,
    *,
    machine: str,
    entity_type: str,
    entity_id: int,
    to_state: str,
    event: str,
    from_state: str | None = None,
    reason: str | None = None,
    context: dict[str, Any] | None = None,
) -> StateTransition:
    """记录一次状态流转。

    状态机只负责"算"，不负责"记" —— 落库由服务层完成，
    因为只有服务层知道事务边界与业务语义。
    """
    transition = _build_transition(
        ctx,
        machine=machine,
        entity_type=entity_type,
        entity_id=entity_id,
        from_state=from_state,
        to_state=to_state,
        event=event,
        reason=reason,
        context=context,
    )
    session.add(transition)
    await session.flush()
    return transition


def record_transition_sync(
    session: Session,
    ctx: ServiceContext,
    *,
    machine: str,
    entity_type: str,
    entity_id: int,
    to_state: str,
    event: str,
    from_state: str | None = None,
    reason: str | None = None,
    context: dict[str, Any] | None = None,
) -> StateTransition:
    """记录一次状态流转（同步版本，供 Celery worker 使用）。"""
    transition = _build_transition(
        ctx,
        machine=machine,
        entity_type=entity_type,
        entity_id=entity_id,
        from_state=from_state,
        to_state=to_state,
        event=event,
        reason=reason,
        context=context,
    )
    session.add(transition)
    session.flush()
    return transition
