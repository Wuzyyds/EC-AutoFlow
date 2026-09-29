"""数据库引擎与 Session 管理。

双引擎设计（TDD-01 ADR-002）：
    - **异步引擎**（aiomysql）：FastAPI 接口层，支撑高并发读
    - **同步引擎**（pymysql）：Celery worker，同步模型更简单且无收益损失

两套引擎共用同一份模型定义（`core/models/base.py` 的 `Base`）。

MySQL 关键约束（ADR-007）：

1. **命名约定必须显式声明**
   MySQL 对匿名约束会自动生成 `orders_ibfk_1`、`orders_chk_1` 这类名字。
   报错信息里出现这种名字，排障时根本不知道是哪条约束。
   通过 `MetaData(naming_convention=...)` 让所有约束自动获得规范名。

2. **时间列一律 DATETIME(6)**
   MySQL 没有 TIMESTAMPTZ。用 DATETIME 存 naive UTC，
   用 TIMESTAMP 会随 session 时区漂移且有 2038 问题。

3. **连接必须注入 init_command**
   统一时区、严格模式、隔离级别。详见 core/config.py。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from core.config import Settings, get_settings

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "get_async_engine",
    "get_sync_engine",
    "get_async_session_factory",
    "get_sync_session_factory",
    "async_session_scope",
    "sync_session_scope",
    "dispose_engines",
    "check_database_capabilities",
]

#: 约束命名约定。
#:
#: 生成的名字示例：
#:   idx_orders_shop_id_order_time    索引
#:   uk_orders_shop_id_platform_order_id  唯一键
#:   fk_order_items_order_id          外键
#:   chk_orders_order_status          检查约束
#:   pk_orders                        主键
#:
#: 注意 MySQL 的标识符上限是 64 字符。长表名 + 多列索引可能超限，
#: 因此这里用列名的前若干列拼接，超长时需在模型里显式指定 name=。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "idx_%(table_name)s_%(column_0_N_name)s",
    "uq": "uk_%(table_name)s_%(column_0_N_name)s",
    "ck": "chk_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    `domain/` 层**不得** import 本模块（TDD-01 §3.2 依赖方向）。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        """转为字典（日志与调试用）。

        注意：**不得**直接用于 API 响应 —— 可能含 PII 与内部字段。
        API 层必须用显式的 Pydantic schema。
        """
        skip = exclude or set()
        return {
            c.name: getattr(self, c.name)
            for c in self.__table__.columns
            if c.name not in skip
        }

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


#: 本项目所有表必须使用的存储引擎与字符集。
#:
#: **为什么必须显式指定而不能依赖默认值**：
#:     本机 MySQL 的 `default_storage_engine` 被配置成了 **MyISAM**，
#:     而 MyISAM 不支持事务、外键、行级锁 —— 对本项目是致命的
#:     （所有状态机与审批流程都依赖事务）。
#:     同时 MyISAM 的索引前缀上限是 1000 字节（InnoDB DYNAMIC 是 3072），
#:     会导致 `idx_approvals_resource` 这类复合索引建表失败。
#:
#:     不依赖服务器配置、在代码里强制，是唯一可靠的做法。
FORCED_TABLE_OPTIONS: dict[str, str] = {
    "mysql_engine": "InnoDB",
    "mysql_row_format": "DYNAMIC",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


@event.listens_for(Table, "before_create")
def _force_table_options(
    target: Table, connection: Any, **kwargs: Any  # noqa: ARG001
) -> None:
    """在建表前强制注入存储引擎与字符集。

    用事件钩子而不是在每个模型写 `__table_args__`：
        46 张表逐个写既繁琐又容易漏，
        而漏掉的那张表会以 MyISAM 建出来 —— 且没有任何报错。

    使用 `setdefault` 语义：模型显式指定的值优先（允许个别表用不同配置）。
    """
    existing = target.dialect_options.get("mysql", {})
    for key, value in FORCED_TABLE_OPTIONS.items():
        short = key.removeprefix("mysql_")
        if short not in existing:
            target.dialect_options["mysql"][short] = value


# ============================================================
# 引擎
# ============================================================

_async_engine: AsyncEngine | None = None
_sync_engine: Any = None


def get_async_engine(settings: Settings | None = None) -> AsyncEngine:
    """获取异步引擎（单例）。"""
    global _async_engine  # noqa: PLW0603
    if _async_engine is None:
        cfg = settings or get_settings()
        _async_engine = create_async_engine(
            cfg.database_url,
            echo=cfg.mysql_echo,
            pool_size=cfg.mysql_pool_size,
            max_overflow=cfg.mysql_max_overflow,
            # 防 MySQL 8 小时空闲断连（wait_timeout 默认 28800 秒）
            pool_recycle=cfg.mysql_pool_recycle_seconds,
            # 取连接前 ping 一次，避免拿到已被服务端关闭的死连接
            pool_pre_ping=True,
            connect_args=cfg.db_connect_args,
        )
    return _async_engine


def get_sync_engine(settings: Settings | None = None) -> Any:
    """获取同步引擎（单例，Celery worker 用）。

    注意用 `db_connect_args_sync` —— pymysql 支持读写超时，
    而 aiomysql 不支持，两者参数不能共用。
    """
    global _sync_engine  # noqa: PLW0603
    if _sync_engine is None:
        cfg = settings or get_settings()
        _sync_engine = create_engine(
            cfg.database_url_sync,
            echo=cfg.mysql_echo,
            pool_size=cfg.mysql_pool_size,
            max_overflow=cfg.mysql_max_overflow,
            pool_recycle=cfg.mysql_pool_recycle_seconds,
            pool_pre_ping=True,
            connect_args=cfg.db_connect_args_sync,
        )
    return _sync_engine


# ============================================================
# Session 工厂
# ============================================================

_async_session_factory: async_sessionmaker[AsyncSession] | None = None
_sync_session_factory: sessionmaker[Session] | None = None


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    global _async_session_factory  # noqa: PLW0603
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_async_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # 提交后仍可访问属性，避免 N+1 意外查询
            autoflush=False,  # 显式 flush，避免隐式 IO 难以排查
        )
    return _async_session_factory


def get_sync_session_factory() -> sessionmaker[Session]:
    global _sync_session_factory  # noqa: PLW0603
    if _sync_session_factory is None:
        _sync_session_factory = sessionmaker(
            bind=get_sync_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sync_session_factory


# ============================================================
# 上下文管理器
# ============================================================


@asynccontextmanager
async def async_session_scope() -> AsyncGenerator[AsyncSession, None]:
    """异步 Session 上下文（自动提交/回滚）。

    用法：
        async with async_session_scope() as session:
            session.add(obj)

    正常退出提交，异常回滚并重新抛出。
    """
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextmanager
def sync_session_scope() -> Generator[Session, None, None]:
    """同步 Session 上下文（Celery worker 用）。"""
    factory = get_sync_session_factory()
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


async def dispose_engines() -> None:
    """释放连接池。服务关闭与测试清理时调用。"""
    global _async_engine, _sync_engine  # noqa: PLW0603
    global _async_session_factory, _sync_session_factory  # noqa: PLW0603
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
    if _sync_engine is not None:
        _sync_engine.dispose()
        _sync_engine = None
    _async_session_factory = None
    _sync_session_factory = None


# ============================================================
# 能力探测
# ============================================================

#: CHECK 约束真正生效所需的最低 MySQL 版本（ADR-007 §3.1）
MIN_MYSQL_VERSION_FOR_CHECK = (8, 0, 16)


async def check_database_capabilities(session: AsyncSession) -> dict[str, Any]:
    """探测数据库能力，返回诊断信息。

    **这是启动期的必要检查。** MySQL 8.0.16 之前会静默忽略 CHECK 约束，
    不检查就无法察觉 —— 数据会在数月后以"状态字段出现非法值"的形式爆发。

    Returns:
        含 `version`、`supports_check`、`charset`、`time_zone`、`sql_mode`、
        `isolation`、`warnings` 的字典。
    """
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                "SELECT VERSION() AS version, "
                "@@character_set_database AS charset, "
                "@@collation_database AS collation, "
                "@@time_zone AS time_zone, "
                "@@sql_mode AS sql_mode, "
                "@@transaction_isolation AS isolation"
            )
        )
    ).mappings().one()

    version_str: str = row["version"]
    # 版本号可能是 "8.0.12" 或 "8.0.12-log" 形式
    numeric = version_str.split("-")[0]
    parts = tuple(int(p) for p in numeric.split(".") if p.isdigit())
    supports_check = parts >= MIN_MYSQL_VERSION_FOR_CHECK

    warnings: list[str] = []

    if not supports_check:
        warnings.append(
            f"MySQL {version_str} 低于 8.0.16，CHECK 约束**不会被执行**。"
            "已自动降级为触发器实现（见 ops/generate_constraints.py）。"
        )
    if row["charset"] != "utf8mb4":
        warnings.append(
            f"数据库字符集为 {row['charset']}，非 utf8mb4 —— "
            "不支持 emoji 与部分扩展汉字，且中文索引可能异常。"
        )
    if row["time_zone"] != "+00:00":
        warnings.append(
            f"连接时区为 {row['time_zone']}，非 +00:00 —— "
            "时间语义不确定。检查 init_command 是否生效。"
        )
    if "STRICT_TRANS_TABLES" not in (row["sql_mode"] or ""):
        warnings.append(
            "sql_mode 未启用 STRICT_TRANS_TABLES —— 超长数据会被静默截断。"
        )

    return {
        "version": version_str,
        "supports_check": supports_check,
        "min_version_for_check": ".".join(str(p) for p in MIN_MYSQL_VERSION_FOR_CHECK),
        "charset": row["charset"],
        "collation": row["collation"],
        "time_zone": row["time_zone"],
        "sql_mode": row["sql_mode"],
        "isolation": row["isolation"],
        "warnings": warnings,
    }
