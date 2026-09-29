"""通用 Repository 基类：CRUD、租户隔离、软删除。

本模块是整个数据访问层的地基，三条硬约束在这里落地。

1. **租户过滤统一注入**（TDD-01 ADR-006）
   业务代码不得手写 `where tenant_id = ...` ——
   漏写一处就是跨租户数据越权，而且**不会有任何报错**。

   强制手段：模型若含 `tenant_id` 列，构造仓储时**必须**传入租户 ID，
   否则抛 `ConfigurationError`。让错误在调用点就暴露，
   而不是等数据泄漏三个月后才发现。

2. **async / sync 双套接口**（TDD-01 ADR-002）
   FastAPI 接口层用 `BaseRepository`（AsyncSession），
   Celery worker 用 `SyncBaseRepository`（Session）。
   查询构建逻辑集中在 `_QueryMixin`，两套共用 —— 避免两套实现行为漂移。

3. **软删除自动识别**
   只有含 `deleted_at` 的表才加过滤条件。
   订单、结算、审计等事实表没有该列，本模块不会误加条件
   （误加会让历史记录凭空消失，这是最难排查的一类 bug）。

使用示例：

    class OrderRepository(BaseRepository[Order]):
        model = Order

    async with async_session_scope() as session:
        repo = OrderRepository(session, tenant_id=1)
        orders = await repo.list(shop_id=5, limit=50, order_by="-created_at")
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from core.db import Base
from core.exceptions import ConfigurationError, NotFoundError
from core.timeutil import utc_now

__all__ = [
    "MAX_LIMIT",
    "BaseRepository",
    "SyncBaseRepository",
]

ModelT = TypeVar("ModelT", bound=Base)

#: 单次查询返回行数上限。
#:
#: 存在的意义不是"性能优化"，而是**防呆**：
#: 忘记写 limit 的全表查询在数据量上来后会拖垮数据库，
#: 而这类问题通常在最不方便的时间暴露。宁可让调用方显式分页。
MAX_LIMIT = 1000


class _QueryMixin(Generic[ModelT]):
    """查询构建逻辑（async 与 sync 共用）。

    只负责"拼查询"，不负责"执行" ——
    执行部分由子类按同步/异步各自实现。
    """

    #: 绑定的 ORM 模型。子类**必须**覆盖。
    model: ClassVar[type[Base]]

    #: 当前租户。模型含 `tenant_id` 时必填。
    tenant_id: int | None = None

    #: 是否把软删除的记录也查出来（默认不查）。
    include_deleted: bool = False

    #: 是否允许跨租户查询。**仅供运维脚本显式开启**，
    #: 业务代码不应设置它。
    allow_cross_tenant: bool = False

    # ========================================================
    # 元信息
    # ========================================================

    @classmethod
    def _table_name(cls) -> str:
        return cls.model.__tablename__

    @classmethod
    def _columns(cls) -> frozenset[str]:
        """本模型全部列名（用于过滤字段白名单校验）。"""
        return frozenset(cls.model.__table__.columns.keys())

    @classmethod
    def _has_column(cls, name: str) -> bool:
        return name in cls.model.__table__.columns

    @classmethod
    def _is_tenant_scoped(cls) -> bool:
        """模型是否带租户字段（`TenantMixin`）。"""
        return cls._has_column("tenant_id")

    @classmethod
    def _supports_soft_delete(cls) -> bool:
        """模型是否支持软删除（`SoftDeleteMixin`）。"""
        return cls._has_column("deleted_at")

    # ========================================================
    # 作用域校验
    # ========================================================

    def _assert_tenant_scope(self) -> None:
        """校验租户作用域。

        含 `tenant_id` 的模型必须给出租户 ID，
        除非显式声明跨租户（运维场景）。

        这条校验是**故意做成硬失败**的：
        如果只是打个警告然后返回全量数据，那等于没做隔离。
        """
        if self._is_tenant_scoped() and self.tenant_id is None and not self.allow_cross_tenant:
            raise ConfigurationError(
                f"{type(self).__name__} 绑定的 {self._table_name()} 含 tenant_id，"
                "构造仓储时必须提供 tenant_id",
                code="TENANT_SCOPE_REQUIRED",
                action=(
                    "传入 tenant_id；"
                    "确需跨租户查询（如运维脚本）请显式传 allow_cross_tenant=True"
                ),
            )

    # ========================================================
    # 查询构建
    # ========================================================

    def _apply_tenant(self, stmt: Select) -> Select:
        """注入租户过滤。

        统一在这里加，业务代码不重复写 —— 少写一处就是越权。
        """
        if self._is_tenant_scoped() and self.tenant_id is not None:
            stmt = stmt.where(self.model.tenant_id == self.tenant_id)  # type: ignore[attr-defined]
        return stmt

    def _apply_soft_delete(self, stmt: Select) -> Select:
        """过滤软删除记录。

        只对**确实有** `deleted_at` 列的表生效。
        """
        if self._supports_soft_delete() and not self.include_deleted:
            stmt = stmt.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return stmt

    def _validate_filters(self, filters: dict[str, Any]) -> None:
        """校验过滤字段名。

        拼错字段名时直接报错，而不是静默忽略条件 ——
        静默忽略会让"按 shop_id 过滤"变成"返回全部店铺"。
        """
        unknown = set(filters) - self._columns()
        if unknown:
            raise ConfigurationError(
                f"{self._table_name()} 不存在字段：{sorted(unknown)}",
                code="UNKNOWN_FILTER_COLUMN",
                action=f"可用字段：{sorted(self._columns())}",
            )

    def _apply_filters(self, stmt: Select, filters: dict[str, Any]) -> Select:
        """应用等值/IN/NULL 过滤。"""
        self._validate_filters(filters)
        for name, value in filters.items():
            column = getattr(self.model, name)
            if value is None:
                stmt = stmt.where(column.is_(None))
            elif isinstance(value, (list, tuple, set, frozenset)):
                values = list(value)
                if not values:
                    # 空集合语义：`IN ()` 在 SQL 里非法，且"匹配空集合"
                    # 业务上就等于"什么都不要"，显式表达比隐式报错好。
                    stmt = stmt.where(False)  # noqa: FBT003
                else:
                    stmt = stmt.where(column.in_(values))
            else:
                stmt = stmt.where(column == value)
        return stmt

    def _apply_ordering(self, stmt: Select, order_by: str | Sequence[str] | None) -> Select:
        """应用排序。

        `order_by` 支持列名（前缀 `-` 表示降序），例如 `"-created_at"`。
        只接受模型真实存在的列，防止 SQL 注入与拼写错误。
        """
        if order_by is None:
            return stmt

        items = [order_by] if isinstance(order_by, str) else list(order_by)
        for item in items:
            descending = item.startswith("-")
            name = item.lstrip("-+")
            if name not in self._columns():
                raise ConfigurationError(
                    f"{self._table_name()} 不存在排序字段：{name}",
                    code="UNKNOWN_ORDER_COLUMN",
                    action=f"可用字段：{sorted(self._columns())}",
                )
            column = getattr(self.model, name)
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        return stmt

    @staticmethod
    def _normalize_pagination(limit: int | None, offset: int) -> tuple[int, int]:
        """规范化分页参数，并强制上限。"""
        if offset < 0:
            raise ConfigurationError(
                f"offset 不能为负：{offset}",
                code="INVALID_PAGINATION",
                action="offset 必须 >= 0",
            )
        if limit is None:
            limit = MAX_LIMIT
        if limit <= 0:
            raise ConfigurationError(
                f"limit 必须为正数：{limit}",
                code="INVALID_PAGINATION",
                action="limit 必须 >= 1",
            )
        if limit > MAX_LIMIT:
            raise ConfigurationError(
                f"limit={limit} 超过上限 {MAX_LIMIT}",
                code="LIMIT_EXCEEDED",
                action=f"请分页查询，单次最多 {MAX_LIMIT} 行",
            )
        return limit, offset

    def _prepare_write(self, obj: ModelT) -> None:
        """写入前处理：自动补租户、校验租户一致性。

        自动补租户是为了防止"忘记赋值 tenant_id"导致 NOT NULL 报错；
        校验一致性是为了防止把 A 租户的对象写进 B 租户的仓储。
        """
        if not self._is_tenant_scoped() or self.tenant_id is None:
            return

        current = getattr(obj, "tenant_id", None)
        if current is None:
            setattr(obj, "tenant_id", self.tenant_id)
        elif current != self.tenant_id:
            raise ConfigurationError(
                f"对象 tenant_id={current} 与仓储租户 {self.tenant_id} 不一致，拒绝写入",
                code="TENANT_MISMATCH",
                action="确认对象所属租户，或改用对应租户的仓储",
            )

    def _validate_update_columns(self, values: dict[str, Any]) -> None:
        """校验更新字段名，防止写入不存在的列。"""
        unknown = set(values) - self._columns()
        if unknown:
            raise ConfigurationError(
                f"{self._table_name()} 不存在字段：{sorted(unknown)}",
                code="UNKNOWN_UPDATE_COLUMN",
                action=f"可用字段：{sorted(self._columns())}",
            )


class BaseRepository(_QueryMixin[ModelT]):
    """异步仓储（FastAPI 接口层用）。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: int | None = None,
        include_deleted: bool = False,
        allow_cross_tenant: bool = False,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.include_deleted = include_deleted
        self.allow_cross_tenant = allow_cross_tenant
        self._assert_tenant_scope()

    # ---------- 读 ----------

    async def get(self, pk: int) -> ModelT | None:
        """按主键查询。"""
        stmt = self._stmt().where(self.model.id == pk)
        return (await self.session.execute(stmt)).scalars().first()

    async def get_or_raise(self, pk: int, *, resource: str | None = None) -> ModelT:
        """按主键查询，不存在则抛 `NotFoundError`。"""
        obj = await self.get(pk)
        if obj is None:
            raise NotFoundError(
                f"{resource or self._table_name()} 不存在",
                resource=resource or self._table_name(),
                resource_id=pk,
            )
        return obj

    async def get_by(self, **filters: Any) -> ModelT | None:
        """按条件查询单条（取第一条）。"""
        stmt = self._apply_filters(self._stmt(), filters)
        return (await self.session.execute(stmt)).scalars().first()

    async def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        order_by: str | Sequence[str] | None = None,
        **filters: Any,
    ) -> list[ModelT]:
        """条件查询列表。"""
        normalized_limit, normalized_offset = self._normalize_pagination(limit, offset)
        stmt = self._apply_filters(self._stmt(), filters)
        stmt = self._apply_ordering(stmt, order_by)
        stmt = stmt.limit(normalized_limit).offset(normalized_offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count(self, **filters: Any) -> int:
        """统计条数。"""
        stmt = select(func.count()).select_from(self.model)
        stmt = self._apply_tenant(stmt)
        stmt = self._apply_soft_delete(stmt)
        stmt = self._apply_filters(stmt, filters)
        return int((await self.session.execute(stmt)).scalar_one())

    async def exists(self, **filters: Any) -> bool:
        """是否存在满足条件的记录。"""
        stmt = self._apply_filters(self._stmt(), filters)
        stmt = stmt.limit(1)
        return (await self.session.execute(stmt)).scalars().first() is not None

    # ---------- 写 ----------

    async def add(self, obj: ModelT) -> ModelT:
        """新增单条（flush，不提交 —— 事务由 session scope 管理）。"""
        self._prepare_write(obj)
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def add_all(self, objs: Sequence[ModelT]) -> list[ModelT]:
        """批量新增。"""
        for obj in objs:
            self._prepare_write(obj)
        self.session.add_all(list(objs))
        await self.session.flush()
        return list(objs)

    async def update(self, obj: ModelT, **values: Any) -> ModelT:
        """按字段更新对象。"""
        self._validate_update_columns(values)
        for key, value in values.items():
            setattr(obj, key, value)
        await self.session.flush()
        return obj

    async def delete(self, obj: ModelT) -> None:
        """物理删除。

        仅适用于无历史价值的表（关联表、缓存表）。
        事实表请用 `soft_delete()` 或干脆不删。
        """
        await self.session.delete(obj)
        await self.session.flush()

    async def soft_delete(self, obj: ModelT) -> None:
        """软删除（标记 `deleted_at`）。"""
        if not self._supports_soft_delete():
            raise ConfigurationError(
                f"{self._table_name()} 不支持软删除（无 deleted_at 列）",
                code="SOFT_DELETE_UNSUPPORTED",
                action="事实表不应删除；关联表请改用 delete()",
            )
        setattr(obj, "deleted_at", utc_now())
        await self.session.flush()

    # ---------- 内部 ----------

    def _stmt(self) -> Select:
        """基础查询：已注入租户过滤与软删除过滤。"""
        stmt = select(self.model)
        stmt = self._apply_tenant(stmt)
        stmt = self._apply_soft_delete(stmt)
        return stmt


class SyncBaseRepository(_QueryMixin[ModelT]):
    """同步仓储（Celery worker 用）。

    与 `BaseRepository` 共用 `_QueryMixin` 的全部查询构建逻辑，
    因此两套接口的行为不会漂移（ADR-002 的代价就是这一份重复的"执行层"）。
    """

    def __init__(
        self,
        session: Session,
        *,
        tenant_id: int | None = None,
        include_deleted: bool = False,
        allow_cross_tenant: bool = False,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.include_deleted = include_deleted
        self.allow_cross_tenant = allow_cross_tenant
        self._assert_tenant_scope()

    # ---------- 读 ----------

    def get(self, pk: int) -> ModelT | None:
        stmt = self._stmt().where(self.model.id == pk)
        return self.session.execute(stmt).scalars().first()

    def get_or_raise(self, pk: int, *, resource: str | None = None) -> ModelT:
        obj = self.get(pk)
        if obj is None:
            raise NotFoundError(
                f"{resource or self._table_name()} 不存在",
                resource=resource or self._table_name(),
                resource_id=pk,
            )
        return obj

    def get_by(self, **filters: Any) -> ModelT | None:
        stmt = self._apply_filters(self._stmt(), filters)
        return self.session.execute(stmt).scalars().first()

    def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        order_by: str | Sequence[str] | None = None,
        **filters: Any,
    ) -> list[ModelT]:
        normalized_limit, normalized_offset = self._normalize_pagination(limit, offset)
        stmt = self._apply_filters(self._stmt(), filters)
        stmt = self._apply_ordering(stmt, order_by)
        stmt = stmt.limit(normalized_limit).offset(normalized_offset)
        return list(self.session.execute(stmt).scalars().all())

    def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(self.model)
        stmt = self._apply_tenant(stmt)
        stmt = self._apply_soft_delete(stmt)
        stmt = self._apply_filters(stmt, filters)
        return int(self.session.execute(stmt).scalar_one())

    def exists(self, **filters: Any) -> bool:
        stmt = self._apply_filters(self._stmt(), filters).limit(1)
        return self.session.execute(stmt).scalars().first() is not None

    # ---------- 写 ----------

    def add(self, obj: ModelT) -> ModelT:
        self._prepare_write(obj)
        self.session.add(obj)
        self.session.flush()
        return obj

    def add_all(self, objs: Sequence[ModelT]) -> list[ModelT]:
        for obj in objs:
            self._prepare_write(obj)
        self.session.add_all(list(objs))
        self.session.flush()
        return list(objs)

    def update(self, obj: ModelT, **values: Any) -> ModelT:
        self._validate_update_columns(values)
        for key, value in values.items():
            setattr(obj, key, value)
        self.session.flush()
        return obj

    def delete(self, obj: ModelT) -> None:
        self.session.delete(obj)
        self.session.flush()

    def soft_delete(self, obj: ModelT) -> None:
        if not self._supports_soft_delete():
            raise ConfigurationError(
                f"{self._table_name()} 不支持软删除（无 deleted_at 列）",
                code="SOFT_DELETE_UNSUPPORTED",
                action="事实表不应删除；关联表请改用 delete()",
            )
        setattr(obj, "deleted_at", utc_now())
        self.session.flush()

    # ---------- 内部 ----------

    def _stmt(self) -> Select:
        stmt = select(self.model)
        stmt = self._apply_tenant(stmt)
        stmt = self._apply_soft_delete(stmt)
        return stmt
