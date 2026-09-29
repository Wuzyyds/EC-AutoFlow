"""服务层测试。

测试重点（TDD-06 §1，服务层覆盖率目标 80%）：

    服务层是"编排"层，最容易出的错不是写错逻辑，
    而是**顺序与边界**错了：

    - 职责分离被绕过（发起人自己批自己的单）
    - 审批事务里做了业务编排（回滚后产生不一致）
    - 关账期间仍能写入
    - P2 告警半夜把人吵醒，最终没人再看告警

    这些都是"跑起来不报错、出事才发现"的问题，只能靠测试拦。

运行：
    python -m pytest tests/integration/test_services.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from core.constants import AlertLevel, ApprovalStatus, PeriodStatus
from core.db import get_sync_session_factory
from core.exceptions import BusinessError, PeriodClosedError
from core.models import AccountingPeriod, Tenant
from core.timeutil import utc_now
from services.base import ServiceContext
from services.finance_service import FinanceService
from services.notification_service import (
    QUIET_HOURS_END,
    QUIET_HOURS_START,
    NotificationService,
)


@pytest.fixture
def session():
    factory = get_sync_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def tenant(session):
    t = Tenant(code="T_SVC", name="服务层测试租户")
    session.add(t)
    session.flush()
    return t


# ============================================================
# 1. 服务上下文
# ============================================================


class TestServiceContext:
    def test_requires_tenant_id(self):
        """上下文必须显式带租户 —— 没有默认值可以偷懒。"""
        with pytest.raises(TypeError):
            ServiceContext()  # type: ignore[call-arg]

    def test_system_actor_detected(self):
        ctx = ServiceContext(tenant_id=1, actor_type="SYSTEM")
        assert ctx.is_system is True
        assert ServiceContext(tenant_id=1).is_system is False

    def test_audit_context_has_no_business_data(self):
        """审计上下文只放标识，不放业务数据。

        业务数据可能含 PII，而审计表是全量留存的 ——
        一旦写进去就删不掉。
        """
        ctx = ServiceContext(tenant_id=1, actor_id=7, trace_id="tr-1", client_ip="10.0.0.1")
        payload = ctx.to_audit_context()
        assert set(payload) == {"actor_id", "actor_type", "trace_id", "client_ip"}
        assert "tenant_id" not in payload


# ============================================================
# 2. 财务：关账校验与利润口径
# ============================================================


class TestFinanceService:
    def test_period_guard(self):
        """关账校验的三种情形：已关账拒绝、未关账放行、期间不存在放行。

        **为什么三个断言合在一个测试里**：
        `get_async_engine()` 是全局单例，绑定在创建它的事件循环上。
        多次 `asyncio.run()` 会让第二次拿到属于已关闭循环的连接池，
        报出的错误与"连不上数据库"极像，很容易误判为配置问题
        （TDD-01 ADR-002 提到的"事件循环冲突"就是这个）。
        """
        import asyncio

        from core.db import async_session_scope, dispose_engines

        async def run() -> dict[str, str]:
            results: dict[str, str] = {}
            async with async_session_scope() as s:
                closed_tenant = Tenant(code="T_FIN_CLOSED", name="已关账租户")
                open_tenant = Tenant(code="T_FIN_OPEN", name="未关账租户")
                s.add_all([closed_tenant, open_tenant])
                await s.flush()

                s.add(
                    AccountingPeriod(
                        tenant_id=closed_tenant.id,
                        period="2026-08",
                        period_start=datetime(2026, 8, 1),
                        period_end=datetime(2026, 8, 31),
                        status=PeriodStatus.CLOSED.value,
                    )
                )
                await s.flush()

                svc_closed = FinanceService(s, ServiceContext(tenant_id=closed_tenant.id))
                try:
                    await svc_closed.assert_period_open("2026-08")
                    results["closed"] = "NO_RAISE"
                except PeriodClosedError as exc:
                    results["closed"] = exc.action or ""

                svc_open = FinanceService(s, ServiceContext(tenant_id=open_tenant.id))
                await svc_open.assert_period_open("2026-09")
                results["open"] = "PASSED"

                # 期间未建立视为未关账 —— 否则首次录入永远进不去
                await svc_open.assert_period_open("2099-01")
                results["missing"] = "PASSED"

                await s.rollback()

            # 在循环还活着时释放连接池 ——
            # 留给 GC 处理会在循环关闭后触发 close，产生
            # "Event loop is closed" 的资源清理异常。
            await dispose_engines()
            return results

        results = asyncio.run(run())
        assert results["closed"] != "NO_RAISE", "关账期间未被拒绝"
        assert "重开" in results["closed"] or "reopen" in results["closed"].lower()
        assert results["open"] == "PASSED"
        assert results["missing"] == "PASSED"

    def test_profit_layering(self):
        """贡献利润分层计算正确。

        履约后 = 收入 − 成本 − 平台费 − 运费 − 退款
        广告后 = 履约后 − 广告费
        """
        b = FinanceService.build_breakdown(
            revenue=Decimal("1000"),
            cogs=Decimal("400"),
            platform_fees=Decimal("150"),
            shipping=Decimal("50"),
            ad_cost=Decimal("100"),
            refunds=Decimal("30"),
        )
        assert b.gross_profit == Decimal("370.000000")
        assert b.contribution_profit == Decimal("270.000000")
        assert b.gross_margin == Decimal("37.000000")

    def test_zero_revenue_margin_no_division_error(self):
        """零收入不能抛异常。

        促销期间的零金额订单是正常现象，
        裸除会抛 InvalidOperation 让整个报表生成失败。
        """
        b = FinanceService.build_breakdown(
            revenue=Decimal("0"),
            cogs=Decimal("0"),
            platform_fees=Decimal("0"),
            shipping=Decimal("0"),
            ad_cost=Decimal("0"),
            refunds=Decimal("0"),
        )
        assert b.gross_margin == Decimal("0")

    def test_total_revenue_no_rounding_drift(self):
        """汇总不应因逐项舍入产生尾差。"""
        amounts = [Decimal("0.0000001")] * 10
        total = FinanceService.total_revenue(amounts)
        assert total == Decimal("0.000001")


# ============================================================
# 3. 通知：分级与静默
# ============================================================


class TestNotificationService:
    def test_p0_never_suppressed(self):
        """P0 任何时候都推 —— 这是"不静默"的定义。"""
        night = datetime(2026, 9, 29, 23, 30)
        assert NotificationService.should_push(AlertLevel.P0.value, moment=night) is True

    def test_p1_suppressed_at_night(self):
        """P1 夜间静默，白天推。"""
        night = datetime(2026, 9, 29, 23, 30)
        day = datetime(2026, 9, 29, 14, 0)
        assert NotificationService.should_push(AlertLevel.P1.value, moment=night) is False
        assert NotificationService.should_push(AlertLevel.P1.value, moment=day) is True

    def test_p2_never_pushed_directly(self):
        """P2 只进日报。

        如果 P2 半夜也推，团队会关掉通知 —— 那时 P0 也收不到了。
        """
        day = datetime(2026, 9, 29, 14, 0)
        assert NotificationService.should_push(AlertLevel.P2.value, moment=day) is False

    def test_quiet_hours_crosses_midnight(self):
        """静默窗口跨零点，不能写成 start <= t <= end（那样永远为假）。"""
        assert NotificationService.is_quiet_hours(datetime(2026, 9, 29, 23, 0)) is True
        assert NotificationService.is_quiet_hours(datetime(2026, 9, 29, 3, 0)) is True
        assert NotificationService.is_quiet_hours(datetime(2026, 9, 29, 12, 0)) is False
        assert QUIET_HOURS_START.hour == 22
        assert QUIET_HOURS_END.hour == 8

    def test_no_channel_reports_gracefully(self):
        """未配置通道时返回未送达，不抛异常。

        通知失败不应该让业务事务回滚。
        """
        import asyncio

        svc = NotificationService(channel=None)
        result = asyncio.run(
            svc.dispatch(
                alert_id=1,
                level=AlertLevel.P0.value,
                title="测试",
                content="内容",
                moment=datetime(2026, 9, 29, 14, 0),
            )
        )
        assert result.delivered is False
        assert result.reason == "NO_CHANNEL"

    def test_channel_exception_swallowed(self):
        """通道抛异常也必须被吞掉，只返回 FAILED。"""

        class BoomChannel:
            async def send(self, *, title: str, content: str, level: str) -> bool:
                raise RuntimeError("网络不可达")

        import asyncio

        svc = NotificationService(channel=BoomChannel())
        result = asyncio.run(
            svc.dispatch(
                alert_id=2,
                level=AlertLevel.P0.value,
                title="t",
                content="c",
                moment=datetime(2026, 9, 29, 14, 0),
            )
        )
        assert result.delivered is False
        assert result.reason == "FAILED"


# ============================================================
# 4. 审批状态机接入（纯逻辑，不落库）
# ============================================================


class TestApprovalStateMachineWiring:
    """审批状态机的接入语义。

    这里直接测状态机 + 上下文，不经过数据库 ——
    审批的核心风险是**规则被绕过**，而不是存储写错。
    """

    def test_requester_cannot_approve_own_request(self):
        """职责分离：发起人不得审批自己的单。"""
        from domain.state_machines import ApprovalContext, ApprovalStateMachine

        ctx = ApprovalContext(roles=frozenset({"admin"}), actor_id=7, requested_by=7)
        result = ApprovalStateMachine.resolve(ApprovalStatus.PENDING.value, "approve", ctx)
        assert result.changed is False
        assert "发起人" in (result.rejection_reason or "")

    def test_other_person_can_approve(self):
        from domain.state_machines import ApprovalContext, ApprovalStateMachine

        ctx = ApprovalContext(roles=frozenset({"approver"}), actor_id=8, requested_by=7)
        result = ApprovalStateMachine.resolve(ApprovalStatus.PENDING.value, "approve", ctx)
        assert result.changed is True
        assert result.to_state == ApprovalStatus.APPROVED.value

    def test_approved_and_executed_are_separate(self):
        """APPROVED ≠ EXECUTED。

        合并这两态会丢失"审批通过但执行失败"的追踪，
        这类单据会永远沉默，直到有人发现商品没上架。
        """
        from domain.state_machines import ApprovalContext, ApprovalStateMachine

        ctx = ApprovalContext(roles=frozenset({"approver"}), actor_id=8, requested_by=7)
        approved = ApprovalStateMachine.resolve(ApprovalStatus.PENDING.value, "approve", ctx)
        assert approved.to_state == ApprovalStatus.APPROVED.value
        assert approved.to_state != ApprovalStatus.EXECUTED.value

        executed = ApprovalStateMachine.resolve(approved.to_state, "execute_success", ctx)
        assert executed.to_state == ApprovalStatus.EXECUTED.value

    def test_retry_has_upper_bound(self):
        """执行失败可重试，但必须有上限。

        无限重试会掩盖真问题 —— 每次都"再试一次"，
        永远没人去看为什么一直失败。
        """
        from domain.state_machines import ApprovalContext, ApprovalStateMachine

        over = ApprovalContext(actor_id=8, retry_count=99, max_retries=3)
        result = ApprovalStateMachine.resolve(ApprovalStatus.FAILED.value, "retry_execute", over)
        assert result.changed is False

        ok = ApprovalContext(actor_id=8, retry_count=1, max_retries=3)
        result_ok = ApprovalStateMachine.resolve(ApprovalStatus.FAILED.value, "retry_execute", ok)
        assert result_ok.changed is True

    def test_expired_is_terminal(self):
        """EXPIRED 是终态，不能直接转回 PENDING。

        审批的价值在于"对人的决策留痕" ——
        超时后重新提交必须重新走决策，否则审批人可能基于过时信息判断。
        """
        from domain.state_machines import ApprovalStateMachine

        assert ApprovalStateMachine.is_terminal(ApprovalStatus.EXPIRED.value) is True
        assert (
            ApprovalStateMachine.resolve(ApprovalStatus.EXPIRED.value, "submit", None).changed
            is False
        )


# ============================================================
# 5. 上架状态机接入
# ============================================================


class TestListingStateMachineWiring:
    def test_partial_active_is_its_own_state(self):
        """部分成功必须是独立状态。

        归为 ACTIVE 会漏监控未上架的变体；
        归为 FAILED 会让重试覆盖掉已经成功的变体。
        """
        from core.constants import ListingStatus
        from domain.state_machines import ListingContext, ListingStateMachine

        ctx = ListingContext(roles=frozenset({"ops"}), variant_total=3, variant_succeeded=1)
        result = ListingStateMachine.resolve(
            ListingStatus.PROCESSING.value, "partially_succeeded", ctx
        )
        assert result.changed is True
        assert result.to_state == ListingStatus.PARTIAL_ACTIVE.value
        assert result.to_state != ListingStatus.ACTIVE.value
        assert result.to_state != ListingStatus.FAILED.value

    def test_phase1_stops_at_pending_approval(self):
        """Phase 1 上架流程终止在待审批（不自动提交平台）。

        授权未到位时自动提交只会产生"以为上架了、其实没有"的错觉。
        """
        from core.constants import ListingStatus
        from domain.state_machines import ListingContext, ListingStateMachine

        ctx = ListingContext(roles=frozenset({"ops"}), requires_approval=True)
        result = ListingStateMachine.resolve(
            ListingStatus.VALIDATING.value, "validation_passed", ctx
        )
        assert result.to_state == ListingStatus.PENDING_APPROVAL.value
