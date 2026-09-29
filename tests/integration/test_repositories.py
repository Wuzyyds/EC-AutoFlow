"""仓储层测试。

测试策略（TDD-06 §1）：

    用**真实 MySQL + 事务回滚**，不用 SQLite ——
    本项目依赖 MySQL 特有能力（生成列、JSON、CHECK 降级触发器），
    SQLite 上跑不出真实行为，测了等于没测。

**测试重点不是 CRUD，而是隔离与过滤**：

    CRUD 写错了会立刻抛异常，很容易发现；
    租户过滤漏写**不会报错** —— 它只是安静地返回别人的数据。
    这类问题只有测试能拦住，所以这里花了最多篇幅。

运行：
    python -m pytest tests/integration/test_repositories.py -v
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

import pytest

from core.constants import EventStatus
from core.db import get_sync_session_factory
from core.exceptions import ConfigurationError
from core.models import DomainEvent, Role, Shop, Tenant, User
from core.timeutil import utc_now
from repositories.base import MAX_LIMIT, BaseRepository, SyncBaseRepository
from repositories.infra import SyncSyncCursorRepository
from repositories.order import OrderRepository
from repositories.shop import ShopRepository, SyncShopRepository
from repositories.tenant import RoleRepository, SyncRoleRepository, UserRepository


@pytest.fixture
def session():
    """同步会话；测试结束整体回滚，不污染数据库。"""
    factory = get_sync_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def two_tenants(session):
    """创建两个租户，返回 (tenant_a, tenant_b)。"""
    a = Tenant(code="T_TEST_A", name="测试租户A")
    b = Tenant(code="T_TEST_B", name="测试租户B")
    session.add_all([a, b])
    session.flush()
    return a, b


# ============================================================
# 1. 租户作用域（不需要数据库）
# ============================================================


class TestTenantScope:
    """租户作用域强制校验。"""

    def test_tenant_scoped_model_requires_tenant_id(self):
        """含 tenant_id 的模型不传租户必须报错。

        这是防"漏写租户过滤"的第一道闸门 ——
        如果只是警告后放行，等于没有隔离。
        """
        with pytest.raises(ConfigurationError) as exc:
            UserRepository(None)
        assert exc.value.code == "TENANT_SCOPE_REQUIRED"

    def test_global_table_needs_no_tenant(self):
        """全局字典表（无 tenant_id）不应要求租户。"""
        from repositories.tenant import PermissionRepository, TenantRepository

        TenantRepository(None)
        PermissionRepository(None)
        assert TenantRepository._is_tenant_scoped() is False

    def test_cross_tenant_requires_explicit_flag(self):
        """跨租户查询必须显式声明，不能是默认行为。"""
        UserRepository(None, allow_cross_tenant=True)

    def test_fact_table_has_no_soft_delete(self):
        """事实表（订单）不应被识别为支持软删除。

        误加 `deleted_at IS NULL` 会让历史记录凭空消失。
        """
        from core.models import Order

        class OrderRepo(SyncBaseRepository):
            model = Order

        assert OrderRepo._supports_soft_delete() is False

    def test_soft_delete_table_detected(self):
        """配置类实体（店铺）应被识别为支持软删除。"""

        class ShopRepo(SyncBaseRepository):
            model = Shop

        assert ShopRepo._supports_soft_delete() is True


# ============================================================
# 2. 参数校验（不需要数据库）
# ============================================================


class TestValidation:
    """过滤字段、排序字段、分页参数校验。"""

    def test_unknown_filter_column_rejected(self):
        """拼错字段名必须报错，而不是静默忽略条件。

        静默忽略会让"按 shop_id 过滤"变成"返回全部店铺"。
        """
        repo = UserRepository(None, tenant_id=1)
        with pytest.raises(ConfigurationError) as exc:
            repo._validate_filters({"not_a_column": 1})
        assert exc.value.code == "UNKNOWN_FILTER_COLUMN"

    def test_unknown_order_column_rejected(self):
        repo = UserRepository(None, tenant_id=1)
        from sqlalchemy import select

        with pytest.raises(ConfigurationError) as exc:
            repo._apply_ordering(select(User), "bogus_field")
        assert exc.value.code == "UNKNOWN_ORDER_COLUMN"

    def test_limit_upper_bound_enforced(self):
        """超上限的分页请求必须拒绝，防止全表扫描。"""
        with pytest.raises(ConfigurationError) as exc:
            SyncBaseRepository._normalize_pagination(MAX_LIMIT + 1, 0)
        assert exc.value.code == "LIMIT_EXCEEDED"

    def test_negative_offset_rejected(self):
        with pytest.raises(ConfigurationError) as exc:
            SyncBaseRepository._normalize_pagination(10, -1)
        assert exc.value.code == "INVALID_PAGINATION"

    def test_unknown_update_column_rejected(self):
        repo = UserRepository(None, tenant_id=1)
        with pytest.raises(ConfigurationError) as exc:
            repo._validate_update_columns({"nope": 1})
        assert exc.value.code == "UNKNOWN_UPDATE_COLUMN"


# ============================================================
# 3. 租户隔离（真实数据库）
# ============================================================


class TestTenantIsolation:
    """租户隔离的端到端验证 —— 本文件最重要的部分。"""

    def test_list_only_returns_own_tenant(self, session, two_tenants):
        tenant_a, tenant_b = two_tenants
        session.add_all(
            [
                Role(tenant_id=tenant_a.id, code="ops", name="运营A"),
                Role(tenant_id=tenant_b.id, code="ops", name="运营B"),
            ]
        )
        session.flush()

        repo_a = SyncRoleRepository(session, tenant_id=tenant_a.id)
        rows = repo_a.list()

        assert len(rows) == 1
        assert all(r.tenant_id == tenant_a.id for r in rows)

    def test_count_is_isolated(self, session, two_tenants):
        tenant_a, tenant_b = two_tenants
        session.add_all(
            [
                Role(tenant_id=tenant_a.id, code="ops", name="A"),
                Role(tenant_id=tenant_b.id, code="ops", name="B"),
            ]
        )
        session.flush()

        assert SyncRoleRepository(session, tenant_id=tenant_a.id).count() == 1
        assert SyncRoleRepository(session, tenant_id=tenant_b.id).count() == 1

    def test_cannot_read_other_tenant_by_id(self, session, two_tenants):
        """拿到别人的主键也读不出来 —— 这是越权的最后一道防线。"""
        tenant_a, tenant_b = two_tenants
        other = Role(tenant_id=tenant_b.id, code="cs", name="客服B")
        session.add(other)
        session.flush()

        repo_a = SyncRoleRepository(session, tenant_id=tenant_a.id)
        assert repo_a.get(other.id) is None

    def test_get_by_filters_across_tenant(self, session, two_tenants):
        """按业务字段查也不能穿透租户。"""
        tenant_a, tenant_b = two_tenants
        session.add(Role(tenant_id=tenant_b.id, code="unique_code", name="B"))
        session.flush()

        repo_a = SyncRoleRepository(session, tenant_id=tenant_a.id)
        assert repo_a.get_by_code("unique_code") is None

    def test_tenant_id_auto_filled_on_add(self, session, two_tenants):
        """新增时自动补租户，避免忘记赋值导致 NOT NULL 报错。"""
        tenant_a, _ = two_tenants
        repo_a = SyncRoleRepository(session, tenant_id=tenant_a.id)
        role = Role(code="auto", name="自动补租户")
        repo_a.add(role)
        assert role.tenant_id == tenant_a.id

    def test_mismatched_tenant_rejected(self, session, two_tenants):
        """把 B 租户的对象塞进 A 的仓储必须被拒绝。"""
        tenant_a, tenant_b = two_tenants
        repo_a = SyncRoleRepository(session, tenant_id=tenant_a.id)
        with pytest.raises(ConfigurationError) as exc:
            repo_a.add(Role(tenant_id=tenant_b.id, code="x", name="x"))
        assert exc.value.code == "TENANT_MISMATCH"


# ============================================================
# 4. 软删除过滤（真实数据库）
# ============================================================


class TestSoftDelete:
    """软删除过滤。"""

    def test_soft_deleted_hidden_by_default(self, session, two_tenants):
        tenant_a, _ = two_tenants
        repo = SyncShopRepository(session, tenant_id=tenant_a.id)
        shop = Shop(tenant_id=tenant_a.id, platform="AMAZON", name="待删店铺", currency="USD")
        repo.add(shop)

        assert repo.count() == 1
        repo.soft_delete(shop)
        assert repo.count() == 0

    def test_include_deleted_flag_exposes_them(self, session, two_tenants):
        tenant_a, _ = two_tenants
        repo = SyncShopRepository(session, tenant_id=tenant_a.id)
        shop = Shop(tenant_id=tenant_a.id, platform="AMAZON", name="已删店铺", currency="USD")
        repo.add(shop)
        repo.soft_delete(shop)

        repo_all = SyncShopRepository(session, tenant_id=tenant_a.id, include_deleted=True)
        assert repo_all.count() >= 1

    def test_soft_delete_unsupported_table_raises(self, session, two_tenants):
        """事实表（角色）不支持软删除，调用应报错而不是静默成功。"""
        tenant_a, _ = two_tenants
        repo = SyncRoleRepository(session, tenant_id=tenant_a.id)
        role = Role(code="no_soft", name="不支持软删")
        repo.add(role)
        with pytest.raises(ConfigurationError) as exc:
            repo.soft_delete(role)
        assert exc.value.code == "SOFT_DELETE_UNSUPPORTED"


# ============================================================
# 5. Outbox 事件（真实数据库）
# ============================================================


class TestDomainEventOutbox:
    """领域事件消费逻辑。"""

    def test_pending_excludes_future_and_exhausted(self, session, two_tenants):
        """待处理事件必须排除"退避未到期"和"重试超限"两类。

        少了 `available_at` 过滤，退避形同虚设；
        少了 `attempts` 过滤，坏事件会被无限重试刷爆日志。
        """
        from repositories.approval import SyncDomainEventRepository

        tenant_a, _ = two_tenants
        repo = SyncDomainEventRepository(session, tenant_id=tenant_a.id)

        ready = DomainEvent(
            tenant_id=tenant_a.id,
            event_type="approval.approved",
            aggregate_type="Approval",
            aggregate_id=1,
            payload={},
            status=EventStatus.PENDING.value,
            available_at=utc_now() - timedelta(seconds=1),
        )
        future = DomainEvent(
            tenant_id=tenant_a.id,
            event_type="approval.approved",
            aggregate_type="Approval",
            aggregate_id=2,
            payload={},
            status=EventStatus.PENDING.value,
            available_at=utc_now() + timedelta(hours=1),
        )
        exhausted = DomainEvent(
            tenant_id=tenant_a.id,
            event_type="approval.approved",
            aggregate_type="Approval",
            aggregate_id=3,
            payload={},
            status=EventStatus.FAILED.value,
            attempts=99,
            available_at=utc_now() - timedelta(seconds=1),
        )
        session.add_all([ready, future, exhausted])
        session.flush()

        ids = {e.id for e in repo.list_pending(limit=100)}
        assert ready.id in ids
        assert future.id not in ids
        assert exhausted.id not in ids

    def test_mark_done_sets_status_and_time(self, session, two_tenants):
        from repositories.approval import SyncDomainEventRepository

        tenant_a, _ = two_tenants
        repo = SyncDomainEventRepository(session, tenant_id=tenant_a.id)
        event = DomainEvent(
            tenant_id=tenant_a.id,
            event_type="listing.submitted",
            aggregate_type="ProductListing",
            aggregate_id=9,
            payload={},
            status=EventStatus.PENDING.value,
            available_at=utc_now(),
        )
        session.add(event)
        session.flush()

        repo.mark_done(event.id)
        assert event.status == EventStatus.DONE.value
        assert event.processed_at is not None

    def test_mark_failed_sets_backoff(self, session, two_tenants):
        """失败必须写退避时间，否则会被立刻重试打爆。"""
        from repositories.approval import SyncDomainEventRepository

        tenant_a, _ = two_tenants
        repo = SyncDomainEventRepository(session, tenant_id=tenant_a.id)
        event = DomainEvent(
            tenant_id=tenant_a.id,
            event_type="listing.submitted",
            aggregate_type="ProductListing",
            aggregate_id=10,
            payload={},
            status=EventStatus.PENDING.value,
            available_at=utc_now(),
        )
        session.add(event)
        session.flush()

        before = utc_now()
        repo.mark_failed(event.id, "平台超时", retry_delay_seconds=120)
        assert event.status == EventStatus.FAILED.value
        assert event.attempts == 1
        assert event.available_at > before + timedelta(seconds=100)


# ============================================================
# 6. 同步游标（真实数据库）
# ============================================================


class TestSyncCursor:
    """同步游标前移与自动暂停。"""

    def test_advance_creates_then_moves(self, session, two_tenants):
        tenant_a, _ = two_tenants
        repo = SyncSyncCursorRepository(session, tenant_id=tenant_a.id)

        cursor = repo.advance(shop_id=1001, resource="ORDERS", cursor_value="2026-09-01T00:00:00Z")
        assert cursor.cursor_value == "2026-09-01T00:00:00Z"

        moved = repo.advance(shop_id=1001, resource="ORDERS", cursor_value="2026-09-02T00:00:00Z")
        assert moved.cursor_value == "2026-09-02T00:00:00Z"

    def test_consecutive_failures_auto_pause(self, session, two_tenants):
        """连续失败达阈值自动暂停。

        Token 失效这类问题重试不会成功，无限重试只会让团队
        关掉告警通知 —— 那时真正的 P0 也收不到了。
        """
        from core.constants import SyncStatus

        tenant_a, _ = two_tenants
        repo = SyncSyncCursorRepository(session, tenant_id=tenant_a.id)

        for _ in range(5):
            cursor = repo.record_failure(1002, "INVENTORY", "401 Unauthorized")

        assert cursor.consecutive_failures == 5
        assert cursor.status == SyncStatus.PAUSED.value
        assert 1002 in {c.shop_id for c in repo.list_paused()}

    def test_success_resets_failure_counter(self, session, two_tenants):
        tenant_a, _ = two_tenants
        repo = SyncSyncCursorRepository(session, tenant_id=tenant_a.id)
        repo.record_failure(1003, "LISTINGS", "timeout")
        cursor = repo.advance(1003, "LISTINGS", "2026-09-03T00:00:00Z")
        assert cursor.consecutive_failures == 0


# ============================================================
# 7. 异步接口（与同步版本行为一致性）
# ============================================================


class TestAsyncParity:
    """异步仓储行为验证。

    async 与 sync 共用查询构建逻辑，这里验证异步侧
    的过滤语义与同步侧一致 —— 否则 worker 能查到 API 查不到的数据。
    """

    def test_async_tenant_isolation(self):
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        async def run() -> tuple[int, int]:
            from core.db import async_session_scope
            from core.models import Role as R

            async with async_session_scope() as s:
                t1 = Tenant(code="T_ASYNC_1", name="异步租户1")
                t2 = Tenant(code="T_ASYNC_2", name="异步租户2")
                s.add_all([t1, t2])
                await s.flush()
                s.add_all(
                    [
                        R(tenant_id=t1.id, code="a1", name="A1"),
                        R(tenant_id=t2.id, code="a2", name="A2"),
                    ]
                )
                await s.flush()

                repo1 = RoleRepository(s, tenant_id=t1.id)
                repo2 = RoleRepository(s, tenant_id=t2.id)
                counts = (await repo1.count(), await repo2.count())
                await s.rollback()
                return counts

        c1, c2 = asyncio.run(run())
        assert c1 == 1
        assert c2 == 1

    def test_async_requires_tenant(self):
        with pytest.raises(ConfigurationError):
            OrderRepository(None)
