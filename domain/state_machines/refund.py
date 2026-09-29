"""退款/售后状态机（TDD-04 §6）—— 混合驱动。

驱动方取决于**平台能力**（TDD-03 能力矩阵的直接应用）：

| 平台 | REFUND_CREATE | 状态机行为 |
|---|---|---|
| Amazon | UNSUPPORTED | **不进入 AUTO_APPROVED**。系统只做"分类 + 建议"，生成人工待办；状态变化全部由平台同步驱动 |
| TikTok / Shopee / Lazada / Shopify | NATIVE | 可走 AUTO_APPROVED，系统可执行退款 |

关键实现：guard 中检查的是**能力**（`platform_supports_refund_write`），
不是**平台名**。这样新增平台时，只要它声明同样的能力，逻辑自动复用。
业务代码出现 `if platform == "amazon"` 即为设计缺陷（TDD-01 原则 1）。

自动决策的留痕要求（TDD-02 refunds.auto_decision）：
    每次自动决策必须写入命中的规则、判定依据、评分因子。
    当买家投诉"为什么给我拒了"，需要能复现当时的决策依据 ——
    **只有决策结果没有依据，等于没有审计能力。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar

from core.constants import RefundStatus, RiskLevel
from core.money import ZERO
from domain.state_machines.base import StateMachine, Transition

__all__ = [
    "RefundContext",
    "RefundStateMachine",
    "REFUND_TRANSITIONS",
    "DEFAULT_AUTO_REFUND_LIMIT",
    "DEFAULT_FRAUD_THRESHOLD",
]

S = RefundStatus

#: 自动执行退款的默认金额上限（TDD-05 §3.3）
#: 注意与"免审批额度"是两个不同概念，且这个更严格 ——
#: 免审批只是"不用人点同意"，自动执行是"系统直接打款给买家"。
DEFAULT_AUTO_REFUND_LIMIT = Decimal("30")

#: 欺诈评分阈值，超过则自动拒绝
DEFAULT_FRAUD_THRESHOLD = Decimal("0.80")


@dataclass
class RefundContext:
    """退款状态机的 guard 判定上下文。

    由 service 层组装 —— 规则命中详情也在这里带进来，
    便于写入 auto_decision 留痕。
    """

    roles: frozenset[str] = frozenset()

    # --- 平台能力（来自 TDD-03 能力矩阵）---
    #: 平台是否支持卖家侧执行退款。Amazon 为 False。
    platform_supports_refund_write: bool = False

    # --- 风险判定 ---
    risk_level: str = RiskLevel.LOW.value
    amount: Decimal = ZERO
    currency: str = "USD"

    #: 自动执行额度上限
    auto_refund_limit: Decimal = DEFAULT_AUTO_REFUND_LIMIT

    # --- 反欺诈 ---
    fraud_score: Decimal = ZERO
    fraud_threshold: Decimal = DEFAULT_FRAUD_THRESHOLD

    # --- 留痕：命中的规则明细，写入 auto_decision ---
    matched_rules: list[dict[str, Any]] = field(default_factory=list)

    extra: dict[str, Any] = field(default_factory=dict)


# ============================================================
# Guard
# ============================================================


def _can_auto_approve(ctx: RefundContext | None) -> bool:
    """自动批准退款的三个必要条件。

    1. **平台支持**卖家侧执行退款（Amazon 不支持 → 永远走不到这里）
    2. 风险等级为低
    3. 金额在自动额度内

    任一不满足都退回人工处理 —— 这是刻意的保守设计：
    退错款的钱要不回来，而人工多审一单只是慢一点。
    """
    if ctx is None:
        return False
    return (
        ctx.platform_supports_refund_write
        and ctx.risk_level == RiskLevel.LOW.value
        and ctx.amount <= ctx.auto_refund_limit
    )


def _should_auto_reject(ctx: RefundContext | None) -> bool:
    """欺诈评分超阈值 → 自动拒绝。"""
    if ctx is None:
        return False
    return ctx.fraud_score > ctx.fraud_threshold


T = Transition

REFUND_TRANSITIONS: tuple[Transition, ...] = (
    # ---------- 平台驱动的流转（镜像部分）----------
    T(S.REQUESTED, S.APPROVED, "platform_approve", description="平台批准退款申请"),
    T(S.REQUESTED, S.REJECTED, "platform_reject", description="平台拒绝退款申请"),
    T(S.REQUESTED, S.CLOSED, "platform_close", description="买家撤诉/平台关闭"),
    # ---------- 本系统自动决策（仅当平台支持写操作）----------
    T(
        S.REQUESTED,
        S.AUTO_APPROVED,
        "auto_decide_approve",
        guard=_can_auto_approve,
        guard_message=(
            "不满足自动批准条件（需同时满足：平台支持执行退款、"
            "风险等级为低、金额在自动额度内），已转人工处理"
        ),
        description="规则自动批准并执行退款",
    ),
    T(
        S.REQUESTED,
        S.REJECTED,
        "auto_decide_reject",
        guard=_should_auto_reject,
        guard_message="欺诈评分未超阈值，不应自动拒绝",
        requires_reason=True,
        description="反欺诈规则自动拒绝",
    ),
    # ---------- 退款完成 ----------
    T(S.APPROVED, S.REFUNDED, "platform_refund_complete", description="平台退款完成"),
    T(
        S.AUTO_APPROVED,
        S.REFUNDED,
        "platform_refund_complete",
        description="本系统执行退款完成",
    ),
    T(S.APPROVED, S.CLOSED, "platform_cancel", description="批准后取消（未实际退款）"),
    T(S.REFUNDED, S.CLOSED, "case_closed", description="结案归档"),
)


class RefundStateMachine(StateMachine):
    """退款状态机（混合驱动）。"""

    machine_name: ClassVar[str] = "refund"

    states: ClassVar[frozenset[str]] = frozenset(s.value for s in S)

    initial: ClassVar[str] = S.REQUESTED.value

    terminal: ClassVar[frozenset[str]] = frozenset(
        {S.REJECTED.value, S.CLOSED.value}
    )

    transitions: ClassVar[tuple[Transition, ...]] = REFUND_TRANSITIONS

    # ========================================================
    # 业务辅助
    # ========================================================

    @classmethod
    def requires_manual_handling(cls, ctx: RefundContext) -> bool:
        """判断该退款是否必须人工处理。

        这是 Amazon 场景下的核心分支 —— 平台不支持写操作，
        系统只能做分类与建议，实际退款要在卖家后台手动完成。
        """
        return not ctx.platform_supports_refund_write

    @classmethod
    def build_auto_decision(
        cls, ctx: RefundContext, decision: str
    ) -> dict[str, Any]:
        """构造 auto_decision 留痕内容。

        **每次自动决策都必须调用此方法并落库**（TDD-04 §6.4）。
        """
        return {
            "decision": decision,
            "amount": str(ctx.amount),
            "currency": ctx.currency,
            "risk_level": ctx.risk_level,
            "fraud_score": str(ctx.fraud_score),
            "fraud_threshold": str(ctx.fraud_threshold),
            "auto_refund_limit": str(ctx.auto_refund_limit),
            "platform_supports_write": ctx.platform_supports_refund_write,
            "matched_rules": ctx.matched_rules,
        }
