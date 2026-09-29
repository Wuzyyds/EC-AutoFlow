"""审批服务。

状态流转（TDD-04 §3）：

    DRAFT ──submit──▶ PENDING ──approve──▶ APPROVED ──execute_success──▶ EXECUTED
                        │                     │
                        ├─reject──▶ REJECTED  └─execute_fail──▶ FAILED
                        ├─expire──▶ EXPIRED                        │
                        └─cancel──▶ CANCELED   ◀──abandon─────────┤
                                                   retry_execute──┘

**三条核心设计判断**：

1. **APPROVED 与 EXECUTED 必须分离**
   "人已同意" ≠ "事已办成"。平台可能拒绝（商品已下架不能改价）、
   网络可能失败。合并会丢失"审批通过但实际没生效"的追踪。

2. **审批事务内不做业务编排**（TDD-04 §486）
   如果审批事务里直接改 `listing_status` 并起任务，
   事务一回滚就产生"审批通过但任务已起"的不一致。
   正确做法：审批事务内**只改审批单状态 + 写 Outbox 事件**，
   真正的执行由事件消费者在**独立事务**中完成。

3. **职责分离：发起人 ≠ 审批人**
   应用层 guard 校验 + 数据库触发器兜底（ADR-007 §3.1，
   低版本 MySQL 的 CHECK 不生效，用触发器实现）。
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import ApprovalStatus, SuggestionSource
from core.exceptions import ConflictError, NotFoundError
from core.models import Approval
from core.timeutil import utc_now
from domain.state_machines import ApprovalContext, ApprovalStateMachine
from repositories.approval import (
    ApprovalActionLogRepository,
    ApprovalRepository,
    AuditEventRepository,
)
from repositories.base import MAX_LIMIT
from services.base import ServiceContext, publish_event, record_transition

__all__ = ["ApprovalService", "generate_approval_no"]

#: 审批单号前缀。
APPROVAL_NO_PREFIX = "AP"


def generate_approval_no(*, moment: datetime | None = None) -> str:
    """生成审批单号。

    格式 `AP` + 年月日 + 6 位随机。

    **不用自增 ID 当单号** —— 单号会展示给用户，
    自增 ID 会把业务量（今天有多少单）直接暴露出去。
    """
    stamp = (moment or utc_now()).strftime("%Y%m%d")
    return f"{APPROVAL_NO_PREFIX}{stamp}{secrets.randbelow(1_000_000):06d}"


class ApprovalService:
    """审批编排服务。

    典型用法：

        async with async_session_scope() as session:
            svc = ApprovalService(session, ctx)
            approval = await svc.create(...)
            await svc.submit(approval.id)
    """

    def __init__(self, session: AsyncSession, ctx: ServiceContext) -> None:
        self.session = session
        self.ctx = ctx
        self.repo = ApprovalRepository(session, tenant_id=ctx.tenant_id)
        self.actions = ApprovalActionLogRepository(session, tenant_id=ctx.tenant_id)
        self.audit = AuditEventRepository(session, tenant_id=ctx.tenant_id)

    # ========================================================
    # 创建与提交
    # ========================================================

    async def create(
        self,
        *,
        approval_type: str,
        risk_level: str,
        payload_after: dict[str, Any],
        idempotency_key: str,
        requested_by: int,
        amount: Any = None,
        quantity: int | None = None,
        shop_id: int | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        payload_before: dict[str, Any] | None = None,
        request_reason: str | None = None,
        suggestion_source: str = SuggestionSource.HUMAN.value,
        expires_at: datetime | None = None,
    ) -> Approval:
        """创建审批单（初始为 DRAFT）。

        幂等：同一 `idempotency_key` 重复提交会返回**已存在的单**，
        而不是新建一条 —— 否则用户双击提交会变成两笔审批。
        """
        existing = await self.repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        approval = Approval(
            approval_no=generate_approval_no(),
            approval_type=approval_type,
            risk_level=risk_level,
            status=ApprovalStatus.DRAFT.value,
            shop_id=shop_id,
            resource_type=resource_type,
            resource_id=resource_id,
            payload_before=payload_before,
            payload_after=payload_after or {},
            suggestion_source=suggestion_source,
            idempotency_key=idempotency_key,
            amount=amount,
            quantity=quantity,
            requested_by=requested_by,
            request_reason=request_reason,
            expires_at=expires_at,
        )
        await self.repo.add(approval)

        await self.audit.log(
            # 审计事件类型暂无对应枚举（constants.py 未定义），
            # 命名沿用 `{聚合}.{动作}` 约定。
            event_type="approval.created",
            actor_type=self.ctx.actor_type,
            actor_id=self.ctx.actor_id,
            resource_type="Approval",
            resource_id=str(approval.id),
            after_value={"approval_no": approval.approval_no, "type": approval_type},
            shop_id=shop_id,
            trace_id=self.ctx.trace_id,
        )
        return approval

    async def submit(self, approval_id: int) -> Approval:
        """提交审批（DRAFT → PENDING）。"""
        approval = await self._get(approval_id)
        await self._apply(
            approval,
            event="submit",
            context=ApprovalContext(
                roles=self.ctx.roles,
                actor_id=self.ctx.actor_id,
                requested_by=approval.requested_by,
                has_payload=bool(approval.payload_after),
            ),
        )
        await self._log_action(approval_id, "submit")
        return approval

    # ========================================================
    # 审批决策
    # ========================================================

    async def approve(self, approval_id: int, *, comment: str | None = None) -> Approval:
        """审批通过（PENDING → APPROVED）。

        **只改状态 + 写 Outbox 事件，不在这里执行业务动作** ——
        执行失败要能独立回滚，不能把审批单一起带下去。
        """
        approval = await self._get(approval_id)
        await self._apply(
            approval,
            event="approve",
            context=self._context_for(approval),
        )
        approval.approved_by = self.ctx.actor_id
        approval.approved_at = utc_now()
        await self.session.flush()

        await self._log_action(approval_id, "approve", comment=comment)
        await publish_event(
            self.session,
            self.ctx,
            event_type="approval.approved",
            aggregate_type="Approval",
            aggregate_id=approval.id,
            payload={
                "approval_no": approval.approval_no,
                "approval_type": approval.approval_type,
                "resource_type": approval.resource_type,
                "resource_id": approval.resource_id,
                "shop_id": approval.shop_id,
            },
        )
        return approval

    async def reject(self, approval_id: int, *, reason: str) -> Approval:
        """审批驳回（PENDING → REJECTED）。"""
        approval = await self._get(approval_id)
        await self._apply(
            approval,
            event="reject",
            reason=reason,
            context=self._context_for(approval),
        )
        approval.reject_reason = reason
        await self.session.flush()
        await self._log_action(approval_id, "reject", comment=reason)
        return approval

    async def cancel(self, approval_id: int, *, reason: str) -> Approval:
        """发起人撤销（PENDING → CANCELED）。"""
        approval = await self._get(approval_id)
        await self._apply(
            approval,
            event="cancel",
            reason=reason,
            context=self._context_for(approval),
        )
        await self._log_action(approval_id, "cancel", comment=reason)
        return approval

    async def expire_overdue(self, *, limit: int = MAX_LIMIT) -> list[Approval]:
        """把超时未处理的审批单批量置为 EXPIRED。

        系统操作（`actor_type=SYSTEM`）。必须显式转终态，
        否则这些单会一直挂在待办里，让审批工作台的数字永远降不下来。
        """
        overdue = await self.repo.list_expired()
        expired: list[Approval] = []
        for approval in overdue[:limit]:
            await self._apply(
                approval,
                event="expire",
                reason="超过审批有效期，自动置为已超时",
                context=self._context_for(approval),
            )
            await self._log_action(approval.id, "expire", comment="系统自动超时")
            expired.append(approval)
        return expired

    # ========================================================
    # 执行结果回写
    # ========================================================

    async def mark_executed(
        self, approval_id: int, *, result: dict[str, Any] | None = None
    ) -> Approval:
        """标记执行成功（APPROVED → EXECUTED）。

        由 Outbox 消费者在**独立事务**中调用。
        """
        approval = await self._get(approval_id)
        await self._apply(approval, event="execute_success", context=self._context_for(approval))
        approval.executed_at = utc_now()
        approval.execution_result = result or {}
        await self.session.flush()
        await self._log_action(approval_id, "execute_success")
        return approval

    async def mark_failed(self, approval_id: int, *, error: str) -> Approval:
        """标记执行失败（APPROVED → FAILED）。

        `FAILED` **不是终态** —— 允许重试执行，但有次数上限。
        """
        approval = await self._get(approval_id)
        await self._apply(
            approval,
            event="execute_fail",
            reason=error,
            context=self._context_for(approval),
        )
        approval.execution_result = {"error": (error or "")[:1000]}
        await self.session.flush()
        await self._log_action(approval_id, "execute_fail", comment=error)
        return approval

    async def retry_execute(self, approval_id: int) -> Approval:
        """重试执行（FAILED → APPROVED）。

        纯技术失败（网络超时）要求重新走审批是浪费，
        但必须有次数上限 —— 否则无限重试会掩盖真问题。
        """
        approval = await self._get(approval_id)
        context = self._context_for(approval)
        context.retry_count = approval.retry_count

        await self._apply(approval, event="retry_execute", context=context)
        approval.retry_count += 1
        await self.session.flush()
        await self._log_action(approval_id, "retry_execute", comment=f"第 {approval.retry_count} 次重试")
        return approval

    # ========================================================
    # 内部
    # ========================================================

    async def _get(self, approval_id: int) -> Approval:
        approval = await self.repo.get(approval_id)
        if approval is None:
            raise NotFoundError("审批单不存在", resource="Approval", resource_id=approval_id)
        return approval

    def _context_for(self, approval: Approval) -> ApprovalContext:
        """构造状态机判定上下文。

        职责分离的两个 ID 都从**库里的真实数据**取 ——
        不用调用方传进来的值，否则伪造 `requested_by` 就能绕过隔离校验。
        """
        return ApprovalContext(
            roles=self.ctx.roles,
            actor_id=self.ctx.actor_id,
            requested_by=approval.requested_by,
            has_payload=bool(approval.payload_after),
            retry_count=approval.retry_count,
        )

    async def _apply(
        self,
        approval: Approval,
        *,
        event: str,
        context: ApprovalContext,
        reason: str | None = None,
    ) -> None:
        """执行状态转换并留痕。

        `resolve_or_raise` 会在非法转换或 guard 失败时抛异常，
        因此调用点无需自己校验 —— 校验逻辑只有状态机一份。
        """
        result = ApprovalStateMachine.resolve_or_raise(approval.status, event, context)

        if not result.changed:
            raise ConflictError(
                f"审批单 {approval.approval_no} 当前状态 {approval.status} 不允许执行 {event}",
                code="APPROVAL_STATE_UNCHANGED",
            )

        await record_transition(
            self.session,
            self.ctx,
            machine=ApprovalStateMachine.machine_name,
            entity_type="Approval",
            entity_id=approval.id,
            from_state=result.from_state,
            to_state=result.to_state,
            event=event,
            reason=reason,
        )
        approval.status = result.to_state
        await self.session.flush()

    async def _log_action(
        self, approval_id: int, action: str, *, comment: str | None = None
    ) -> None:
        """写审批操作日志（谁在什么时候做了什么）。"""
        await self.actions.log(
            approval_id=approval_id,
            action=action,
            actor_type=self.ctx.actor_type,
            actor_id=self.ctx.actor_id,
            comment=comment,
        )
