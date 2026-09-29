"""租户、用户、角色、权限仓储（RBAC）。

权限模型见 TDD-06 §2.3：权限点格式 `{资源}:{动作}`，如 `listing:publish`。

三个必须分离的高危权限：

1. `credential:view_plaintext` —— 只有 admin，且每次访问必须留审计
2. `finance:period_close` vs `finance:period_reopen` —— 关账容易、重开难
3. `listing:publish` vs `listing:publish_direct` —— 普通用户走审批，管理员可直发

**性能约定**：鉴权中间件每次请求都要取用户权限，
必须**一次查询取全**（见 `list_permission_codes`）。
"先查角色、再逐个查权限"是 N+1，会随角色数线性放大 ——
这是登录后每个请求都要付的成本。

实现方式：查询构建方法集中在 `_TenantQueries`，async 与 sync 仓储共用，
避免两套实现行为漂移（TDD-01 ADR-002）。
"""

from __future__ import annotations

from typing import Any, Generic

from sqlalchemy import Select, select

from core.constants import TenantStatus, UserStatus
from core.models import (
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)
from repositories.base import BaseRepository, SyncBaseRepository

__all__ = [
    "PermissionRepository",
    "RolePermissionRepository",
    "RoleRepository",
    "SyncPermissionRepository",
    "SyncRolePermissionRepository",
    "SyncRoleRepository",
    "SyncTenantRepository",
    "SyncUserRepository",
    "SyncUserRoleRepository",
    "TenantRepository",
    "UserRepository",
    "UserRoleRepository",
]


class _TenantQueries:
    """查询构建（async / sync 共用）。

    只返回 `Select`，不执行 —— 执行由各自的仓储完成。
    方法依赖宿主类的 `_stmt()`（由 `_QueryMixin` 提供）。
    """

    # ---------- 租户 ----------

    def _q_tenant_by_code(self, code: str) -> Select:
        return self._stmt().where(Tenant.code == code)

    def _q_tenants_by_status(self, status: str) -> Select:
        return self._stmt().where(Tenant.status == status)

    # ---------- 用户 ----------

    def _q_user_by_username(self, username: str) -> Select:
        """按登录名查用户。

        租户过滤由 `_stmt()` 注入，因此同名用户在租户间互不可见。
        """
        return self._stmt().where(User.username == username)

    def _q_users_by_status(self, status: str) -> Select:
        return self._stmt().where(User.status == status)

    def _q_users_locked(self) -> Select:
        """处于锁定状态的用户（`locked_until` 未过期）。"""
        from core.timeutil import utc_now

        return self._stmt().where(User.locked_until.is_not(None)).where(
            User.locked_until > utc_now()
        )

    # ---------- 角色 ----------

    def _q_role_by_code(self, code: str) -> Select:
        return self._stmt().where(Role.code == code)

    # ---------- 权限 ----------

    def _q_permission_by_code(self, code: str) -> Select:
        """按权限点查。`permissions` 表无 tenant_id（全局字典）。"""
        return self._stmt().where(Permission.code == code)

    def _q_permissions_sensitive(self) -> Select:
        """高危权限点（需额外审计）。"""
        return self._stmt().where(Permission.is_sensitive.is_(True))

    # ---------- 关联表 ----------

    def _q_user_role_ids(self, user_id: int) -> Select:
        return self._stmt().where(UserRole.user_id == user_id)

    def _q_role_permission_ids(self, role_id: int) -> Select:
        return self._stmt().where(RolePermission.role_id == role_id)


class TenantRepository(BaseRepository[Tenant], _TenantQueries):
    """租户仓储。

    `tenants` 表**没有** tenant_id（它本身就是租户），
    因此构造时不需要传租户 ID —— `_QueryMixin` 会自动识别并跳过校验。
    """

    model = Tenant

    async def get_by_code(self, code: str) -> Tenant | None:
        return (await self.session.execute(self._q_tenant_by_code(code))).scalars().first()

    async def list_active(self) -> list[Tenant]:
        result = await self.session.execute(self._q_tenants_by_status(TenantStatus.ACTIVE.value))
        return list(result.scalars().all())


class UserRepository(BaseRepository[User], _TenantQueries):
    """用户仓储。"""

    model = User

    async def get_by_username(self, username: str) -> User | None:
        """按登录名查用户（租户内唯一）。"""
        return (await self.session.execute(self._q_user_by_username(username))).scalars().first()

    async def list_active(self, *, limit: int | None = None) -> list[User]:
        result = await self.session.execute(
            self._q_users_by_status(UserStatus.ACTIVE.value).limit(
                self._normalize_pagination(limit, 0)[0]
            )
        )
        return list(result.scalars().all())

    async def list_role_ids(self, user_id: int) -> list[int]:
        """用户拥有的角色 ID 列表。"""
        result = await self.session.execute(self._q_user_role_ids(user_id))
        return [row.role_id for row in result.scalars().all()]

    async def list_permission_codes(self, user_id: int) -> set[str]:
        """聚合用户的全部权限点。

        **一次查询取全**（三个表 join），而不是"先查角色再逐个查权限"。
        鉴权是每个请求都要走的热路径，N+1 在这里代价最高。

        注意 `permissions` 表没有 tenant_id，
        但 `user_roles` / `role_permissions` 有 —— 过滤加在关联表上。
        """
        stmt = (
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
        )
        if self.tenant_id is not None:
            stmt = stmt.where(UserRole.tenant_id == self.tenant_id)
            stmt = stmt.where(RolePermission.tenant_id == self.tenant_id)

        result = await self.session.execute(stmt)
        return set(result.scalars().all())

    async def list_sensitive_permission_codes(self, user_id: int) -> set[str]:
        """用户持有的**高危**权限点。

        登录时用于决定是否需要强制二次验证，
        以及前端是否显示危险操作入口。
        """
        stmt = (
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
            .where(Permission.is_sensitive.is_(True))
        )
        if self.tenant_id is not None:
            stmt = stmt.where(UserRole.tenant_id == self.tenant_id)

        result = await self.session.execute(stmt)
        return set(result.scalars().all())


