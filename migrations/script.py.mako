"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

迁移编写须知（ADR-007 / TDD-06 §5.3）：

1. **必须向后兼容**
   - 新增表 / 可空字段        → 安全
   - 新增非空字段（有默认值）  → 安全
   - **删除字段**             → 危险，需分两次发布（先停用，再删除）
   - **重命名字段**           → 危险，需分步（加新 → 双写 → 切读 → 删旧）
   原因：回滚时旧代码会找不到字段直接崩溃。

2. **索引与约束要显式命名**
   匿名约束在 MySQL 中会变成 `orders_ibfk_1` 这类名字，
   报错信息里出现这种名字，排障时根本不知道是哪条约束。
   命名规范见 core/db.py 的 NAMING_CONVENTION。

3. **MySQL 的 ALTER 能力有限**
   改列类型、删列、加约束可能需要重建表。
   大表上这些操作会锁表，需评估影响窗口。

4. **不要用 `op.execute("DROP ...")` 做数据清理**
   迁移只负责结构，数据清理属于运维脚本（ops/）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
