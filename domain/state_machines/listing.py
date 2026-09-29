"""上架状态机（TDD-04 §2）。

12 个状态，与 core.constants.ListingStatus 严格一致。
任何改动必须同步：core/constants.py + 本文件的转换表 + TDD-04 文档。

核心设计判断（TDD-04 §2.1）：

1. **VALIDATING 是独立状态而非瞬态**
   校验可能耗时（要拉平台类目 schema、检查类目资格），
   且校验失败的记录需要保留用于分析。

2. **REJECTED 之后回 DRAFT 而非 VALIDATING**
   审批被拒通常意味着商品信息需要实质修改，
   回到 DRAFT 强制重新校验，避免"改了价格绕过校验"。

3. **PARTIAL_ACTIVE 既不能归为 FAILED 也不能归为 ACTIVE**
   - 归为 FAILED → 运营以为没上架成功，不监控库存（商品在卖却没人管）
   - 归为 ACTIVE → 还有 SKU 没上，不完整
   它是一个**必须人工处理的中间态**。

4. **poll_timeout 必须显式建模**
   轮询超时时，商品可能**已经上架成功了**，不能简单重试。
   必须转 FAILED 并生成人工待办去核实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from core.constants import ListingStatus
from domain.state_machines.base import StateMachine, Transition

__all__ = [
    "ListingContext",
    "ListingStateMachine",
    "LISTING_TRANSITIONS",
]

S = ListingStatus


# ============================================================
# 判定上下文
# ============================================================


@dataclass
class ListingContext:
    """上架状态机的 guard 判定上下文。

    由 service 层组装 —— 状态机本身不去查库，保持纯函数特性。
    """

    # --- 权限 ---
    roles: frozenset[str] = frozenset()
    has_edit_permission: bool = False

    # --- 数据完整性 ---
    missing_required_fields: tuple[str, ...] = ()

    # --- 审批判定（由 approval_rules 计算得出，不在状态机里查规则）---
    requires_approval: bool = False
    approval_reason: str = ""

    # --- 变体情况（判断 partial_active 用）---
    variant_total: int = 1
    variant_succeeded: int = 0

    # --- 重试 ---
    retry_count: int = 0
    max_retries: int = 3

    # --- 平台能力（决定是否需要 SUBMITTED/PROCESSING 中间态）---
    capability: str = "NATIVE"

    # --- 附带信息 ---
    extra: dict[str, Any] = field(default_factory=dict)


# ============================================================
# Guard 函数
# ============================================================


def _has_required_fields(ctx: ListingContext | None) -> bool:
    """必填字段完整才能进入校验。"""
    if ctx is None:
        return True
    return not ctx.missing_required_fields


def _needs_approval(ctx: ListingContext | None) -> bool:
    """需要审批 → 走 PENDING_APPROVAL。"""
    return bool(ctx and ctx.requires_approval)


def _not_needs_approval(ctx: ListingContext | None) -> bool:
    """免审批 → 直接 QUEUED。

    与 _needs_approval 是互补关系。两条转换共用同一事件，
    靠 guard 分支 —— 这是声明式状态机的典型用法。
    """
    return not (ctx and ctx.requires_approval)


def _can_edit(ctx: ListingContext | None) -> bool:
    return bool(ctx and ctx.has_edit_permission)


def _has_partial_success(ctx: ListingContext | None) -> bool:
    """至少一个子项成功且至少一个失败 → PARTIAL_ACTIVE。

    精确定义（TDD-04 §2.1 关键判断 3）：
        0 < variant_succeeded < variant_total
    """
    if ctx is None:
        return False
    return 0 < ctx.variant_succeeded < ctx.variant_total


def _all_failed(ctx: ListingContext | None) -> bool:
    return bool(ctx and ctx.variant_succeeded == 0)


# ============================================================
# 转换表
# ============================================================

T = Transition

LISTING_TRANSITIONS: tuple[Transition, ...] = (
    # ---------- 创建与校验 ----------
    T(
        S.DRAFT,
        S.VALIDATING,
        "validate",
        guard=_has_required_fields,
        guard_message="必填字段不完整，无法校验",
        description="提交本地校验",
    ),
    T(S.VALIDATING, S.DRAFT, "validation_failed", description="校验失败，退回草稿"),
    T(
        S.VALIDATING,
        S.PENDING_APPROVAL,
        "validation_passed",
        guard=_needs_approval,
        guard_message="该操作未达到审批条件",
        description="校验通过且需审批",
    ),
    T(
        S.VALIDATING,
        S.QUEUED,
        "validation_passed",
        guard=_not_needs_approval,
        guard_message="该操作超过免审批额度，需走审批流程",
        description="校验通过且免审批",
    ),
    # ---------- 审批 ----------
    T(S.PENDING_APPROVAL, S.QUEUED, "approve", description="审批通过"),
    T(
        S.PENDING_APPROVAL,
        S.REJECTED,
        "reject",
        requires_reason=True,
        description="审批驳回",
    ),
    T(
        S.PENDING_APPROVAL,
        S.REJECTED,
        "expire",
        requires_reason=True,
        description="审批超时自动驳回",
    ),
    # ---------- 驳回返工 ----------
    T(
        S.REJECTED,
        S.DRAFT,
        "revise",
        guard=_can_edit,
        guard_message="无编辑权限",
        description="修改后重新走全流程（强制重新校验）",
    ),
    # ---------- 提交与平台处理 ----------
    T(S.QUEUED, S.SUBMITTED, "submit", description="提交到平台（异步平台才有此态）"),
    T(
        S.QUEUED,
        S.FAILED,
        "submit_failed",
        requires_reason=True,
        description="提交动作本身失败（网络/认证）",
    ),
    T(S.SUBMITTED, S.PROCESSING, "platform_accepted", description="平台已受理，等待处理"),
    T(
        S.SUBMITTED,
        S.FAILED,
        "platform_rejected",
        requires_reason=True,
        description="平台拒绝受理",
    ),
    # ---------- 异步处理结果（三分支）----------
    T(
        S.PROCESSING,
        S.ACTIVE,
        "all_succeeded",
        guard=lambda ctx: not _has_partial_success(ctx) and not _all_failed(ctx),
        guard_message="并非全部子项成功",
        description="全部子项成功",
    ),
    T(
        S.PROCESSING,
        S.PARTIAL_ACTIVE,
        "partially_succeeded",
        guard=_has_partial_success,
        guard_message="非部分成功场景",
        description="部分子项成功（商品已在售但不完整）",
    ),
    T(
        S.PROCESSING,
        S.FAILED,
        "all_failed",
        guard=_all_failed,
        guard_message="存在成功的子项，应走 partial_active",
        requires_reason=True,
        description="全部子项失败",
    ),
    T(
        S.PROCESSING,
        S.FAILED,
        "poll_timeout",
        requires_reason=True,
        description=(
            "轮询超时 —— 商品可能已上架成功，必须人工核实而非重试"
        ),
    ),
    # ---------- PARTIAL_ACTIVE 的出路 ----------
    T(
        S.PARTIAL_ACTIVE,
        S.ACTIVE,
        "retry_succeeded",
        description="失败子项补发成功",
    ),
    T(
        S.PARTIAL_ACTIVE,
        S.ACTIVE,
        "abandon_failed_variants",
        requires_reason=True,
        requires_role=("ops_lead", "admin"),
        description="人工确认放弃失败子项（需主管权限并留原因）",
    ),
    T(
        S.PARTIAL_ACTIVE,
        S.FAILED,
        "all_variants_failed",
        requires_reason=True,
        description="后续失败导致全部子项都失败",
    ),
    # ---------- 线上管理 ----------
    T(S.ACTIVE, S.INACTIVE, "delist", description="下架"),
    T(S.PARTIAL_ACTIVE, S.INACTIVE, "delist", description="下架（含部分上线）"),
    T(S.INACTIVE, S.QUEUED, "relist", description="重新上架（走完整提交链路）"),
    # ---------- 失败重试 ----------
    T(S.FAILED, S.QUEUED, "retry", description="重试提交"),
    T(S.FAILED, S.DRAFT, "revise", description="回到草稿修改"),
    # ---------- 删除 ----------
    T(S.DRAFT, S.DELETED, "delete", description="删除"),
    T(S.REJECTED, S.DELETED, "delete", description="删除"),
    T(S.FAILED, S.DELETED, "delete", description="删除"),
    T(S.INACTIVE, S.DELETED, "delete", description="删除"),
)


class ListingStateMachine(StateMachine):
    """上架状态机。"""

    machine_name: ClassVar[str] = "listing"

    states: ClassVar[frozenset[str]] = frozenset(s.value for s in S)

    initial: ClassVar[str] = S.DRAFT.value

    #: 严格终态（无出边）。
    #: 注意只有 DELETED —— ACTIVE / INACTIVE 虽业务上是"稳定态"，
    #: 但它们有 delist / relist / delete 出边，不算终态。
    terminal: ClassVar[frozenset[str]] = frozenset({S.DELETED.value})

    transitions: ClassVar[tuple[Transition, ...]] = LISTING_TRANSITIONS

    # ---------- 业务辅助 ----------

    @classmethod
    def next_events_for_user(
        cls, current: str, ctx: ListingContext
    ) -> list[dict[str, str]]:
        """列出当前用户在当前状态下可执行的动作。

        给前端渲染按钮用 —— 只返回 guard 通过的动作，
        用户不会看到点了会失败的红按钮。
        """
        actions: list[dict[str, str]] = []
        for t in cls.allowed_transitions(current):
            if t.guard is not None and not t.guard(ctx):
                continue
            if t.requires_role and not (set(t.requires_role) & set(ctx.roles)):
                continue
            actions.append(
                {
                    "event": t.event,
                    "label": t.description or t.event,
                    "target_state": t.target,
                    "requires_reason": "true" if t.requires_reason else "false",
                }
            )
        return actions