class RoleRepository(BaseRepository[Role], _TenantQueries):
    """角色仓储。"""

    model = Role

    async def get_by_code(self, code: str) -> Role | None:
        return (await self.session.execute(self._q_role_by_code(code))).scalars().first()

    async def list_builtin(self) -> list[Role]:
        """系统内置角色（不允许删除）。"""
        result = await self.session.execute(self._stmt().where(Role.is_builtin.is_(True)))
        return list(result.scalars().all())


class PermissionRepository(BaseRepository[Permission], _TenantQueries):
    """权限点仓储。

    `permissions` 是**全局字典表**，无 tenant_id，构造不需要租户 ID。
    """

    model = Permission

    async def get_by_code(self, code: str) -> Permission | None:
        return (await self.session.execute(self._q_permission_by_code(code))).scalars().first()

    async def list_sensitive(self) -> list[Permission]:
        result = await self.session.execute(self._q_permissions_sensitive())
        return list(result.scalars().all())


class UserRoleRepository(BaseRepository[UserRole], _TenantQueries):
    """用户-角色关联仓储。"""

    model = UserRole

    async def list_by_user(self, user_id: int) -> list[UserRole]:
        result = await self.session.execute(self._q_user_role_ids(user_id))
        return list(result.scalars().all())

    async def revoke(self, user_id: int, role_id: int) -> int:
        """解除用户与角色的关联，返回删除条数。"""
        rows = await self.list(user_id=user_id, role_id=role_id)
        for row in rows:
            await self.delete(row)
        return len(rows)


class RolePermissionRepository(BaseRepository[RolePermission], _TenantQueries):
    """角色-权限关联仓储。"""

    model = RolePermission

    async def list_by_role(self, role_id: int) -> list[RolePermission]:
        result = await self.session.execute(self._q_role_permission_ids(role_id))
        return list(result.scalars().all())

    async def list_permission_ids(self, role_id: int) -> list[int]:
        return [row.permission_id for row in await self.list_by_role(role_id)]


# ============================================================
# 同步版本（Celery worker 用）
# ============================================================
#
# 与异步版本共用 `_TenantQueries` 的查询构建，只有"执行"部分不同。
# 这样两套接口的过滤语义不会漂移 —— 否则 worker 里能查到
# API 里查不到的数据，是最难定位的一类问题。


class SyncTenantRepository(SyncBaseRepository[Tenant], _TenantQueries):
    model = Tenant

    def get_by_code(self, code: str) -> Tenant | None:
        return self.session.execute(self._q_tenant_by_code(code)).scalars().first()

    def list_active(self) -> list[Tenant]:
        return list(
            self.session.execute(self._q_tenants_by_status(TenantStatus.ACTIVE.value))
            .scalars()
            .all()
        )


class SyncUserRepository(SyncBaseRepository[User], _TenantQueries):
    model = User

    def get_by_username(self, username: str) -> User | None:
        return self.session.execute(self._q_user_by_username(username)).scalars().first()

    def list_active(self, *, limit: int | None = None) -> list[User]:
        stmt = self._q_users_by_status(UserStatus.ACTIVE.value).limit(
            self._normalize_pagination(limit, 0)[0]
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_role_ids(self, user_id: int) -> list[int]:
        result = self.session.execute(self._q_user_role_ids(user_id))
        return [row.role_id for row in result.scalars().all()]

    def list_permission_codes(self, user_id: int) -> set[str]:
        stmt = (
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
        )
        if self.tenant_id is not None:
            stmt = stmt.where(UserRole.tenant_id == self.tenant_id)
            stmt = stmt.where(RolePermission.tenant_id == self.tenant_id)
        return set(self.session.execute(stmt).scalars().all())


class SyncRoleRepository(SyncBaseRepository[Role], _TenantQueries):
    model = Role

    def get_by_code(self, code: str) -> Role | None:
        return self.session.execute(self._q_role_by_code(code)).scalars().first()

    def list_builtin(self) -> list[Role]:
        return list(
            self.session.execute(self._stmt().where(Role.is_builtin.is_(True))).scalars().all()
        )


class SyncPermissionRepository(SyncBaseRepository[Permission], _TenantQueries):
    model = Permission

    def get_by_code(self, code: str) -> Permission | None:
        return self.session.execute(self._q_permission_by_code(code)).scalars().first()

    def list_sensitive(self) -> list[Permission]:
        return list(self.session.execute(self._q_permissions_sensitive()).scalars().all())


class SyncUserRoleRepository(SyncBaseRepository[UserRole], _TenantQueries):
    model = UserRole

    def list_by_user(self, user_id: int) -> list[UserRole]:
        return list(self.session.execute(self._q_user_role_ids(user_id)).scalars().all())


class SyncRolePermissionRepository(SyncBaseRepository[RolePermission], _TenantQueries):
    model = RolePermission

    def list_by_role(self, role_id: int) -> list[RolePermission]:
        return list(self.session.execute(self._q_role_permission_ids(role_id)).scalars().all())

    def list_permission_ids(self, role_id: int) -> list[int]:
        return [row.permission_id for row in self.list_by_role(role_id)]
