"""审批、审计、状态流转与领域事件仓储。

**Outbox 模式**（TDD-04 §3.4）：

    业务事务内同时"改状态 + 写 `domain_events`"，两者原子提交；
    事务提交后 worker 扫描待处理事件。

    **禁止用 `task.delay()` 直接发消息** —— 事务回滚会留下"幽灵事件"：
    Celery 已把任务投递出去，但数据没提交，worker 处理时找不到对象，
    报错又无法归因。Outbox 是唯一能保证"事件与状态同生共死"的做法。

**APPROVED 与 EXECUTED 必须分离**（TDD-04 §3）：

    "人已同意" ≠ "事已办成"。
    合并成一个状态会丢失"审批通过但执行失败"的追踪 ——
    这类单据会永远沉默，直到有人发现商品没上架。

    `list_approved_not_executed` 专门捞这种卡在中间的单据。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Select

from core.constants import ApprovalStatus, EventStatus
from core.models import (
    Approval,
    ApprovalActionLog,
    AuditEvent,
    DomainEvent,
    StateTransition,
)
from core.timeutil import utc_now
from repositories.base import BaseRepository, SyncBaseRepository

__all__ = [
    "ApprovalActionLogRepository",
    "ApprovalRepository",
    "AuditEventRepository",
    "DomainEventRepository",
    "StateTransitionRepository",
    "SyncApprovalRepository",
    "SyncDomainEventRepository",
]

#: 领域事件最大重试次数。超过后停止重试并等待人工介入 ——
#: 无限重试只会刷爆日志，掩盖真正需要人看的错误。
MAX_EVENT_ATTEMPTS = 5


class ApprovalRepository(BaseRepository[Approval]):
    """审批单仓储。"""

    model = Approval

    async def get_by_no(self, approval_no: str) -> Approval | None:
        stmt = self._stmt().where(Approval.approval_no == approval_no)
        return (await self.session.execute(stmt)).scalars().first()

    async def get_by_idempotency_key(self, idempotency_key: str) -> Approval | None:
        """按幂等键查审批单（防重复提交）。

        同一幂等键提交不同参数属于严重错误（`IdempotencyConflictError`），
        由 service 层判定，这里只负责查。
        """
        stmt = self._stmt().where(Approval.idempotency_key == idempotency_key)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_pending(self, *, limit: int | None = None) -> list[Approval]:
        """待审批列表（审批人工作台）。"""
        stmt = self._stmt().where(Approval.status == ApprovalStatus.PENDING.value)
        stmt = self._apply_ordering(stmt, "created_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_approved_not_executed(self, *, limit: int | None = None) -> list[Approval]:
        """已通过但尚未执行的审批单。

        **这是最容易被忽略的一类单据**：
        审批人点了"同意"，但下游执行失败或根本没触发，
        单据从此沉默 —— 直到有人发现商品没上架、价格没改。
        """
        stmt = self._stmt().where(Approval.status == ApprovalStatus.APPROVED.value)
        stmt = self._apply_ordering(stmt, "approved_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_expired(self, *, now: datetime | None = None) -> list[Approval]:
        """已超过有效期但仍未处理的审批单。

        超时未处理必须显式转 EXPIRED，否则会一直挂在待办里，
        让审批工作台的数字永远降不下来。
        """
        moment = now or utc_now()
        stmt = (
            self._stmt()
            .where(Approval.expires_at.is_not(None))
            .where(Approval.expires_at <= moment)
            .where(Approval.status == ApprovalStatus.PENDING.value)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_retryable(self, *, limit: int | None = None) -> list[Approval]:
        """执行失败但可重试的审批单。

        `FAILED` 不是终态（`ApprovalStatus.terminal` 不含它）——
        允许重试执行，但必须有上限，否则会陷入无限重试。
        """
        stmt = self._stmt().where(Approval.status == ApprovalStatus.FAILED.value)
        stmt = self._apply_ordering(stmt, "updated_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class ApprovalActionLogRepository(BaseRepository[ApprovalActionLog]):
    """审批操作日志（append-only）。"""

    model = ApprovalActionLog

    async def log(
        self,
        *,
        approval_id: int,
        action: str,
        actor_type: str,
        actor_id: int | None = None,
        comment: str | None = None,
        metadata_json: dict | None = None,
    ) -> ApprovalActionLog:
        entry = ApprovalActionLog(
            approval_id=approval_id,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            comment=comment,
            metadata_json=metadata_json,
        )
        return await self.add(entry)

    async def list_by_approval(self, approval_id: int) -> list[ApprovalActionLog]:
        stmt = self._apply_ordering(
            self._stmt().where(ApprovalActionLog.approval_id == approval_id), "created_at"
        )
        return list((await self.session.execute(stmt)).scalars().all())


class AuditEventRepository(BaseRepository[AuditEvent]):
    """审计事件仓储（append-only）。

    只提供写入与查询 —— 审计记录可修改就失去了审计意义。
    """

    model = AuditEvent

    async def log(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: int | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        before_value: dict | None = None,
        after_value: dict | None = None,
        reason: str | None = None,
        context: dict | None = None,
        shop_id: int | None = None,
        client_ip: str | None = None,
        trace_id: str | None = None,
    ) -> AuditEvent:
        entry = AuditEvent(
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            before_value=before_value,
            after_value=after_value,
            reason=reason,
            context=context,
            shop_id=shop_id,
            client_ip=client_ip,
            trace_id=trace_id,
        )
        return await self.add(entry)

    async def list_by_resource(
        self, resource_type: str, resource_id: str, *, limit: int | None = None
    ) -> list[AuditEvent]:
        stmt = self._stmt().where(AuditEvent.resource_type == resource_type)
        stmt = stmt.where(AuditEvent.resource_id == resource_id)
        stmt = self._apply_ordering(stmt, "-created_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class StateTransitionRepository(BaseRepository[StateTransition]):
    """状态流转记录仓储（append-only）。

    每一次状态机的转换都写一条 —— 出问题时能精确还原"谁在什么时候
    基于什么把状态从 A 改成了 B"。
    """

    model = StateTransition

    async def log(
        self,
        *,
        machine: str,
        entity_type: str,
        entity_id: int,
        to_state: str,
        event: str,
        from_state: str | None = None,
        actor_type: str = "SYSTEM",
        actor_id: int | None = None,
        reason: str | None = None,
        context: dict | None = None,
        trace_id: str | None = None,
    ) -> StateTransition:
        entry = StateTransition(
            machine=machine,
            entity_type=entity_type,
            entity_id=entity_id,
            from_state=from_state,
            to_state=to_state,
            event=event,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            context=context,
            trace_id=trace_id,
        )
        return await self.add(entry)

    async def list_by_entity(
        self, entity_type: str, entity_id: int
    ) -> list[StateTransition]:
        stmt = self._stmt().where(StateTransition.entity_type == entity_type)
        stmt = stmt.where(StateTransition.entity_id == entity_id)
        stmt = self._apply_ordering(stmt, "created_at")
        return list((await self.session.execute(stmt)).scalars().all())


class DomainEventRepository(BaseRepository[DomainEvent]):
    """领域事件仓储（Outbox 生产与消费）。"""

    model = DomainEvent

    async def list_pending(
        self, *, limit: int | None = None, max_attempts: int = MAX_EVENT_ATTEMPTS
    ) -> list[DomainEvent]:
        """拉取可处理的事件。

        三个条件缺一不可：
            1. `PENDING` 或 `FAILED`（失败的要能重试）
            2. `available_at <= now`（退避未到期的不能取）
            3. `attempts < max_attempts`（超限的等人工介入）

        只查 `PENDING` 会让失败事件永远沉默；
        不查 `available_at` 会让退避形同虚设，失败事件被立刻重试打爆。
        """
        stmt = self._stmt().where(
            DomainEvent.status.in_([EventStatus.PENDING.value, EventStatus.FAILED.value])
        )
        stmt = stmt.where(DomainEvent.available_at <= utc_now())
        stmt = stmt.where(DomainEvent.attempts < max_attempts)
        stmt = self._apply_ordering(stmt, "available_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def mark_done(self, event_id: int) -> DomainEvent:
        """标记事件处理完成。"""
        event = await self.get_or_raise(event_id, resource="领域事件")
        event.status = EventStatus.DONE.value
        event.processed_at = utc_now()
        event.last_error = None
        await self.session.flush()
        return event

    async def mark_failed(
        self, event_id: int, error: str, *, retry_delay_seconds: int = 60
    ) -> DomainEvent:
        """标记事件处理失败，并设置退避时间。

        `error` 必须已脱敏，且做长度截断。
        """
        event = await self.get_or_raise(event_id, resource="领域事件")
        event.attempts += 1
        event.status = EventStatus.FAILED.value
        event.last_error = (error or "")[:1000]
        event.available_at = utc_now() + timedelta(seconds=retry_delay_seconds)
        await self.session.flush()
        return event


# ============================================================
# 同步版本（Celery worker 用）
# ============================================================


class SyncApprovalRepository(SyncBaseRepository[Approval]):
    model = Approval

    def get_by_no(self, approval_no: str) -> Approval | None:
        return (
            self.session.execute(self._stmt().where(Approval.approval_no == approval_no))
            .scalars()
            .first()
        )

    def list_approved_not_executed(self, *, limit: int | None = None) -> list[Approval]:
        stmt = self._stmt().where(Approval.status == ApprovalStatus.APPROVED.value)
        stmt = self._apply_ordering(stmt, "approved_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list(self.session.execute(stmt).scalars().all())

    def list_expired(self, *, now: datetime | None = None) -> list[Approval]:
        moment = now or utc_now()
        stmt = (
            self._stmt()
            .where(Approval.expires_at.is_not(None))
            .where(Approval.expires_at <= moment)
            .where(Approval.status == ApprovalStatus.PENDING.value)
        )
        return list(self.session.execute(stmt).scalars().all())


class SyncDomainEventRepository(SyncBaseRepository[DomainEvent]):
    model = DomainEvent

    def list_pending(
        self, *, limit: int | None = None, max_attempts: int = MAX_EVENT_ATTEMPTS
    ) -> list[DomainEvent]:
        stmt = self._stmt().where(
            DomainEvent.status.in_([EventStatus.PENDING.value, EventStatus.FAILED.value])
        )
        stmt = stmt.where(DomainEvent.available_at <= utc_now())
        stmt = stmt.where(DomainEvent.attempts < max_attempts)
        stmt = self._apply_ordering(stmt, "available_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list(self.session.execute(stmt).scalars().all())

    def mark_done(self, event_id: int) -> DomainEvent:
        event = self.get_or_raise(event_id, resource="领域事件")
        event.status = EventStatus.DONE.value
        event.processed_at = utc_now()
        event.last_error = None
        self.session.flush()
        return event

    def mark_failed(
        self, event_id: int, error: str, *, retry_delay_seconds: int = 60
    ) -> DomainEvent:
        event = self.get_or_raise(event_id, resource="领域事件")
        event.attempts += 1
        event.status = EventStatus.FAILED.value
        event.last_error = (error or "")[:1000]
        event.available_at = utc_now() + timedelta(seconds=retry_delay_seconds)
        self.session.flush()
        return event
