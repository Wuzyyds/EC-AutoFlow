"""MySQL 列类型（ADR-007 §3.2 类型映射的落点）。

**这里返回的是类型对象（TypeEngine），不是 Annotated 别名。**

为什么这样设计（踩过的坑）：
    SQLAlchemy 的 `Annotated[..., mapped_column(...)]` 别名**只能**用在
    `Mapped[XxxCol]` 位置，不能作为 `mapped_column(XxxCol, ...)` 的参数 ——
    后者会抛 `ArgumentError: 'SchemaItem' object ... expected`。

    本项目两种写法都会用到：
        # 写法 A（需要覆盖 nullable / default / comment 时）
        amount: Mapped[Decimal] = mapped_column(MoneyCol, nullable=False)

        # 写法 B（简洁声明）
        tenant_id: Mapped[int] = mapped_column(BigIntCol, index=True)

    返回类型对象可以同时支持两种写法，且语义更直白。
    唯一的例外是 `BigIntPK` —— 它必须携带 primary_key/autoincrement，
    因此保留 Annotated 形式（只在 `Mapped[BigIntPK]` 位置使用）。

关键映射（与 ADR-007 附录 A 一致）：

| PostgreSQL      | 本项目（MySQL）    | 说明 |
|-----------------|-------------------|------|
| BIGSERIAL       | BIGINT UNSIGNED AI | 自增主键 |
| TIMESTAMPTZ     | DATETIME(6)       | **存 naive UTC** |
| JSONB           | JSON              | 无 GIN 索引 |
| BYTEA           | VARBINARY         | 加密字段 |
| NUMERIC(18,6)   | DECIMAL(18,6)     | 金额 |
| VARCHAR(n)[]    | JSON              | 无数组类型 |
| BOOLEAN         | TINYINT(1)        | 布尔 |
"""

from __future__ import annotations

from typing import Annotated

from sqlalchemy import Integer, SmallInteger, Text
from sqlalchemy.dialects.mysql import (
    BIGINT as MySQLBigInt,
    DATETIME as MySQLDateTime,
    DECIMAL as MySQLDecimal,
    JSON as MySQLJSON,
    TINYINT as MySQLTinyInt,
    VARBINARY as MySQLVarBinary,
)
from sqlalchemy.orm import mapped_column
from sqlalchemy.types import TypeEngine

__all__ = [
    "BigIntPK",
    "BigIntCol",
    "BigIntColNullable",
    "IntCol",
    "IntColNullable",
    "SmallIntCol",
    "MoneyCol",
    "MoneyColNullable",
    "RateCol",
    "RateColNullable",
    "PercentCol",
    "PercentColNullable",
    "BoolCol",
    "BoolColNullable",
    "TimestampCol",
    "TimestampColNullable",
    "JsonCol",
    "JsonColNullable",
    "JsonArrayCol",
    "BinaryCol",
    "BinaryColNullable",
    "SmallBinaryColNullable",
    "TEXT",
    "STR",
]


# ============================================================
# 主键（唯一的 Annotated 例外）
# ============================================================

#: 自增主键。对应 PostgreSQL 的 BIGSERIAL。
#:
#: 用 UNSIGNED —— 主键和外键不会是负数，
#: 且 UNSIGNED BIGINT 的上限是 1.8e19，永远用不完。
#:
#: **用法限定**：只能写 `id: Mapped[BigIntPK]`，
#: 因为它已携带 primary_key/autoincrement，不需要再套 mapped_column。
BigIntPK = Annotated[
    int,
    mapped_column(MySQLBigInt(unsigned=True), primary_key=True, autoincrement=True),
]


# ============================================================
# 整数
# ============================================================

#: 外键 / 普通大整数列
BigIntCol: TypeEngine[int] = MySQLBigInt(unsigned=True)

#: 可空大整数（在 mapped_column 里配 nullable=True）
BigIntColNullable: TypeEngine[int] = MySQLBigInt(unsigned=True)

IntCol: TypeEngine[int] = Integer()

IntColNullable: TypeEngine[int] = Integer()

SmallIntCol: TypeEngine[int] = SmallInteger()


