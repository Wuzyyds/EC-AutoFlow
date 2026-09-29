"""同步状态机（TDD-04 §4）。

核心设计判断：

1. **连续失败自动暂停（ERROR → PAUSED）**
   如果店铺 Token 失效，重试 100 次也没用，只会刷爆日志和告警。
   连续失败达阈值（默认 5 次）自动暂停，等人介入。
   阈值在 TDD-05 的配置表中可调。

2. **游标只在成功时前移**（关键实现约束）
   每页都写游标会导致中途失败时丢数据。
   本状态机不涉及游标，但 service 层必须遵守这条 —— 见
   apps/worker/AGENTS.md。

3. **没有终态**
   同步是周期性循环，IDLE / RUNNING / ERROR / PAUSED 之间流转，
   不会"结束"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from core.constants import SyncStatus
from domain.state_machines.base import StateMachine, Transition

__all__ = [
    "SyncContext",
    "SyncStateMachine",
    "SYNC_TRANSITIONS",
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
]

S = SyncStatus

#: 连续失败达此值自动暂停（TDD-05 可配置覆盖）
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class SyncContext:
    """同步状态机的 guard 判定上下文。"""

    roles: frozenset[str] = frozenset()

    #: 当前连续失败次数。
    consecutive_failures: int = 0
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES

    #: 该资源是否已被人工暂停（防止自动恢复）。
    manually_paused: bool = False

    extra: dict[str, Any] = field(default_factory=dict)


def _can_retry(ctx: SyncContext | None) -> bool:
    """未达连续失败上限才允许自动重试。"""
    if ctx is None:
        return True
    return ctx.consecutive_failures < ctx.max_consecutive_failures


def _exceeded_failure_limit(ctx: SyncContext | None) -> bool:
    if ctx is None:
        return False
    return ctx.consecutive_failures >= ctx.max_consecutive_failures


T = Transition

SYNC_TRANSITIONS: tuple[Transition, ...] = (
    T(S.IDLE, S.RUNNING, "start", description="开始同步"),
    T(S.RUNNING, S.IDLE, "succeed", description="同步成功"),
    T(
        S.RUNNING,
        S.ERROR,
        "fail",
        requires_reason=True,
        description="同步失败",
    ),
    T(
        S.ERROR,
        S.RUNNING,
        "retry",
        guard=_can_retry,
        guard_message=f"连续失败已达 {DEFAULT_MAX_CONSECUTIVE_FAILURES} 次，已自动暂停",
        description="自动重试",
    ),
    T(
        S.ERROR,
        S.PAUSED,
        "auto_pause",
        guard=_exceeded_failure_limit,
        guard_message="未达自动暂停阈值，应先重试",
        requires_reason=True,
        description="连续失败超限，自动暂停",
    ),
    T(
        S.PAUSED,
        S.IDLE,
        "resume",
        requires_role=("ops", "ops_lead", "admin"),
        description="人工恢复",
    ),
    T(
        S.IDLE,
        S.PAUSED,
        "manual_pause",
        requires_role=("ops", "ops_lead", "admin"),
        description="人工暂停",
    ),
    T(
        S.RUNNING,
        S.PAUSED,
        "manual_pause",
        requires_role=("ops", "ops_lead", "admin"),
        description="人工暂停（同步进行中）",
    ),
)


class SyncStateMachine(StateMachine):
    """同步状态机。

    注意：**没有终态** —— 同步是周期性循环。
    """

    machine_name: ClassVar[str] = "sync"

    states: ClassVar[frozenset[str]] = frozenset(s.value for s in S)

    initial: ClassVar[str] = S.IDLE.value

    terminal: ClassVar[frozenset[str]] = frozenset()

    transitions: ClassVar[tuple[Transition, ...]] = SYNC_TRANSITIONS
