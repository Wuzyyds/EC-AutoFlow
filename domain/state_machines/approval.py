"""审批状态机（TDD-04 §3）。

核心设计判断：

1. **APPROVED 与 EXECUTED 必须分离**
   "人已同意" ≠ "事已办成"。平台可能拒绝（商品已下架不能改价）、
   网络可能失败。合并会导致"审批通过但实际没生效"无法追踪。
   这是审批系统最常见的设计错误。

2. **EXPIRED 后不能直接转回 PENDING**
   必须新建审批单。审批的价值在于"对人的决策留痕"，
   超时后重新提交应重新走决策，否则审批人可能基于过时信息判断。

3. **FAILED 允许回到 APPROVED（重试执行）但有次数上限**
   纯技术失败（网络超时）要求重新审批是浪费。
   但必须限次 —— 否则无限重试会掩盖真问题。
   超过上限强制 CANCELED，要求重新发起。

4. **职责分离（发起人 ≠ 审批人）**
   应用层 guard + 数据库层 CHECK 双重保障。
   数据库层用 `chk_separation_of_duties` 约束兜底（ADR-007 §3.1 说明
   低版本 MySQL 需用触发器实现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from core.constants import ApprovalStatus
from domain.state_machines.base import StateMachine, Transition

__all__ = [
    "ApprovalContext",
    "ApprovalStateMachine",
    "APPROVAL_TRANSITIONS",
]

S = ApprovalStatus


@dataclass
class ApprovalContext:
    """审批状态机的 guard 判定上下文。"""

    roles: frozenset[str] = frozenset()

    #: 当前操作人 ID。系统操作为 None。
    actor_id: int | None = None

    #: 审批单发起人 ID。用于职责分离校验。
    requested_by: int | None = None

    #: 变更内容是否已填写（payload_after 非空）。
    has_payload: bool = True

    #: 执行重试次数。
    retry_count: int = 0
    max_retries: int = 3

    extra: dict[str, Any] = field(default_factory=dict)


# ============================================================
# Guard
# ============================================================


def _is_not_requester(ctx: ApprovalContext | None) -> bool:
    """职责分离：发起人不得审批自己的单。

    系统操作（actor_id 为 None）也不允许 —— 审批必须由人做。
    """
    if ctx is None or ctx.actor_id is None or ctx.requested_by is None:
        return False
    return ctx.actor_id != ctx.requested_by


def _is_requester(ctx: ApprovalContext | None) -> bool:
    """只有发起人能撤销自己的单。"""
    if ctx is None or ctx.actor_id is None or ctx.requested_by is None:
        return False
    return ctx.actor_id == ctx.requested_by


def _has_payload(ctx: ApprovalContext | None) -> bool:
    return not (ctx and not ctx.has_payload)


def _can_retry(ctx: ApprovalContext | None) -> bool:
    if ctx is None:
        return False
    return ctx.retry_count < ctx.max_retries


T = Transition

APPROVAL_TRANSITIONS: tuple[Transition, ...] = (
    # ---------- 创建 ----------
    T(
        S.DRAFT,
        S.PENDING,
        "submit",
        guard=_has_payload,
        guard_message="变更内容为空，无法提交审批",
        description="提交审批",
    ),
    T(S.DRAFT, S.CANCELED, "cancel", description="撤销草稿"),
    # ---------- 审批决策 ----------
    T(
        S.PENDING,
        S.APPROVED,
        "approve",
        guard=_is_not_requester,
        guard_message="发起人不能审批自己提交的申请",
        requires_role=("approver", "ops_lead", "admin"),
        description="审批通过",
    ),
    T(
        S.PENDING,
        S.REJECTED,
        "reject",
        guard=_is_not_requester,
        guard_message="发起人不能审批自己提交的申请",
        requires_reason=True,
        requires_role=("approver", "ops_lead", "admin"),
        description="审批驳回",
    ),
    T(
        S.PENDING,
        S.EXPIRED,
        "expire",
        requires_reason=True,
        description="超时未处理，自动置为已超时",
    ),
    T(
        S.PENDING,
        S.CANCELED,
        "cancel",
        guard=_is_requester,
        guard_message="只有发起人可以撤销申请",
        requires_reason=True,
        description="发起人撤销",
    ),
    # ---------- 执行 ----------
    T(S.APPROVED, S.EXECUTED, "execute_success", description="执行成功"),
    T(
        S.APPROVED,
        S.FAILED,
        "execute_fail",
        requires_reason=True,
        description="执行失败",
    ),
    # ---------- 执行失败的处理 ----------
    T(
        S.FAILED,
        S.APPROVED,
        "retry_execute",
        guard=_can_retry,
        guard_message="重试已达上限，请重新发起审批",
        description="重试执行（不重新审批，但有次数上限）",
    ),
    T(
        S.FAILED,
        S.CANCELED,
        "abandon",
        requires_reason=True,
        description="放弃执行",
    ),
)


class ApprovalStateMachine(StateMachine):
    """审批状态机。"""

    machine_name: ClassVar[str] = "approval"

    states: ClassVar[frozenset[str]] = frozenset(s.value for s in S)

    initial: ClassVar[str] = S.DRAFT.value

    terminal: ClassVar[frozenset[str]] = frozenset(
        {S.REJECTED.value, S.EXPIRED.value, S.CANCELED.value, S.EXECUTED.value}
    )

    transitions: ClassVar[tuple[Transition, ...]] = APPROVAL_TRANSITIONS
