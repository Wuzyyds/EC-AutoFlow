"""ORM 通用 Mixin。

这些 Mixin 承载跨表的一致性约束，避免每张表重复写。

设计原则（TDD-01 原则 8）：
    **所有业务表都必须有 tenant_id**，即使当前是单租户。
    理由：后期加租户字段需要改所有表、所有索引、所有查询，成本极高；
    现在加的成本几乎为零（ADR-006）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text, text
from sqlalchemy.dialects.mysql import BIGINT as MySQLBigInt
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

__all__ = ["TimestampMixin", "TenantMixin", "SoftDeleteMixin", "NoteMixin"]


class TimestampMixin:
    """创建/更新时间。

    用 `CURRENT_TIMESTAMP(6)` 而非 `NOW()`：
        必须显式带精度 6，否则 MySQL 默认精度是 0（秒），
        会丢掉毫秒 —— 而我们在毫秒级做排序（如同一秒内的多条事件）。

    `updated_at` 用 `onupdate` 由 ORM 维护，而不是数据库的
    `ON UPDATE CURRENT_TIMESTAMP`：
        ORM 维护的好处是"批量 update 语句也会更新"，
        且行为在各数据库间一致；数据库触发器在批量操作时容易漏。

    这里直接写 MySQLDateTime 而不用 types.TimestampCol 别名 ——
    因为 Annotated 别名只能用在 `Mapped[...]` 位置，
    而 declared_attr 的返回值本身就是 mapped_column 调用。
    """

    @declared_attr
    @classmethod
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(
            MySQLDateTime(fsp=6),
            server_default=text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
            comment="创建时间（UTC）",
        )

    @declared_attr
    @classmethod
    def updated_at(cls) -> Mapped[datetime]:
        return mapped_column(
            MySQLDateTime(fsp=6),
            server_default=text("CURRENT_TIMESTAMP(6)"),
            onupdate=text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
            comment="更新时间（UTC）",
        )


class TenantMixin:
    """租户隔离字段。

    `repositories/base.py` 会统一注入租户过滤，业务代码不手写
    （否则漏写一处就是数据越权）。
    """

    @declared_attr
    @classmethod
    def tenant_id(cls) -> Mapped[int]:
        return mapped_column(
            MySQLBigInt(unsigned=True),
            nullable=False,
            index=True,
            comment="租户 ID（隔离单位）",
        )


class SoftDeleteMixin:
    """软删除。

    注意：并非所有表都需要软删除。
    业务事实表（订单、结算、审计）**不应**软删除 ——
    它们是历史记录，删了就破坏了可追溯性。
    只有配置类、商品类实体适合软删除。
    """

    @declared_attr
    @classmethod
    def deleted_at(cls) -> Mapped[datetime | None]:
        return mapped_column(
            MySQLDateTime(fsp=6),
            nullable=True,
            default=None,
            comment="软删除时间（NULL 表示未删除）",
        )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class NoteMixin:
    """备注字段。

    注意 MySQL 的 TEXT 不允许有 DEFAULT 值（ADR-007 §3.6），
    所以这里只声明 nullable，不给 default。
    """

    @declared_attr
    @classmethod
    def note(cls) -> Mapped[str | None]:
        return mapped_column(
            Text,
            nullable=True,
            comment="备注",
        )