# ============================================================
# 字符串
# ============================================================
#
# 字符串列**不提供别名**，直接用 `mapped_column(String(n), ...)`。
# 原因：长度是每个字段都要单独决定的，预定义别名会导致
# 要么别名过多（Str64/Str128/Str255/...），要么被迫用不合适的长度。
#
# **索引前缀长度提醒**（ADR-007 §3.12）：
#     utf8mb4 下每字符 4 字节，InnoDB DYNAMIC 行格式的索引前缀上限
#     是 3072 字节 → **单列最大 VARCHAR(768)**。
#     本项目最长的可索引列是 VARCHAR(512) = 2048 字节，安全。

#: 长文本。**注意 MySQL 的 TEXT 不允许有 DEFAULT 值**（ADR-007 §3.6）。
TEXT = Text

#: 字符串类型构造器（语法糖，等价于 sqlalchemy.String）
from sqlalchemy import String as STR  # noqa: E402


# ============================================================
# 数值
# ============================================================

#: 金额。对应 PostgreSQL 的 NUMERIC(18,6)。
#:
#: 6 位小数足以容纳汇率换算的中间结果；
#: 12 位整数部分支持大金额（上限约 9999 亿）。
MoneyCol: TypeEngine[object] = MySQLDecimal(18, 6)

MoneyColNullable: TypeEngine[object] = MySQLDecimal(18, 6)

#: 汇率（需要更高精度，8 位小数）
RateCol: TypeEngine[object] = MySQLDecimal(18, 8)

RateColNullable: TypeEngine[object] = MySQLDecimal(18, 8)

#: 百分比 / 比率（0–100 的数值，如 TACOS = 15.32）
#:
#: 统一用 0–100 而非 0–1，与业务人员的习惯一致，
#: 避免"这个 0.15 是 15% 还是 0.15%"的歧义。
PercentCol: TypeEngine[object] = MySQLDecimal(8, 4)

PercentColNullable: TypeEngine[object] = MySQLDecimal(8, 4)


# ============================================================
# 布尔
# ============================================================

#: 布尔。MySQL 内部是 TINYINT(1)。
BoolCol: TypeEngine[bool] = MySQLTinyInt(1)

BoolColNullable: TypeEngine[bool] = MySQLTinyInt(1)


# ============================================================
# 时间
# ============================================================

#: 时间戳。对应 PostgreSQL 的 TIMESTAMPTZ。
#:
#: **关键约定（ADR-007 §3.3）**：
#:     用 DATETIME(6) 而不是 TIMESTAMP，因为 MySQL 的 TIMESTAMP
#:     会随 session time_zone 自动转换 —— 同一行在不同连接下
#:     读出不同的时间，且上限是 2038 年。
#:
#:     存进去的**一定是 naive UTC**（应用层经 core.timeutil.to_db 转换），
#:     读出来的是 naive UTC，由 from_db() 补上 tzinfo。
#:
#:     精度 6（微秒）是必须的：我们在毫秒级做排序，
#:     默认精度 0（秒）会让同一秒内的多条事件无法定序。
TimestampCol: TypeEngine[object] = MySQLDateTime(fsp=6)

TimestampColNullable: TypeEngine[object] = MySQLDateTime(fsp=6)


# ============================================================
# 结构化
# ============================================================

#: JSON 列。对应 PostgreSQL 的 JSONB。
#:
#: **能力差异（ADR-007 §3.4）**：MySQL 的 JSON 列无法直接建索引，
#: 需要生成列（Generated Column）间接实现。
#: 本项目的 JSON 列主要用于归档原始报文，**没有按内容检索的需求**，
#: 因此不需要生成列。
JsonCol: TypeEngine[object] = MySQLJSON()

JsonColNullable: TypeEngine[object] = MySQLJSON()

#: JSON 数组（原 PostgreSQL 的 VARCHAR[] / BIGINT[]）
JsonArrayCol: TypeEngine[object] = MySQLJSON()


# ============================================================
# 二进制
# ============================================================

#: 二进制。对应 PostgreSQL 的 BYTEA。用于加密后的凭据与 PII。
#:
#: 长度 4096 足够容纳 AES-GCM 加密后的 token（原始 2KB 以内）。
BinaryCol: TypeEngine[bytes] = MySQLVarBinary(4096)

BinaryColNullable: TypeEngine[bytes] = MySQLVarBinary(4096)

#: 短二进制。用于 nonce（AES-GCM 的 12 字节）这类定长小数据。
SmallBinaryColNullable: TypeEngine[bytes] = MySQLVarBinary(32)
