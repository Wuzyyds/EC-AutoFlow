"""约束生成器（MySQL 版本自适应）。

**这个脚本存在的理由（ADR-007 §3.1）**：

    MySQL 8.0.16 之前的版本会**解析 `CHECK` 语法但完全忽略它** ——
    不报错、不警告、不生效。

    本机实测服务端是 8.0.12，即属于这种情况。
    如果不做补偿，core/constants.py 里定义的 29 个枚举字段
    全部没有约束保护，非法状态值可以随意写入，
    且**没有任何报错** —— 数据会在数月后以
    "状态字段出现拼写错误的值"的形式爆发。

**两种实现方式**：

    8.0.16+  → 原生 CHECK 约束（更高效，元数据可见）
    < 8.0.16 → BEFORE INSERT/UPDATE 触发器（对所有版本生效）

    触发器方案其实**更强**：它不依赖版本，且对 root 用户同样生效。

**除了枚举约束，还生成三类特殊约束**：

    1. 职责分离（approvals）：approved_by <> requested_by
    2. 金额非负（金额字段）
    3. append-only（4 张审计表禁止 UPDATE/DELETE）
    4. 生成列（sku_mappings / config_items 的"部分唯一索引"）

用法：
    python ops/generate_constraints.py --dry-run   # 只看要执行什么
    python ops/generate_constraints.py --apply     # 实际执行
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from core.config import get_settings  # noqa: E402
from core.constants import ENUM_COLUMNS  # noqa: E402
from core.db import MIN_MYSQL_VERSION_FOR_CHECK  # noqa: E402

#: append-only 表（禁止 UPDATE / DELETE）
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "audit_events",
    "state_transitions",
    "approval_actions",
    "credential_audit_logs",
)

#: 金额非负字段（表, 字段）
NON_NEGATIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("orders", "item_total"),
    ("orders", "grand_total"),
    ("refunds", "amount"),
    ("order_fees", "amount"),
    ("settlement_fees", "amount"),
    ("profit_snapshots", "net_sales"),
    ("cost_items", "amount"),
)

#: 生成列定义。
#:
#: **注意：生成列现在由 ORM 的 `Computed()` 声明 + Alembic 迁移管理**，
#: 本脚本不再负责创建它们（这里只保留定义用于文档说明与清理）。
#:
#: 为什么改由 ORM 管理：
#:     早先写成"ORM 声明普通列 + 本脚本 ALTER TABLE 加生成表达式"，
#:     结果是 Alembic 先建了一个普通 VARCHAR 列，本脚本的 ALTER 又因
#:     "列已存在"被跳过 —— 最终索引建在一个永远为 NULL 的列上，
#:     **约束既不生效也不报错**。
#:     正确做法是在 ORM 里用 `Computed(expr, persisted=True)`，
#:     让 Alembic 一次性生成正确的 DDL。
GENERATED_COLUMNS: tuple[dict[str, str], ...] = (
    {
        "table": "sku_mappings",
        "column": "primary_sku_key",
        "expression": "IF(is_primary = 1, internal_sku, NULL)",
        "type": "VARCHAR(128)",
        "index_name": "uk_sku_mappings_primary",
        "index_cols": "shop_id, primary_sku_key",
        "purpose": "每个店铺每个内部 SKU 只能有一个主映射（替代 PostgreSQL 的 EXCLUDE 约束）",
    },
    {
        "table": "config_items",
        "column": "active_key",
        # 用 CHAR(58) 而非字面量 ':' —— SQLAlchemy 的 text() 会把
        # 冒号当成绑定参数占位符，即使它在引号内也可能被误解析
        "expression": "IF(effective_to IS NULL, CONCAT(namespace, CHAR(58), config_key), NULL)",
        "type": "VARCHAR(192)",
        "index_name": "uk_config_items_active",
        "index_cols": "tenant_id, active_key",
        "purpose": "每个 key 只能有一个当前生效值",
    },
)


@dataclass
class ConstraintPlan:
    """待执行的约束清单。"""

    mode: str = "trigger"
    statements: list[tuple[str, str]] = field(default_factory=list)

    def add(self, category: str, sql: str) -> None:
        self.statements.append((category, sql))

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cat, _ in self.statements:
            counts[cat] = counts.get(cat, 0) + 1
        return counts


# ============================================================
# 枚举约束
# ============================================================


def _sql_enum_list(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def build_enum_check(table: str, column: str, values: list[str]) -> str:
    """MySQL 8.0.16+ 的原生 CHECK 约束。"""
    name = f"chk_{table}_{column}"
    return (
        f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` "
        f"CHECK (`{column}` IN ({_sql_enum_list(values)}))"
    )


def build_enum_trigger(table: str, column: str, values: list[str], *, event: str) -> str:
    """低版本 MySQL 的触发器实现。

    用 `BEFORE INSERT` 与 `BEFORE UPDATE` 两个触发器覆盖全部写入路径。
    只做 INSERT 会漏掉"改状态"这个最关键的场景。

    **注意消息文本的写法**：
        MESSAGE_TEXT 是单引号字符串，所以内容里**不能再出现单引号** ——
        否则字符串提前闭合，抛 1064 语法错误（这个坑踩过）。
        因此消息里只放字段名，不放带引号的取值列表；
        而且 MySQL 的 MESSAGE_TEXT 上限是 128 字符，
        长枚举列表也放不下。
    """
    name = f"trg_{table}_{column}_{event.lower()}"
    allowed = _sql_enum_list(values)
    # 消息里只保留表字段名（不含引号），完整取值列表见 core/constants.py
    message = f"{table}.{column}: invalid enum value"
    return (
        f"CREATE TRIGGER `{name}` BEFORE {event} ON `{table}` FOR EACH ROW\n"
        f"BEGIN\n"
        f"    IF NEW.`{column}` NOT IN ({allowed}) THEN\n"
        f"        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}';\n"
        f"    END IF;\n"
        f"END"
    )


# ============================================================
# 特殊约束
# ============================================================


def build_separation_of_duties() -> str:
    """职责分离：发起人不得为审批人。

    应用层已有 guard（domain.state_machines.approval 的 _is_not_requester），
    数据库层再加一道 —— 防的是"绕过 API 直接改库"的场景。
    """
    return (
        "ALTER TABLE `approvals` ADD CONSTRAINT `chk_approvals_separation_of_duties` "
        "CHECK (`approved_by` IS NULL OR `approved_by` <> `requested_by`)"
    )


def build_separation_of_duties_trigger(*, event: str) -> str:
    return (
        f"CREATE TRIGGER `trg_approvals_sod_{event.lower()}` BEFORE {event} ON `approvals`\n"
        f"FOR EACH ROW\n"
        f"BEGIN\n"
        f"    IF NEW.`approved_by` IS NOT NULL AND NEW.`approved_by` = NEW.`requested_by` THEN\n"
        f"        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        f"'职责分离：发起人不能审批自己提交的申请';\n"
        f"    END IF;\n"
        f"END"
    )


def build_non_negative(table: str, column: str) -> str:
    name = f"chk_{table}_{column}_non_negative"
    return f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` CHECK (`{column}` >= 0)"


def build_non_negative_trigger(table: str, column: str, *, event: str) -> str:
    name = f"trg_{table}_{column}_nn_{event.lower()}"
    return (
        f"CREATE TRIGGER `{name}` BEFORE {event} ON `{table}` FOR EACH ROW\n"
        f"BEGIN\n"
        f"    IF NEW.`{column}` < 0 THEN\n"
        f"        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        f"'{table}.{column} 不得为负数';\n"
        f"    END IF;\n"
        f"END"
    )


def build_append_only_trigger(table: str, *, event: str) -> str:
    """append-only 强制。

    为什么用触发器而不是 `REVOKE UPDATE, DELETE ... FROM PUBLIC`
    （ADR-007 §3.9）：
        MySQL 没有 PUBLIC 角色概念，REVOKE 必须针对具体用户；
        且即使 REVOKE 了应用账号，root 和 DBA 仍可修改。
        **触发器对所有用户包括 root 生效**，是更可靠的强制手段。
    """
    name = f"trg_{table}_no_{event.lower()}"
    return (
        f"CREATE TRIGGER `{name}` BEFORE {event} ON `{table}` FOR EACH ROW\n"
        f"BEGIN\n"
        f"    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        f"'{table} 是 append-only 表，禁止 {event}';\n"
        f"END"
    )


def build_generated_column(spec: dict[str, str]) -> str:
    """生成列（用于实现"部分唯一索引"）。

    关键技巧：MySQL 的唯一索引允许多个 NULL。
    所以"仅当条件满足时唯一"可以表达为
    "条件不满足时生成 NULL"。
    """
    return (
        f"ALTER TABLE `{spec['table']}`\n"
        f"    ADD COLUMN `{spec['column']}` {spec['type']}\n"
        f"        GENERATED ALWAYS AS ({spec['expression']}) STORED,\n"
        f"    ADD UNIQUE KEY `{spec['index_name']}` ({spec['index_cols']})"
    )


# ============================================================
# 触发器体片段（会被合并进同一张表的同一个触发器）
# ============================================================


def _check_enum_body(table: str, column: str, values: list[str]) -> str:
    """枚举校验的触发器体片段。

    **消息文本必须是纯 ASCII**（这个坑踩过）：
        含中文的 MESSAGE_TEXT 经 pymysql 发送时会被截断，
        导致整个触发器体丢失，生成一个空的 `BEGIN END` 触发器 ——
        **不报错，但约束完全失效**。这类静默失效最危险。
        因此消息统一用英文，中文说明放在代码注释与文档里。
    """
    allowed = _sql_enum_list(values)
    return (
        f"    IF NEW.`{column}` NOT IN ({allowed}) THEN\n"
        f"        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        f"'{table}.{column}: invalid enum value';\n"
        f"    END IF;"
    )


def _check_non_negative_body(table: str, column: str) -> str:
    return (
        f"    IF NEW.`{column}` < 0 THEN\n"
        f"        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        f"'{table}.{column}: must not be negative';\n"
        f"    END IF;"
    )


def _check_separation_of_duties_body() -> str:
    return (
        "    IF NEW.`approved_by` IS NOT NULL "
        "AND NEW.`approved_by` = NEW.`requested_by` THEN\n"
        "        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'approvals: requester cannot approve own request';\n"
        "    END IF;"
    )


def _check_append_only_body(table: str, event: str) -> str:
    return (
        f"    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        f"'{table} is append-only, {event} is forbidden';"
    )


def build_merged_trigger(table: str, event: str, bodies: list[str]) -> str:
    """把同一张表同一事件的所有校验合并为**一个**触发器。

    **为什么必须合并**（MySQL 的硬限制）：
        一张表在同一个 timing + event 组合上**只能有一个触发器**。
        创建第二个会报 `ERROR 1359 Trigger already exists`。

        如果不合并，会得到这样的结果：
        - approvals 表需要 6 个 BEFORE INSERT 校验（5 个枚举 + 职责分离）
        - 只有第 1 个真正生效，其余 5 个被静默跳过
        - 表面看"84 条约束全部成功"，实际大部分没生效

        这是本次开发中最隐蔽的一个坑 —— 直到逐条写入非法数据
        做验证时才暴露。
    """
    name = f"trg_{table}_{event.lower()}"
    body = "\n".join(bodies)
    return (
        f"CREATE TRIGGER `{name}` BEFORE {event} ON `{table}` FOR EACH ROW\n"
        f"BEGIN\n"
        f"{body}\n"
        f"END"
    )


# ============================================================
# 计划构建
# ============================================================


def build_plan(*, use_check: bool) -> ConstraintPlan:
    """构建约束执行计划。

    Args:
        use_check: True 用原生 CHECK（8.0.16+），False 用触发器。

    触发器模式下，同一表同一事件的校验会被**合并**（见 build_merged_trigger）。
    """
    plan = ConstraintPlan(mode="check" if use_check else "trigger")

    if use_check:
        # ---- 原生 CHECK 模式：每个约束独立，无需合并 ----
        for key, enum_cls in ENUM_COLUMNS.items():
            table, column = key.split(".", 1)
            plan.add("enum", build_enum_check(table, column, enum_cls.db_values()))

        plan.add("integrity", build_separation_of_duties())

        for table, column in NON_NEGATIVE_COLUMNS:
            plan.add("integrity", build_non_negative(table, column))

        for table in APPEND_ONLY_TABLES:
            plan.add("append_only", build_append_only_trigger(table, event="UPDATE"))
            plan.add("append_only", build_append_only_trigger(table, event="DELETE"))
    else:
        # ---- 触发器模式：按 (表, 事件) 聚合 ----
        #
        # 结构：{(table, event): [body, ...]}
        buckets: dict[tuple[str, str], list[str]] = {}

        def add_body(table: str, event: str, body: str) -> None:
            buckets.setdefault((table, event), []).append(body)

        # 1. 枚举校验
        for key, enum_cls in ENUM_COLUMNS.items():
            table, column = key.split(".", 1)
            body = _check_enum_body(table, column, enum_cls.db_values())
            add_body(table, "INSERT", body)
            add_body(table, "UPDATE", body)

        # 2. 金额非负
        for table, column in NON_NEGATIVE_COLUMNS:
            body = _check_non_negative_body(table, column)
            add_body(table, "INSERT", body)
            add_body(table, "UPDATE", body)

        # 3. 职责分离
        sod = _check_separation_of_duties_body()
        add_body("approvals", "INSERT", sod)
        add_body("approvals", "UPDATE", sod)

        # 4. append-only
        for table in APPEND_ONLY_TABLES:
            add_body(table, "UPDATE", _check_append_only_body(table, "UPDATE"))
            add_body(table, "DELETE", _check_append_only_body(table, "DELETE"))

        # 5. 合并输出
        for (table, event), bodies in buckets.items():
            category = "append_only" if table in APPEND_ONLY_TABLES else "enum"
            if table == "approvals" and event in ("INSERT", "UPDATE"):
                category = "integrity"
            plan.add(category, build_merged_trigger(table, event, bodies))

    # ---- 生成列 ----
    #
    # **本脚本不创建生成列** —— 它们由 ORM 的 `Computed()` 声明，
    # 由 Alembic 迁移负责创建（见 GENERATED_COLUMNS 的说明）。
    # 这里只做"如果发现了历史遗留的错误生成列则清理"，
    # 实际的创建留给 `alembic upgrade head`。
    #
    # 这样职责清晰：
    #   Alembic   → 表结构（含生成列与唯一索引）
    #   本脚本    → 约束（CHECK / 触发器），因为这部分 MySQL 版本差异大

    return plan


# ============================================================
# 执行
# ============================================================


def detect_supports_check(conn: object) -> tuple[bool, str]:
    """探测 MySQL 版本是否支持 CHECK 约束。"""
    version = conn.execute(text("SELECT VERSION()")).scalar()  # type: ignore[attr-defined]
    numeric = str(version).split("-")[0]
    parts = tuple(int(p) for p in numeric.split(".") if p.isdigit())
    return parts >= MIN_MYSQL_VERSION_FOR_CHECK, str(version)


def drop_existing(engine: object, plan: ConstraintPlan) -> int:
    """删除计划中涉及的已存在触发器与 CHECK 约束。

    **为什么需要这个函数**（踩过的坑）：
        如果只是"创建时跳过已存在的"，会留下**内容错误的旧触发器** ——
        例如上一次运行因故生成了空的 `BEGIN END` 触发器体，
        这次运行会因"名称已存在"而跳过，于是空触发器永久残留，
        且**没有任何报错**，约束形同虚设。

        先删后建（幂等重建）是唯一可靠的做法。

    **注意：本函数不碰生成列。**
        生成列由 ORM 的 `Computed()` 声明、由 Alembic 迁移创建。
        早先这里也删生成列，结果是：
            本脚本删了生成列 → Alembic 不重跑 → 唯一索引退化成
            只含 shop_id 的单列索引 → 同一店铺只能插一条 SKU 映射。
        这类"索引悄悄少了一列"的问题极难排查。

    Returns:
        删除的对象数量。
    """
    dropped = 0
    with engine.begin() as conn:  # type: ignore[attr-defined]
        # 1. 删除触发器
        rows = conn.exec_driver_sql(
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA = DATABASE()"
        ).fetchall()
        for (name,) in rows:
            conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS `{name}`")
            dropped += 1

        # 2. 删除 CHECK 约束（若上次用的是 CHECK 模式）
        constraints = conn.exec_driver_sql(
            "SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
        ).fetchall()
        for table, name in constraints:
            try:
                conn.exec_driver_sql(f"ALTER TABLE `{table}` DROP CONSTRAINT `{name}`")
                dropped += 1
            except Exception:  # noqa: BLE001, S110
                pass

    return dropped


def apply_plan(engine: object, plan: ConstraintPlan, *, dry_run: bool) -> tuple[int, int]:
    """执行计划，返回 (成功数, 失败数)。

    三个关键实现细节（都踩过坑）：

    1. **用 `exec_driver_sql` 而不是 `execute(text(...))`**
       `text()` 构造的 TextClause 在包含多行 + 分号的 DDL 上
       会出现"触发器体丢失"的现象 —— 生成的触发器变成空的
       `BEGIN END` 块，**不报错但完全不起作用**。
       这类"静默失效"是最危险的，因为它看起来成功了。

    2. **每条语句独立事务**
       批量在同一事务里执行时，任一条失败会回滚前面所有成功的语句。
       而约束创建本身是幂等的（重复执行会因"已存在"失败），
       独立事务可以让部分成功保留下来。

    3. **"已存在"不算失败**
       重复执行本脚本是正常操作（如新增枚举值后重跑），
       此时已存在的约束会报错，应视为成功。

    Returns:
        (成功数, 失败数)
    """
    ok = fail = 0
    for category, sql in plan.statements:
        if dry_run:
            ok += 1
            continue
        try:
            # 每条独立事务
            with engine.begin() as conn:  # type: ignore[attr-defined]
                conn.exec_driver_sql(sql)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if any(k in msg for k in ("already exists", "Duplicate", "1061", "1050", "1826", "1060")):
                ok += 1
            else:
                fail += 1
                print(f"  [FAIL] {category}: {sql.splitlines()[0][:70]}")
                print(f"         {msg[:160]}")
    return ok, fail


def main() -> int:
    parser = argparse.ArgumentParser(description="生成并应用数据库约束")
    parser.add_argument("--apply", action="store_true", help="实际执行（默认只预览）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    parser.add_argument("--force-check", action="store_true", help="强制用 CHECK 模式")
    parser.add_argument("--force-trigger", action="store_true", help="强制用触发器模式")
    parser.add_argument("--show", type=int, default=0, help="打印前 N 条 SQL")
    parser.add_argument(
        "--admin-user",
        default=os.environ.get("MYSQL_ADMIN_USER", "root"),
        help="执行 DDL 的管理账号（创建触发器需要 SUPER 权限）",
    )
    parser.add_argument(
        "--admin-password",
        default=os.environ.get("MYSQL_ADMIN_PASSWORD", "root"),
        help="管理账号密码",
    )
    args = parser.parse_args()

    settings = get_settings()

    # ========================================================
    # 关键：DDL 用**管理账号**执行，不用应用账号
    # ========================================================
    # 为什么：
    #   1. 创建触发器需要 SUPER 权限（MySQL 在 binlog 开启时的限制），
    #      应用账号不应该有 SUPER —— 那会破坏最小权限原则
    #   2. 最小权限原则：应用账号只做 DML（增删改查），
    #      管理账号做 DDL（建表、约束、迁移）
    #   3. 这也符合生产实践：部署脚本用高权限账号，应用运行时用低权限账号
    admin_url = (
        f"mysql+pymysql://{args.admin_user}:{quote_plus(args.admin_password)}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"
        f"?charset=utf8mb4"
    )
    engine = create_engine(admin_url, connect_args=settings.db_connect_args_sync)

    with engine.begin() as conn:
        supports_check, version = detect_supports_check(conn)

        print("=" * 66)
        print("数据库约束生成")
        print("=" * 66)
        print(f"  目标库          : {settings.mysql_database}@{settings.mysql_host}:{settings.mysql_port}")
        print(f"  执行账号        : {args.admin_user}（DDL 权限）")
        print(f"  MySQL 版本      : {version}")
        print(f"  原生 CHECK 支持 : {'是' if supports_check else '否'}")
        print(f"  最低要求版本    : {'.'.join(str(p) for p in MIN_MYSQL_VERSION_FOR_CHECK)}")

        if args.force_check:
            use_check = True
        elif args.force_trigger:
            use_check = False
        else:
            use_check = supports_check

        mode_label = "原生 CHECK 约束" if use_check else "触发器（兼容模式）"
        print(f"  采用方式        : {mode_label}")

        if not supports_check and not args.force_trigger:
            print()
            print("  [WARN] 服务端低于 8.0.16，CHECK 约束**不会被执行**。")
            print("         已自动降级为触发器实现（对任何版本都生效，且对 root 也生效）。")

        plan = build_plan(use_check=use_check)

        print()
        print("  计划生成：")
        for cat, cnt in sorted(plan.summary().items()):
            print(f"    {cat:<20} {cnt:>3} 条")

        if args.show:
            print()
            print(f"  前 {args.show} 条 SQL：")
            for _, sql in plan.statements[: args.show]:
                print("    " + sql.splitlines()[0][:100])

        if not args.apply and not args.dry_run:
            print()
            print("  （预览模式。加 --apply 实际执行）")
            return 0

        if args.dry_run:
            print()
            print(f"  [DRY-RUN] 将执行 {len(plan.statements)} 条语句，未实际执行。")
            return 0

        print()
        print("  执行中...")
        dropped = drop_existing(engine, plan)
        if dropped:
            print(f"  已清理旧对象 {dropped} 个（避免残留错误的旧触发器）")
        ok, fail = apply_plan(engine, plan, dry_run=False)

        print()
        print(f"  结果：成功 {ok} 条，失败 {fail} 条")
        if fail:
            print("  [WARN] 存在失败项，请检查上方输出")
        else:
            print("  [ OK ] 全部约束已应用")

    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
