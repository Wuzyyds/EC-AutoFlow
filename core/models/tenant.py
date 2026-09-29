"""租户、用户、角色、权限（RBAC）。

权限模型（TDD-06 §2.3）：
    权限点格式 `{资源}:{动作}`，如 `listing:publish`、`credential:view_plaintext`。

三个必须分离的权限（TDD-06 §2.3）：
    1. `credential:view_plaintext` —— 只有 admin，且必须审计
    2. `finance:period_close` vs `finance:period_reopen` —— 关账容易、重开难
    3. `listing:publish` vs `listing:publish_direct` —— 普通用户走审批，管理员可直发

为什么要细粒度权限而不是简单角色：
    "运营"这个角色里，有人能改价有人不能，有人能看利润有人不能。
    粗粒度角色会导致权限要么过大（风险）要么不够用（找 admin 开权限）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.constants import TenantStatus, UserStatus
from core.db import Base
from core.models.mixins import TimestampMixin
from core.models.types import (
    BigIntCol,
    BigIntColNullable,
    BigIntPK,
    BoolCol,
    IntCol,
    IntColNullable,
    TimestampCol,
    TimestampColNullable,
)

__all__ = ["Tenant", "User", "Role", "UserRole", "Permission", "RolePermission"]


class Tenant(Base, TimestampMixin):
    """租户。

    当前是单租户，但字段现在就加（ADR-006）——
    后期加租户字段需要改所有表、所有索引、所有查询，成本极高。
    """

    __tablename__ = "tenants"

    id: Mapped[BigIntPK]
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, comment="租户编码")
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="租户名称")

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TenantStatus.ACTIVE.value,
        server_default=TenantStatus.ACTIVE.value,
        comment="状态：ACTIVE/SUSPENDED/DELETED",
    )

    #: 默认时区（新店铺继承）
    default_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )

    #: 配置上限（防止单租户占满资源）
    max_shops: Mapped[int] = mapped_column(IntCol, nullable=False, default=10, server_default="10")

    __table_args__ = (
        Index("idx_tenants_status", "status"),
        {"comment": "租户"},
    )


class User(Base, TimestampMixin):
    """用户。"""

    __tablename__ = "users"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    username: Mapped[str] = mapped_column(String(128), nullable=False, comment="登录名")
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="显示名")

    #: 邮箱（含 PII，日志中必须脱敏）
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 密码哈希（bcrypt，cost ≥ 12）
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=UserStatus.ACTIVE.value,
        server_default=UserStatus.ACTIVE.value,
    )

    is_superuser: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")

    last_login_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, )
    last_login_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 连续登录失败次数（防爆破）
    failed_login_count: Mapped[int] = mapped_column(IntCol, nullable=False, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(TimestampColNullable, )

    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uk_users_tenant_username"),
        Index("idx_users_status", "tenant_id", "status"),
        {"comment": "用户"},
    )


class Role(Base, TimestampMixin):
    """角色。"""

    __tablename__ = "roles"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    code: Mapped[str] = mapped_column(String(64), nullable=False, comment="角色编码，如 admin/ops/cs")
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: 是否系统内置（内置角色不允许删除）
    is_builtin: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uk_roles_tenant_code"),
        {"comment": "角色"},
    )


class UserRole(Base):
    """用户-角色关联。"""

    __tablename__ = "user_roles"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    role_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    granted_by: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, 
        server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uk_user_roles_user_role"),
        Index("idx_user_roles_user", "user_id"),
        {"comment": "用户角色关联"},
    )


class Permission(Base):
    """权限点。

    格式：`{资源}:{动作}`，如 `listing:publish`、`finance:period_close`。
    """

    __tablename__ = "permissions"

    id: Mapped[BigIntPK]
    code: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, comment="权限点，如 listing:publish"
    )
    resource: Mapped[str] = mapped_column(String(64), nullable=False, comment="资源，如 listing")
    action: Mapped[str] = mapped_column(String(64), nullable=False, comment="动作，如 publish")
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 是否为高危权限（需要额外审计）
    is_sensitive: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")

    __table_args__ = (
        Index("idx_permissions_resource", "resource"),
        {"comment": "权限点"},
    )


class RolePermission(Base):
    """角色-权限关联。"""

    __tablename__ = "role_permissions"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    permission_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampCol, 
        server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uk_role_permissions_role_perm"),
        Index("idx_role_permissions_role", "role_id"),
        {"comment": "角色权限关联"},
    )
