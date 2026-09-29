"""Alembic 迁移环境。

关键设计（ADR-007）：

1. **连接串从 core.config 读取**，不写在 alembic.ini 里 ——
   避免凭据进入版本库。

2. **同步引擎**（pymysql）：Alembic 用同步方式执行迁移更简单可靠，
   不需要处理事件循环。

3. **连接级补偿必须注入**（`init_command`）：
   统一 UTC 时区、严格 SQL 模式、READ-COMMITTED 隔离级别。
   否则迁移过程中的时间语义会与运行时不一致。

4. **强制导入 core.models**：否则 autogenerate 检测不到表，
   会生成"删除所有表"的迁移 —— 这是最危险的误操作之一。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 确保能 import 项目模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_settings  # noqa: E402
from core.db import NAMING_CONVENTION, Base  # noqa: E402

# 强制导入全部模型 —— 这一步不能省！
# 没有它，autogenerate 会认为"所有表都该被删除"
import core.models  # noqa: E402, F401

config = context.config
settings = get_settings()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: autogenerate 的比对目标
target_metadata = Base.metadata


def _get_url() -> str:
    """同步连接串（Alembic 用同步驱动更简单）。"""
    return settings.database_url_sync


def _connect_args() -> dict[str, object]:
    """连接级补偿。与运行时保持一致，否则迁移的语义会不同。"""
    return settings.db_connect_args_sync


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 不执行（用于 review 迁移内容）。

    用法：
        alembic upgrade head --sql > migration.sql
    """
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        # 保留命名约定，保证生成的约束名与 ORM 定义一致
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：直接连接数据库执行迁移。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=_connect_args(),
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 检测类型变化（如 VARCHAR(64) → VARCHAR(128)）
            compare_type=True,
            # 检测 server_default 变化
            compare_server_default=True,
            # MySQL 的 ALTER 能力有限，用 batch 模式重建表
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
