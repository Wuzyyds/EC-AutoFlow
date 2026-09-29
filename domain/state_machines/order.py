"""订单状态机（TDD-04 §5）—— 只读镜像。

**这是五个状态机里唯一不由本系统驱动的。**

设计立场（TDD-04 §5.1）：
    订单状态由**平台**决定，本系统不做订单状态流转。
    `orders.order_status` 是平台状态的本地镜像，同步任务负责更新。

因此这个状态机的作用与其他四个不同：

| 状态机 | 方向 | 谁驱动 |
|---|---|---|
| Listing | 本系统 → 平台 | 本系统 |
| Approval | 纯本地 | 本系统 |
| Sync | 纯本地 | 本系统 |
| **Order** | **平台 → 本系统** | **平台** |
| Refund | 本系统 → 平台（部分平台） | 混合 |

为什么不把订单状态机做成可写：
    本系统定位是经营分析，不是 OMS（PRD 2.3 已声明不做 WMS/OMS）。
    如果自己维护订单状态，就会与平台产生冲突（平台改了状态我们不知道），
    最终数据不可信。

关键设计：**异常换向不阻断，只告警**
    平台的真实行为总比文档多。如果同步到"不符合预期"的换向就拒绝写入，
    会导致本地数据与平台脱节 —— 比"接受异常并告警"更糟。
    **数据完整性优先于状态纯洁性。**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from core.constants import OrderStatus
from domain.state_machines.base import StateMachine, Transition

__all__ = [
    "OrderContext",
    "OrderStateMachine",
    "ORDER_TRANSITIONS",
    "ORDER_ANOMALY_ALLOWED",
    "TransitionVerdict",
]

S = OrderStatus


@dataclass
class OrderContext:
    """订单状态机上下文（镜像场景下用得很少）。"""

    platform: str = ""
    #: 平台上报的原始状态文本（未映射成功的会记录在此，便于补映射）
    raw_status: str = ""
    extra: dict[str, object] | None = None


T = Transition

#: 平台可能的状态流转。本系统不主动触发，只用于校验同步结果。
ORDER_TRANSITIONS: tuple[Transition, ...] = (
    T(S.PENDING, S.UNSHIPPED, "platform_confirm", description="平台确认订单"),
    T(S.PENDING, S.CANCELED, "platform_cancel", description="下单后取消"),
    T(S.UNSHIPPED, S.PARTIALLY_SHIPPED, "platform_partial_ship", description="部分发货"),
    T(S.UNSHIPPED, S.SHIPPED, "platform_ship", description="发货"),
    T(S.UNSHIPPED, S.CANCELED, "platform_cancel", description="发货前取消"),
    T(S.PARTIALLY_SHIPPED, S.SHIPPED, "platform_ship_rest", description="剩余部分发货"),
    T(S.SHIPPED, S.DELIVERED, "platform_deliver", description="签收"),
    T(S.SHIPPED, S.RETURNED, "platform_return", description="发货后退货"),
    T(S.DELIVERED, S.RETURNED, "platform_return", description="签收后退货"),
)

#: 已知的"异常但真实存在"的换向。
#:
#: 这些不是 bug，是平台的真实行为。记录下来是为了：
#:   1. 不触发无意义的告警（已知异常，见怪不怪）
#:   2. 但仍然写入 anomaly_detail 供后续分析
ORDER_ANOMALY_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        (S.CANCELED.value, S.UNSHIPPED.value),  # 平台撤销取消（确有发生）
        (S.SHIPPED.value, S.CANCELED.value),  # 发货后取消（拦截成功）
        (S.RETURNED.value, S.SHIPPED.value),  # 退货被撤销
        (S.DELIVERED.value, S.SHIPPED.value),  # 状态回退（平台数据修正）
    }
)


@dataclass(frozen=True, slots=True)
class TransitionVerdict:
    """同步结果校验结论。"""

    #: 是否为已声明的正常换向
    declared: bool
    #: 是否为已知的异常换向（不告警但仍标记）
    known_anomaly: bool
    #: 是否需要告警（未声明的换向）
    should_alert: bool
    #: 是否应当写入本地（**恒为 True** —— 见模块文档"数据完整性优先"）
    should_persist: bool = True

    @property
    def anomaly_flag(self) -> bool:
        """是否应给订单打上异常标记。"""
        return not self.declared


class OrderStateMachine(StateMachine):
    """订单状态机（镜像）。"""

    machine_name: ClassVar[str] = "order"

    states: ClassVar[frozenset[str]] = frozenset(s.value for s in S)

    initial: ClassVar[str] = S.PENDING.value

    #: 严格终态。
    #: DELIVERED **不是**终态 —— 签收后仍可能退货。
    terminal: ClassVar[frozenset[str]] = frozenset(
        {S.CANCELED.value, S.RETURNED.value}
    )

    transitions: ClassVar[tuple[Transition, ...]] = ORDER_TRANSITIONS

    # ========================================================
    # 镜像场景专用：校验平台上报的状态变化
    # ========================================================

    @classmethod
    def verdict(cls, from_state: str, to_state: str) -> TransitionVerdict:
        """校验一次平台上报的状态变化。

        与 `resolve()` 的区别：这里不做 guard 判定，也不拒绝。
        返回的 `should_persist` **恒为 True** —— 无论是否异常，
        数据都要落库，只决定是否告警与标记。

        用法（service 层）：
            v = OrderStateMachine.verdict(order.order_status, new_status)
            order.order_status = new_status
            if v.anomaly_flag:
                order.anomaly_flag = True
                order.anomaly_detail = {"from": old, "to": new}
            if v.should_alert:
                await alert_service.raise_order_anomaly(...)
        """
        if from_state == to_state:
            # 无变化，不算异常
            return TransitionVerdict(
                declared=True, known_anomaly=False, should_alert=False
            )

        declared = any(
            t.source == from_state and t.target == to_state for t in cls.transitions
        )
        if declared:
            return TransitionVerdict(
                declared=True, known_anomaly=False, should_alert=False
            )

        known = (from_state, to_state) in ORDER_ANOMALY_ALLOWED
        return TransitionVerdict(
            declared=False,
            known_anomaly=known,
            # 已知异常不告警，未知异常告警（可能是平台改了规则或映射漏了）
            should_alert=not known,
        )

    @classmethod
    def unmapped_statuses(cls) -> list[str]:
        """返回所有状态值的列表。

        供同步任务在遇到无法映射的平台状态时，
        记录原始值并触发"需补映射"告警。
        """
        return sorted(cls.states)
