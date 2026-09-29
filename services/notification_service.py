"""通知服务。

**分级推送策略**（TDD-05 §4.4）：

    P0 —— 不静默，立即推
    P1 —— 夜间静默（次日早晨汇总推）
    P2 —— 只进日报，不单独推

**为什么必须分级**：

    如果 P2 半夜也推，团队会直接关掉通知 ——
    那时真正的 P0 也收不到了。这是"告警疲劳"的标准失败路径，
    而且一旦发生就几乎不可逆：没人会主动把通知重新打开。

**Phase 1 范围**：

    本模块只做"分级决策 + 推送编排"，
    实际通道（企微 / 飞书）在 `integrations/notify/` 中实现。
    通道未配置时**记录并返回未送达**，不抛异常 ——
    通知失败不应该让业务事务回滚。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Protocol

from core.constants import AlertLevel
from core.timeutil import utc_now

__all__ = [
    "NotificationChannel",
    "NotificationResult",
    "NotificationService",
    "QUIET_HOURS_END",
    "QUIET_HOURS_START",
]

#: 夜间静默窗口（本地时间，含起不含止）。
QUIET_HOURS_START = time(22, 0)
QUIET_HOURS_END = time(8, 0)

#: 未投递原因。定义成常量而非散落字面量 ——
#: 调用方要靠它做分支判断，拼错一个字符就会静默走错分支。
REASON_SUPPRESSED = "SUPPRESSED"  # 被静默策略拦截
REASON_NO_CHANNEL = "NO_CHANNEL"  # 未配置通知通道
REASON_FAILED = "FAILED"  # 通道调用报错


class NotificationChannel(Protocol):
    """通知通道协议。

    实现方见 `integrations/notify/`（企微、飞书）。
    """

    async def send(self, *, title: str, content: str, level: str) -> bool:
        """发送一条通知，返回是否成功。"""
        ...


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """一次通知投递的结果。"""

    alert_id: int
    level: str
    delivered: bool
    #: 未投递原因：`SUPPRESSED`（静默）/ `NO_CHANNEL`（未配置）/ `FAILED`（通道报错）
    reason: str | None = None

    @property
    def suppressed(self) -> bool:
        return self.reason == REASON_SUPPRESSED


class NotificationService:
    """通知编排服务。

    注意：本服务**不持有数据库会话** ——
    通知是外部副作用，不应该参与业务事务。
    拿会话进来迟早会有人在事务里调它，然后因为通道超时拖垮整个事务。
    """

    def __init__(self, channel: NotificationChannel | None = None) -> None:
        self.channel = channel

    # ========================================================
    # 分级决策
    # ========================================================

    @staticmethod
    def is_quiet_hours(moment: datetime | None = None) -> bool:
        """是否处于夜间静默窗口。

        跨零点判断：22:00–08:00 不能写成 `start <= t <= end`，
        那样永远为假。
        """
        current = (moment or utc_now()).time()
        return current >= QUIET_HOURS_START or current < QUIET_HOURS_END

    @classmethod
    def should_push(cls, level: str, *, moment: datetime | None = None) -> bool:
        """判断该级别此刻是否应该推送。

        | 级别 | 白天 | 夜间 |
        |---|---|---|
        | P0 | 推 | **推**（不静默） |
        | P1 | 推 | 静默 |
        | P2 | 不单独推（只进日报） | 静默 |
        """
        if level == AlertLevel.P0.value:
            return True
        if level == AlertLevel.P1.value:
            return not cls.is_quiet_hours(moment)
        return False

    # ========================================================
    # 投递
    # ========================================================

    async def dispatch(
        self,
        *,
        alert_id: int,
        level: str,
        title: str,
        content: str,
        moment: datetime | None = None,
    ) -> NotificationResult:
        """按级别决定是否推送并投递。

        **任何失败都只记录、不抛出** ——
        通知是辅助能力，让它把业务事务带崩是本末倒置。
        """
        if not self.should_push(level, moment=moment):
            return NotificationResult(
                alert_id=alert_id, level=level, delivered=False, reason=REASON_SUPPRESSED
            )

        if self.channel is None:
            return NotificationResult(
                alert_id=alert_id, level=level, delivered=False, reason=REASON_NO_CHANNEL
            )

        try:
            ok = await self.channel.send(title=title, content=content, level=level)
        except Exception:  # noqa: BLE001
            # 通道异常（网络、鉴权）不应该向上冒泡
            return NotificationResult(
                alert_id=alert_id, level=level, delivered=False, reason=REASON_FAILED
            )

        return NotificationResult(
            alert_id=alert_id,
            level=level,
            delivered=ok,
            reason=None if ok else REASON_FAILED,
        )

    @staticmethod
    def build_alert_message(alert: Any) -> tuple[str, str]:
        """从预警记录构造 (标题, 正文)。

        正文**只放业务字段**，不放原始报文 ——
        原始报文可能含 PII，而通知会经第三方通道传输。
        """
        level = getattr(alert, "level", AlertLevel.P2.value)
        title = f"[{level}] {getattr(alert, 'title', '系统预警')}"
        content = str(getattr(alert, "message", ""))[:2000]
        return title, content
