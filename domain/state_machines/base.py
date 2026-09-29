"""声明式状态机框架（TDD-04 §1）。

设计原则（TDD-04 §1）：
    S1 状态转换必须显式声明 —— 转换表是唯一真相，
       代码不得用 `if status == ...` 硬编码流转
    S2 转换有前置条件（guard），不满足则**拒绝**而非抛异常
    S3 每次转换必须记录事件（由 service 层写入 state_transitions）
    S4 终态明确（无出边）
    S5 非法转换返回明确错误，携带可行动作列表
    S6 状态变更与副作用解耦（本模块只管状态，不发通知不起任务）
    S7 幂等（同状态重复触发同一事件 → 直接成功）
    S8 乐观锁保护（由 service 层实现）

为什么用声明式而不是 if/else：
    1. 状态图可自动生成（`to_mermaid()`），文档与代码不会脱节
    2. 可穷举测试（所有 (状态 × 事件) 组合），漏掉的分支会暴露
    3. 前端可直接渲染"当前可执行动作"（`allowed_transitions()`）

反面例子（必须避免）：
    if listing.status == "draft":
        if user.has_permission("listing.publish"):
            listing.status = "validating"
        else:
            raise PermissionError()
    elif listing.status == "validating":
        ...  # 200 行后没人知道到底允许哪些转换

正确做法：
    result = ListingStateMachine.resolve(
        current=listing.listing_status, event="validate", ctx=ctx
    )
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from core.exceptions import AppError

__all__ = [
    "Transition",
    "TransitionResult",
    "InvalidTransitionError",
    "GuardFailedError",
    "StateMachine",
]


class InvalidTransitionError(AppError):
    """非法状态转换。

    携带当前状态与**可行动作列表** —— 前端可以直接把 `allowed`
    渲染成按钮，而不是弹一个"操作失败"让用户猜。
    """

    code = "INVALID_TRANSITION"
    http_status = 409

    def __init__(
        self,
        *,
        machine: str,
        current: str,
        event: str,
        allowed: list[str] | None = None,
    ) -> None:
        allowed = allowed or []
        hint = "、".join(allowed) if allowed else "无"
        super().__init__(
            f"{machine} 当前处于 {current}，不支持事件 {event}",
            action=f"当前可执行：{hint}",
            context={
                "machine": machine,
                "current_state": current,
                "event": event,
                "allowed_events": allowed,
            },
        )
        self.machine = machine
        self.current = current
        self.event = event
        self.allowed = allowed


class GuardFailedError(AppError):
    """前置条件不满足。

    与 InvalidTransitionError 的区别：
    - InvalidTransitionError：这个状态**根本不允许**这个事件（定义问题）
    - GuardFailedError：状态允许，但**当前条件不满足**（业务问题）
    """

    code = "GUARD_FAILED"
    http_status = 403

    def __init__(
        self,
        *,
        machine: str,
        current: str,
        event: str,
        reason: str,
    ) -> None:
        super().__init__(
            reason,
            context={"machine": machine, "current_state": current, "event": event},
        )
        self.machine = machine
        self.current = current
        self.event = event
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Transition:
    """一条状态转换规则。

    Attributes:
        source: 源状态。
        target: 目标状态。
        event: 触发事件名（业务动作，如 "validate"、"approve"）。
        guard: 前置条件。接收上下文对象，返回是否放行。
               为 None 表示无条件放行。
        guard_message: guard 不满足时的提示（给用户看的）。
        requires_reason: 是否强制填写原因（驳回、失败类操作）。
        requires_role: 需要的角色（空元组表示不限制）。
        description: 说明，用于生成文档。
    """

    source: str
    target: str
    event: str
    guard: Callable[[Any], bool] | None = None
    guard_message: str | None = None
    requires_reason: bool = False
    requires_role: tuple[str, ...] = ()
    description: str = ""

    def __str__(self) -> str:
        return f"{self.source} --[{self.event}]--> {self.target}"


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """转换结果。

    采用"结果对象"而非"抛异常"的表达方式，是因为
    **"能不能转"是常规业务判断**，不是异常情况。
    上层可以据此给前端返回"可行动作 + 不可行动原因"的完整信息。
    """

    ok: bool
    machine: str
    from_state: str
    event: str
    to_state: str | None = None
    rejection_reason: str | None = None
    requires_reason: bool = False
    required_roles: tuple[str, ...] = ()
    transition: Transition | None = None

    @property
    def changed(self) -> bool:
        """是否产生了实际状态变化。

        幂等场景（同状态重复触发同一事件）返回 ok=True 但 changed=False。
        """
        return self.ok and self.to_state is not None and self.to_state != self.from_state


class StateMachine(ABC):
    """声明式状态机基类。

    子类只需定义四个类属性（`machine_name` 建议一并定义）：

        class FooStateMachine(StateMachine):
            machine_name = "foo"
            states = frozenset({...})
            initial = "..."
            terminal = frozenset({...})
            transitions = (Transition(...), ...)

    全部方法都是类方法 —— 状态机定义是静态的，不需要实例。
    """

    machine_name: ClassVar[str] = "unknown"

    #: 全部状态
    states: ClassVar[frozenset[str]] = frozenset()

    #: 初始状态
    initial: ClassVar[str] = ""

    #: 终态集合（无出边）
    terminal: ClassVar[frozenset[str]] = frozenset()

    #: 转换表
    transitions: ClassVar[tuple[Transition, ...]] = ()

    #: 按源状态索引（由 __init_subclass__ 自动构建）
    _by_source: ClassVar[dict[str, tuple[Transition, ...]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # 自动构建源状态索引，避免每次查询都遍历全表
        index: dict[str, list[Transition]] = {}
        for t in cls.transitions:
            index.setdefault(t.source, []).append(t)
        cls._by_source = {k: tuple(v) for k, v in index.items()}

    # ========================================================
    # 查询
    # ========================================================

    @classmethod
    def allowed_transitions(cls, current: str) -> tuple[Transition, ...]:
        """当前状态允许的全部转换（未过滤 guard）。

        前端据此渲染可操作按钮 —— 比"操作失败"友好得多。
        """
        return cls._by_source.get(current, ())

    @classmethod
    def allowed_events(cls, current: str) -> list[str]:
        """当前状态允许的事件名列表（去重）。"""
        seen: dict[str, None] = {}
        for t in cls.allowed_transitions(current):
            seen[t.event] = None
        return list(seen)

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        return state in cls.terminal

    @classmethod
    def is_valid_state(cls, state: str) -> bool:
        return state in cls.states

    # ========================================================
    # 转换判定
    # ========================================================

    @classmethod
    def resolve(cls, current: str, event: str, ctx: Any = None) -> TransitionResult:
        """判定一次转换。

        处理三种情况：
        1. 找不到匹配转换 → ok=False，给出可行动作
        2. 找到但 guard 不满足 → ok=False，给出 guard_message
        3. 找到且 guard 通过 → ok=True，返回目标状态

        **幂等（S7）**：若目标状态 == 当前状态，返回 ok=True 但 changed=False，
        表示"已经在这个状态了，无需重复执行"。

        Args:
            current: 当前状态。
            event: 触发事件。
            ctx: 传给 guard 的上下文对象（可以是 dataclass、dict、ORM 对象）。

        Returns:
            TransitionResult，不抛异常。
        """
        if not cls.is_valid_state(current):
            return TransitionResult(
                ok=False,
                machine=cls.machine_name,
                from_state=current,
                event=event,
                rejection_reason=f"未知状态：{current}",
            )

        candidates = tuple(t for t in cls.allowed_transitions(current) if t.event == event)

        if not candidates:
            return TransitionResult(
                ok=False,
                machine=cls.machine_name,
                from_state=current,
                event=event,
                rejection_reason=(
                    f"状态 {current} 不支持事件 {event}；"
                    f"可执行：{'、'.join(cls.allowed_events(current)) or '无'}"
                ),
            )

        # 逐条尝试 guard。多条同事件转换时（如"需审批/免审批"分支），
        # 第一个 guard 通过的即为结果。
        last_rejection: str | None = None
        for t in candidates:
            if t.guard is None:
                return cls._make_result(current, event, t)
            try:
                passed = bool(t.guard(ctx))
            except Exception as exc:  # noqa: BLE001
                # guard 内部异常不应导致状态机崩溃 —— 视为不通过并记录
                last_rejection = f"前置条件判定异常：{exc}"
                continue
            if passed:
                return cls._make_result(current, event, t)
            last_rejection = t.guard_message or f"前置条件不满足（{t.source} → {t.target}）"

        return TransitionResult(
            ok=False,
            machine=cls.machine_name,
            from_state=current,
            event=event,
            rejection_reason=last_rejection or "前置条件不满足",
            transition=candidates[0],
        )

    @classmethod
    def _make_result(cls, current: str, event: str, t: Transition) -> TransitionResult:
        return TransitionResult(
            ok=True,
            machine=cls.machine_name,
            from_state=current,
            event=event,
            to_state=t.target,
            requires_reason=t.requires_reason,
            required_roles=t.requires_role,
            transition=t,
        )

    @classmethod
    def resolve_or_raise(cls, current: str, event: str, ctx: Any = None) -> TransitionResult:
        """同 `resolve()`，但失败时抛异常。

        用于"这一步必须成功，否则就是 bug 或非法请求"的场景。
        """
        result = cls.resolve(current, event, ctx)
        if result.ok:
            return result

        if not cls.is_valid_state(current) or not cls.allowed_transitions(current):
            raise InvalidTransitionError(
                machine=cls.machine_name,
                current=current,
                event=event,
                allowed=cls.allowed_events(current),
            )

        # 状态允许该事件，但 guard 没过
        raise GuardFailedError(
            machine=cls.machine_name,
            current=current,
            event=event,
            reason=result.rejection_reason or "前置条件不满足",
        )

    @classmethod
    def next_state(cls, current: str, event: str) -> str:
        """按事件推目标状态（不做 guard 判定）。

        仅适用于**该事件只有一条转换**的情况。
        有多条（如按 guard 分支）时会抛 ValueError —— 此时请用 resolve()。

        Raises:
            InvalidTransitionError: 该状态不支持该事件。
            ValueError: 该事件有多条转换，需用 resolve() 提供上下文。
        """
        candidates = tuple(t for t in cls.allowed_transitions(current) if t.event == event)

        if not candidates:
            raise InvalidTransitionError(
                machine=cls.machine_name,
                current=current,
                event=event,
                allowed=cls.allowed_events(current),
            )
        if len(candidates) > 1:
            targets = "、".join(t.target for t in candidates)
            raise ValueError(
                f"{cls.machine_name}: 事件 {event} 在状态 {current} 下有 "
                f"{len(candidates)} 条转换（{targets}），需用 resolve() 提供上下文"
            )
        return candidates[0].target

    # ========================================================
    # 定义自检
    # ========================================================

    @classmethod
    def validate_definition(cls) -> list[str]:
        """校验状态机定义的一致性。

        返回问题列表（空列表 = 定义正确）。
        应作为单元测试的一部分对所有状态机执行 ——
        状态机定义写错是"运行时才暴露、且症状诡异"的典型场景。
        """
        problems: list[str] = []

        if not cls.states:
            problems.append("states 为空")
            return problems

        if cls.initial not in cls.states:
            problems.append(f"initial={cls.initial!r} 不在 states 中")

        for s in cls.terminal:
            if s not in cls.states:
                problems.append(f"terminal 中的 {s!r} 不在 states 中")

        seen: set[tuple[str, str, str]] = set()
        for t in cls.transitions:
            if t.source not in cls.states:
                problems.append(f"转换 {t} 的 source={t.source!r} 不在 states 中")
            if t.target not in cls.states:
                problems.append(f"转换 {t} 的 target={t.target!r} 不在 states 中")
            key = (t.source, t.event, t.target)
            if key in seen:
                problems.append(f"重复转换：{t}")
            seen.add(key)

        # 终态封闭性（S4）
        for s in cls.terminal:
            outgoing = cls._by_source.get(s, ())
            if outgoing:
                events = "、".join(t.event for t in outgoing)
                problems.append(
                    f"终态 {s!r} 存在出边（{events}）—— "
                    "终态必须无出边；若需保留出边，请改用 stable 语义"
                )

        # 不可达状态（除 initial 外，没有任何入边）
        targets = {t.target for t in cls.transitions}
        for s in cls.states:
            if s != cls.initial and s not in targets:
                problems.append(f"状态 {s!r} 不可达（没有任何转换指向它）")

        return problems

    # ========================================================
    # 文档生成
    # ========================================================

    @classmethod
    def to_mermaid(cls) -> str:
        """生成 Mermaid 状态图。

        用途：把状态机图直接贴进设计文档，**保证文档与代码同步**。
        手写状态图必然与代码脱节 —— 这是自动化它的唯一理由。
        """
        lines = ["stateDiagram-v2"]
        lines.append(f"    [*] --> {cls.initial}")

        for t in cls.transitions:
            label = t.event
            if t.guard is not None and t.guard_message:
                # 有 guard 的转换标注条件
                label = f"{t.event} [有条件]"
            lines.append(f"    {t.source} --> {t.target}: {label}")

        for s in sorted(cls.terminal):
            lines.append(f"    {s} --> [*]")

        return "\n".join(lines)

    @classmethod
    def describe(cls) -> str:
        """人类可读的定义摘要（排障与评审用）。"""
        out = [
            f"状态机: {cls.machine_name}",
            f"  状态数: {len(cls.states)}",
            f"  初始态: {cls.initial}",
            f"  终态  : {'、'.join(sorted(cls.terminal)) or '无'}",
            f"  转换数: {len(cls.transitions)}",
            "",
            "  转换明细:",
        ]
        for t in cls.transitions:
            extra = []
            if t.guard is not None:
                extra.append(f"条件={t.guard_message or '自定义'}")
            if t.requires_reason:
                extra.append("需填原因")
            if t.requires_role:
                extra.append(f"角色={'/'.join(t.requires_role)}")
            suffix = f"  ({'; '.join(extra)})" if extra else ""
            out.append(f"    {t.source:>20} --[{t.event}]--> {t.target}{suffix}")
        return "\n".join(out)


# ============================================================
# 通用 Guard 工厂
# ============================================================


def has_role(*roles: str) -> Callable[[Any], bool]:
    """构造"当前用户具备指定角色之一"的 guard。

    约定 ctx 需有 `roles` 属性（frozenset[str] 或 list[str]）。
    """

    def _guard(ctx: Any) -> bool:
        user_roles = getattr(ctx, "roles", None) or ()
        return bool(set(roles) & set(user_roles))

    return _guard


def is_actor(*, actor_attr: str, expected_attr: str) -> Callable[[Any], bool]:
    """构造"操作者等于某字段"的 guard。

    典型用途：职责分离 —— 发起人不能审批自己的单。

    用法：
        Transition(
            "PENDING", "APPROVED", "approve",
            guard=is_actor(actor_attr="actor_id", expected_attr="requested_by")
            # 实际语义取反，见下面 note
        )
    """

    def _guard(ctx: Any) -> bool:
        actor = getattr(ctx, actor_attr, None)
        expected = getattr(ctx, expected_attr, None)
        return actor is not None and expected is not None and actor == expected

    return _guard


@dataclass
class MachineReport:
    """状态机自检报告（批量校验多个状态机时用）。"""

    machine: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems
