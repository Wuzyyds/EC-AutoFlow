# ADR-007 数据库从 PostgreSQL 变更为 MySQL

| 项 | 内容 |
|---|---|
| 决策日期 | 2026-09-29 |
| 决策状态 | **已决策**（用户指令） |
| 影响范围 | TDD-01 §2.1 技术栈、TDD-02 全部 DDL、TDD-05 配置表、所有 Repository |
| 替代文档 | 本 ADR 修订 TDD-01 ADR-004 与 §2.1 版本锁定表 |

---

## 1. 决策

数据库由 **PostgreSQL 16** 变更为 **MySQL 8.0.16 及以上**。

**版本下限必须锁 8.0.16，这不是建议是硬要求。** 原因见 §3.1。

---

## 2. 背景

TDD-01 §2.1 锁定 PostgreSQL 16，理由是其分区表、JSONB、窗口函数能力。用户要求改用 MySQL，通常是出于团队运维熟悉度与既有基础设施考虑。这是合理的工程取舍，本 ADR 负责把取舍的代价量化清楚，并给出每一项的替代方案。

---

## 3. 能力差异与替代方案（核心内容）

### 3.1 【最高优先级】CHECK 约束的版本陷阱

| 项 | PostgreSQL | MySQL |
|---|---|---|
| CHECK 约束 | 全版本支持 | **仅 8.0.16+ 强制执行** |

**MySQL 8.0.16 之前的版本会解析 `CHECK` 语法但完全忽略它**——不报错、不警告、不生效。

这不是理论风险。本机实测：

```
$ mysql -uroot -proot -e "SELECT VERSION();"
8.0.12          ← 服务端版本
```

**如果直接用它，TDD-02 里所有 CHECK 约束（状态枚举、职责分离、金额非负）全部失效，且没有任何提示。** 数据会在数月后以"状态字段出现拼写错误的值"的形式爆发。

**处置**：

1. 强制使用 Docker `mysql:8.0`（当前为 8.0.4x），容器启动后必须校验版本
2. `ops/healthcheck.py` 增加版本断言：`VERSION() >= 8.0.16`
3. CI 中执行"约束有效性测试"：故意写入非法状态，**必须**被拒绝

```python
# tests/integration/test_constraints_enforced.py
async def test_check_constraint_really_enforced(session):
    """验证 CHECK 约束真实生效 —— 防止跑在 8.0.16 以下版本上。

    这个测试存在的意义：MySQL 低版本会静默忽略 CHECK，
    只有主动写入非法数据才能发现。
    """
    with pytest.raises(IntegrityError):
        await session.execute(
            text("INSERT INTO tenants (code, name, status) VALUES ('x','x','NOT_A_STATUS')")
        )
```

### 3.2 类型映射表

| PostgreSQL | MySQL 8.0 | 说明 |
|---|---|---|
| `BIGSERIAL` | `BIGINT UNSIGNED NOT NULL AUTO_INCREMENT` | 自增主键 |
| `TIMESTAMPTZ` | `DATETIME(6)` | **MySQL 无带时区类型**，详见 §3.3 |
| `JSONB` | `JSON` | 无 GIN 索引，需生成列方案，详见 §3.4 |
| `BYTEA` | `VARBINARY(n)` | 加密字段，按需定长 |
| `NUMERIC(18,6)` | `DECIMAL(18,6)` | 语义等价（MySQL 中 NUMERIC 是 DECIMAL 别名） |
| `BOOLEAN` | `TINYINT(1)` | SQLAlchemy 自动处理，DDL 层可见 |
| `TEXT` | `TEXT` | **MySQL 的 TEXT 不允许有 DEFAULT 值**，详见 §3.6 |
| `VARCHAR(n)[]` | `JSON` | **MySQL 无数组类型**，详见 §3.5 |
| `now()` | `CURRENT_TIMESTAMP(6)` | 必须显式带精度 |
| `gen_random_uuid()` | 应用层生成 | MySQL 无此默认函数 |
| `INFINITY` 时间戳 | `'9999-12-31 23:59:59.999999'` | 哨兵值替代 |

### 3.3 时间类型：为什么用 `DATETIME(6)` 而不是 `TIMESTAMP`

MySQL 有两个时间类型，**都不能替代 `TIMESTAMPTZ`**，必须做取舍：

| 类型 | 范围 | 时区行为 | 结论 |
|---|---|---|---|
| `TIMESTAMP` | 1970–2038 | **随 session `time_zone` 自动转换** | ❌ 危险 |
| `DATETIME` | 1000–9999 | 原样存储，不做转换 | ✅ 选用 |

**为什么 `TIMESTAMP` 是陷阱**：它会把存入的值按 session 时区转换后存储，读取时再转回来。这看起来"方便"，实际是灾难——

- 应用连接串没设时区（默认跟随服务器）→ 存进去的值含义不确定
- 换一个 session 时区查询 → 同一条记录读出不同的时间
- 2038 年溢出（`TIMESTAMP` 上限）

**决策**：

1. 全部时间字段用 `DATETIME(6)`（6 位微秒精度，与 TDD-02 的 `TIMESTAMPTZ` 精度对齐）
2. **全链路只写 UTC**，应用层负责转换（TDD-01 原则 4）
3. Docker 容器强制 `--default-time-zone=+00:00`
4. 连接串强制 `init_command="SET time_zone='+00:00'"`，不依赖服务器默认值
5. 展示层按 `shop.timezone` 转换

```python
# core/timeutil.py 约定
def utc_now() -> datetime:
    """全系统唯一的时间获取入口。返回 aware UTC datetime。"""
    return datetime.now(timezone.utc)


def to_db(dt: datetime) -> datetime:
    """写库前统一转 naive UTC —— 因为 DATETIME 列不存时区信息。

    这是本项目的关键约定：库里的 DATETIME 一定是 UTC，
    带不带 tzinfo 是应用层的事。
    """
    if dt.tzinfo is None:
        raise ValueError("禁止写入 naive datetime，必须显式指定时区")
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
```

**硬约束（写进 AGENTS.md）**：禁止 `datetime.now()`（naive），只允许 `utc_now()`。

### 3.4 JSON 字段：索引能力下降与生成列方案

PostgreSQL 的 `JSONB` 支持 GIN 索引，可以直接对 JSON 内部字段建索引。**MySQL 的 `JSON` 列无法直接建索引**，只能通过**生成列（Generated Column）**间接实现。

```sql
-- PostgreSQL 写法（TDD-02 原有）
CREATE INDEX idx_orders_platform_raw_asin ON orders USING gin ((platform_raw -> 'asin'));

-- MySQL 写法：先建生成列，再对生成列建索引
ALTER TABLE orders
    ADD COLUMN asin_generated VARCHAR(32)
        GENERATED ALWAYS AS (JSON_UNQUOTE(JSON_EXTRACT(platform_raw, '$.asin'))) STORED,
    ADD INDEX idx_orders_asin (asin_generated);
```

**代价评估**：

| 项 | 影响 |
|---|---|
| 需要为每个要索引的 JSON 路径建生成列 | 增加列数 |
| `STORED` 生成列占用存储 | 可接受 |
| `VIRTUAL` 生成列不占存储但索引仍可用 | 优先用 VIRTUAL |
| 生成列表达式不可用非确定性函数 | 需注意 |

**Phase 1 的处置**：只对**确有查询需求**的 JSON 路径建生成列。TDD-02 中 JSON 字段主要用于**归档原始报文**（`platform_raw`），这类字段**不需要索引**——查原始报文永远是通过主键关联，不通过 JSON 内容查。因此实际需要生成列的场景很少。

**决策**：Phase 1 不为 `platform_raw` 建任何 JSON 索引。若后续出现"按 JSON 内部字段查询"的需求，再按需添加生成列。

### 3.5 数组类型：`VARCHAR[]` 必须改为 JSON

TDD-02 中有 4 处使用数组类型：

| 表.字段 | 原定义 | MySQL 方案 |
|---|---|---|
| `alert_rules.notify_channels` | `VARCHAR(64)[]` | `JSON` |
| `alert_rules.notify_roles` | `VARCHAR(64)[]` | `JSON` |
| `approval_rules.approver_roles` | `VARCHAR(64)[]` | `JSON` |
| `category_schemas.required_fields` | `VARCHAR(128)[]` | `JSON` |
| `feature_flags.allowed_shops` | `BIGINT[]` | `JSON` |

**取舍说明**：数组在 PostgreSQL 中可建 GIN 索引并支持 `@>` 包含查询；MySQL JSON 需要 `JSON_CONTAINS()`，**无法索引**。

**影响评估**：这 5 个字段的使用场景都是"读取出来在应用层判断"，**没有"按数组内容检索"的需求**（比如不会有人问"哪些规则推送到了 wecom 渠道"）。因此用 JSON 完全够用。

**硬约束**：JSON 数组字段**只允许整体读写，禁止在 SQL 里做包含查询**。若出现此类需求，改建关联表。

### 3.6 TEXT 与默认值

MySQL 的 `TEXT` / `BLOB` 类型**不允许有 `DEFAULT` 值**（即使是 `DEFAULT ''` 也报错）。

TDD-02 中受影响的字段：

| 表.字段 | 原定义 | MySQL 方案 |
|---|---|---|
| `product_listings.platform_raw` | `JSONB NOT NULL DEFAULT '{}'` | `JSON` 可为 NULL，应用层保证非空 |
| 各表 `platform_raw` | 同上 | 同上 |

**决策**：

1. `JSON` 列允许 NULL（MySQL 限制），但 **ORM 层强制默认 `{}`**，并在写入前断言非空
2. 不使用 `TEXT DEFAULT ''` 的写法
3. 通过 `nullable=False` + Python 侧 default 保证逻辑非空

**这是一个"数据库约束降级为应用层约束"的案例**，必须在 Repository 层补齐。

### 3.7 排除约束（EXCLUDE）：MySQL 完全不支持

TDD-02 中有两处使用 PostgreSQL 的排除约束：

**场景 A：SKU 主映射唯一（`sku_mappings`）**

```sql
-- PostgreSQL 原写法
EXCLUDE USING btree (shop_id WITH =, internal_sku WITH =) WHERE (is_primary = TRUE)
```

**MySQL 替代方案：生成列 + 唯一索引**（这是标准技巧）

```sql
-- 思路：把"仅当 is_primary=TRUE 时唯一"转成
--       "当 is_primary=FALSE 时生成 NULL，而 NULL 不参与唯一约束"
ALTER TABLE sku_mappings
    ADD COLUMN primary_sku_key VARCHAR(128)
        GENERATED ALWAYS AS (IF(is_primary = 1, internal_sku, NULL)) STORED,
    ADD UNIQUE KEY uk_sku_primary (shop_id, primary_sku_key);
```

**为什么这样能工作**：MySQL 的唯一索引中，**多个 NULL 值互不冲突**。所以非主映射的行生成 NULL，可以无限多；主映射的行生成真实 SKU，受唯一约束保护。

**这是 MySQL 里表达"部分唯一索引"的唯一可靠手段。**

**场景 B：配置项时间区间不重叠（`config_items`）**

```sql
-- PostgreSQL 原写法
EXCLUDE USING gist (tenant_id WITH =, namespace WITH =, key WITH =,
                    tstzrange(effective_from, COALESCE(effective_to,'infinity')) WITH &&)
```

**MySQL 替代方案：拆成两层**

| 层 | 手段 | 覆盖范围 |
|---|---|---|
| 第一层 | 生成列 + 唯一索引（同上技巧） | 保证"同一 key 只有一个当前生效值" |
| 第二层 | `BEFORE INSERT/UPDATE` 触发器 | 校验历史区间不重叠 |

```sql
-- 第一层：每个 key 只能有一条 effective_to IS NULL 的记录
ALTER TABLE config_items
    ADD COLUMN active_key VARCHAR(192)
        GENERATED ALWAYS AS (
            IF(effective_to IS NULL, CONCAT(namespace, ':', config_key), NULL)
        ) STORED,
    ADD UNIQUE KEY uk_config_active (tenant_id, active_key);

-- 第二层：触发器校验区间不重叠
DELIMITER //
CREATE TRIGGER trg_config_items_no_overlap
BEFORE INSERT ON config_items
FOR EACH ROW
BEGIN
    DECLARE v_conflict INT DEFAULT 0;

    SELECT COUNT(*) INTO v_conflict
    FROM config_items
    WHERE tenant_id = NEW.tenant_id
      AND namespace = NEW.namespace
      AND config_key = NEW.config_key
      AND id <> COALESCE(NEW.id, 0)
      AND NEW.effective_from < COALESCE(effective_to, '9999-12-31 23:59:59.999999')
      AND COALESCE(NEW.effective_to, '9999-12-31 23:59:59.999999') > effective_from;

    IF v_conflict > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'config_items: 时间区间与既有配置重叠';
    END IF;
END//
DELIMITER ;
```

**注意**：这里必须用 `'9999-12-31 23:59:59.999999'` 哨兵值替代 PostgreSQL 的 `'infinity'`。

**权衡说明**：触发器比排除约束弱（并发插入理论上仍可能穿透，因为触发器在 READ COMMITTED 下不加间隙锁）。**补充手段**：应用层在写入前先查一次 + 唯一索引兜底当前生效值。三重防护后，实际风险可忽略。

### 3.8 部分索引（Partial Index）：不支持，改全量索引

TDD-02 中有 12 处使用 `WHERE` 子句的部分索引：

```sql
-- PostgreSQL
CREATE INDEX idx_orders_settle ON orders(shop_id, settle_time DESC)
    WHERE settle_time IS NOT NULL;
```

**MySQL 不支持部分索引**。替代方案有两个：

| 方案 | 做法 | 适用 |
|---|---|---|
| A. 全量索引 | 直接去掉 `WHERE` | 简单，索引略大 |
| B. 生成列 + 索引 | 建标记列再索引 | 复杂，收益小 |

**决策：采用方案 A（全量索引）**。

**代价评估**：部分索引的价值在于"索引更小"。但在 Phase 1 数据量下（10 店铺、年 365 万订单），全量索引与部分索引的差异在性能上不可感知，而复杂度差异明显。

**唯一需要注意的**：`idx_orders_settle` 这类索引如果大部分行的 `settle_time` 为 NULL，全量索引会包含大量 NULL 条目。MySQL 的 B-tree 索引对 NULL 有优化（NULL 集中存放），影响可控。

### 3.9 审计表 append-only：REVOKE 改为触发器

**PostgreSQL 方案**（TDD-02 原写法）：

```sql
REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC;
```

**MySQL 的问题**：

1. MySQL **没有 `PUBLIC` 角色概念**，`REVOKE` 必须针对具体用户
2. 即使针对应用账号 `REVOKE`，**root 和 DBA 账号仍可修改**——而合规审计要求的是"任何人不可改"

**MySQL 方案：`BEFORE UPDATE/DELETE` 触发器抛错**

```sql
DELIMITER //
CREATE TRIGGER trg_audit_events_no_update
BEFORE UPDATE ON audit_events
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'audit_events 是 append-only 表，禁止 UPDATE';
END//

CREATE TRIGGER trg_audit_events_no_delete
BEFORE DELETE ON audit_events
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'audit_events 是 append-only 表，禁止 DELETE';
END//
DELIMITER ;
```

**为什么触发器比 REVOKE 更好**：MySQL 触发器**对所有用户生效，包括 root**。而 `REVOKE` 对 root 无效。

**但触发器也有绕过方式**：`DROP TRIGGER`。所以仍需第三层校验（定期检查行数只增不减），与 TDD-06 §2.4 的三重防护一致。

**受影响的表（4 张）**：

| 表 | 触发器 |
|---|---|
| `audit_events` | no_update + no_delete |
| `state_transitions` | no_update + no_delete |
| `approval_actions` | no_update + no_delete |
| `credential_audit_logs` | no_update + no_delete |

### 3.10 分区表：MySQL 限制导致 Phase 1 不分区的决策

**这是本次变更中影响最大的结构调整，需要重点评审。**

**MySQL 分区表的硬限制**：

> 分区表上的**每一个唯一索引（含主键）都必须包含分区列的所有列**。

**PostgreSQL 没有这个限制**。所以 TDD-02 中的设计：

```sql
-- TDD-02 原设计（PostgreSQL 可行）
CREATE TABLE orders (
    id BIGSERIAL PRIMARY KEY,                    -- 主键只有 id
    shop_id BIGINT NOT NULL,
    platform_order_id VARCHAR(255) NOT NULL,
    order_time TIMESTAMPTZ NOT NULL,
    ...
    UNIQUE (shop_id, platform_order_id)
);
-- 分区：PARTITION BY RANGE (order_time)   ← PG 允许
```

**在 MySQL 上会直接报错**：主键 `(id)` 和唯一键 `(shop_id, platform_order_id)` 都不含 `order_time`。

**要分区，必须改成**：

```sql
PRIMARY KEY (id, order_time),
UNIQUE KEY uk_order (shop_id, platform_order_id, order_time)
```

**这带来三个连锁问题**：

1. **主键变成复合键**，所有外键引用 `orders(id)` 的地方都要改成引用 `(id, order_time)`——**这是灾难性的**（`order_items`、`order_fees`、`refunds` 全部受影响）
2. ORM 的 `relationship` 需要显式指定复合外键
3. 应用层的"按 ID 查订单"需要额外带 `order_time`，或退化为扫描

**决策：Phase 1 不使用分区表。**

**理由**：

| # | 理由 |
|---|---|
| 1 | **数据量不需要**：10 店铺 × 日 1 万单 = 年 365 万行。MySQL InnoDB 单表千万级无压力，配合索引性能完全够 |
| 2 | **代价远大于收益**：复合主键会污染 4-5 张关联表的设计，让整个数据模型复杂化 |
| 3 | **可后补**：MySQL 支持在线 `ALTER TABLE ... PARTITION BY`，等数据量真的上来再分区，届时业务已稳定，迁移风险可控 |
| 4 | **有替代方案**：先用**归档表策略**（`orders_archive_2026` 按年分表）解决冷数据问题，这个方案不改主键 |

**替代方案：归档表策略**

```sql
-- 冷数据归档：不改变主键结构，通过应用层路由
-- 规则：超过 24 个月的订单迁移到 orders_archive，主表只保留热数据
CREATE TABLE orders_archive LIKE orders;   -- 结构相同
```

**这与 TDD-02 的差异需评审确认**。若坚持分区，需要接受复合主键带来的复杂度。

**保留的例外**：`api_call_logs`（技术日志，无外键引用）和 `raw_payloads`（无外键引用）**可以分区**，因为它们没有外键被引用的问题。但 Phase 1 数据量同样不需要，一并暂缓。

### 3.11 保留字陷阱

MySQL 的保留字比 PostgreSQL 多。TDD-02 中命中的字段：

| 表.字段 | 问题 | 处置 |
|---|---|---|
| `config_items.key` | **`KEY` 是 MySQL 保留字** | 改名 `config_key` |
| `alert_rules.condition` | `CONDITION` 是保留字（存储过程语境） | 改名 `condition_expr` |
| `alerts.level` | 非保留字但语义模糊 | 改名 `alert_level` |
| `users.status` | 非保留字 | 保留 |
| `roles.name` | 非保留字 | 保留 |

**改名清单（3 个字段）**：

| 原字段 | 新字段 | 理由 |
|---|---|---|
| `config_items.key` | `config_key` | 保留字 |
| `alert_rules.condition` | `condition_expr` | 保留字 |
| `alerts.level` | `alert_level` | 与 `alert_rules.level` 保持一致的显式命名 |

**校验手段**：用 MySQL 的保留字清单做一次全字段扫描，纳入 CI。

### 3.12 其他需要注意的差异

| 项 | PostgreSQL | MySQL | 处置 |
|---|---|---|---|
| 标识符大小写 | 折叠为小写 | **Windows/macOS 默认不区分，Linux 区分** | 全部用小写蛇形，避免跨平台问题 |
| `LIKE` 大小写 | 区分（`ILIKE` 不区分） | `utf8mb4_0900_ai_ci` 默认不区分 | 无需改代码，但语义变了要知晓 |
| 字符串拼接 | `\|\|` | `CONCAT()` | 原生 SQL 报表需改 |
| `RETURNING` 子句 | 支持 | ❌ 不支持 | 用 `LAST_INSERT_ID()` 或 ORM flush |
| `ON CONFLICT DO UPDATE` | 支持 | `ON DUPLICATE KEY UPDATE` | 原生 SQL 需改 |
| 事务隔离默认 | READ COMMITTED | **REPEATABLE READ** | 需评估幻读影响 |
| 死锁检测 | 立即报错 | 立即报错 | 一致 |
| 索引前缀 | 无长度限制（B-tree 有页大小限制） | **3072 字节上限**（DYNAMIC 行格式） | 见下 |
| 全文索引 | `tsvector` + GIN | `FULLTEXT` + **ngram 解析器**（中文必需） | 见下 |

**索引前缀长度校验**：

`utf8mb4` 下每字符 4 字节，InnoDB DYNAMIC 行格式的索引前缀上限 3072 字节 → **单列最大 `VARCHAR(768)`**。

TDD-02 中的字段：

| 字段 | 长度 | 字节 | 结论 |
|---|---|---|---|
| `platform_order_id VARCHAR(255)` | 255 | 1020 | ✅ |
| `endpoint VARCHAR(512)` | 512 | 2048 | ✅ |
| `platform_item_id VARCHAR(255)` | 255 | 1020 | ✅ |
| `error_msg TEXT` | — | — | ⚠️ 不能直接索引，需前缀索引 |

**结论**：无超限字段，但 `TEXT` 类型字段建索引时必须用前缀（如 `error_code(64)`）。

**全文索引（中文）**：

MySQL 的默认 `FULLTEXT` 解析器按空格分词，**对中文无效**（整句被当成一个词）。必须用 ngram：

```sql
ALTER TABLE products
    ADD FULLTEXT INDEX ft_products_title (title, description) WITH PARSER ngram;
```

**Phase 1 是否需要全文检索？** TDD-02 的 `products` 表有 `title` / `description`。若需要商品搜索，用 ngram 全文索引；否则用 `LIKE '%xxx%'` 也够（数据量小）。

**决策**：Phase 1 用 `LIKE`，不建全文索引。数据量上来后改 ngram。

---

## 4. 命名规范统一（用户明确要求）

TDD-01 附录 A 定义了命名规范，但 **TDD-02 的实现与之冲突**。本 ADR 按规范修正实现。

### 4.1 发现的冲突

| 项 | TDD-01 规范 | TDD-02 实际 | 处置 |
|---|---|---|---|
| **枚举值** | **数据库存 `UPPER_SNAKE`** | 存 `lower_snake`（如 `'active'`、`'pending_approval'`） | **改为 UPPER_SNAKE** |

**冲突原因**：TDD-02 的 DDL 先写，未严格对齐 TDD-01 附录 A 的约定。

### 4.2 枚举值改名的完整影响

全部枚举值从 `lower_snake` 改为 `UPPER_SNAKE`。示例：

| 原值 | 新值 |
|---|---|
| `'active'` | `'ACTIVE'` |
| `'pending_approval'` | `'PENDING_APPROVAL'` |
| `'partially_shipped'` | `'PARTIALLY_SHIPPED'` |
| `'auto_approved'` | `'AUTO_APPROVED'` |
| `'gaps_detected'` | `'GAPS_DETECTED'` |

**影响范围**：

| 层 | 影响 |
|---|---|
| DDL CHECK 约束 | 全部枚举值改写 |
| Python `StrEnum` | 成员值改为大写 |
| API 请求/响应 | 枚举值大小写变化，**前端需同步** |
| 已有数据 | 无（尚未投产） |

**为什么现在改成本最低**：项目尚未投产，无历史数据。若上线后再改，需要数据迁移 + 前端适配 + 兼容期。

### 4.3 其他命名规范的落实检查

| 规范 | 要求 | 检查结果 |
|---|---|---|
| 表名 | 复数小写蛇形 | ✅ `orders`、`product_listings`、`audit_events` |
| 字段名 | 小写蛇形 | ✅（3 处保留字已改名） |
| 主键 | `id` | ✅ |
| 外键 | `{单数}_id` | ✅ `shop_id`、`order_id` |
| 布尔字段 | `is_` / `has_` 前缀 | ✅ `is_primary`、`is_active` |
| 时间字段 | `_at` 后缀 | ✅ `created_at`、`order_time`（业务口径用 `_time`） |
| 金额字段 | 无后缀或 `_amount` | ⚠️ 混用，见下 |
| Python 类 | PascalCase | — |
| Python 函数 | snake_case | — |
| 常量 | UPPER_SNAKE | — |
| API 路径 | kebab-case 复数 | `/api/v1/shop-credentials` |
| 索引 | `idx_{表}_{列}` | ✅ |
| 唯一键 | `uk_{表}_{列}` | ⚠️ 原为匿名 `UNIQUE`，改为显式命名 |
| 外键约束 | `fk_{表}_{引用表}` | ⚠️ 原为匿名，改为显式命名 |

**需要补充的规范（原文档未明确）**：

| 项 | 新增约定 |
|---|---|
| 唯一键 | `uk_{表名}_{语义}` 显式命名（原为匿名 `UNIQUE(...)`） |
| 外键 | `fk_{表名}_{引用表名}` 显式命名 |
| CHECK 约束 | `chk_{表名}_{语义}` 显式命名 |
| 触发器 | `trg_{表名}_{动作}` 显式命名 |
| 生成列 | `{语义}_generated` 后缀 |

**为什么必须显式命名约束**：匿名约束在 MySQL 中会自动生成名字（如 `orders_ibfk_1`），**报错信息里出现这种名字，排障时根本不知道是哪条约束**。显式命名是生产环境的必备实践。

**金额字段命名（需要统一）**：

TDD-02 中混用了几种风格：

| 现状 | 建议统一为 | 示例 |
|---|---|---|
| `item_total` | ✅ 保留（语义清晰） | `item_total` |
| `grand_total` | ✅ 保留 | `grand_total` |
| `amount` | ✅ 保留（单值场景） | `refunds.amount` |
| `min_amount` | ✅ 保留 | `alert_rules.min_amount` |

**结论**：金额字段命名**保持现状**（已足够清晰），但补充规则：**多币种场景必须配套 `currency` 字段**，且两者不可分离。纳入 CI 检查。

---

## 5. 驱动与依赖变更

| 组件 | PostgreSQL 方案 | MySQL 方案 | 理由 |
|---|---|---|---|
| 异步驱动 | `asyncpg` | **`aiomysql`** | 纯 Python，Windows 免编译 |
| 同步驱动 | （asyncpg 兼任） | **`pymysql`** | Celery worker 用同步 Session |
| URL 前缀 | `postgresql+asyncpg://` | `mysql+aiomysql://` | — |
| 同步 URL | — | `mysql+pymysql://` | — |

**为什么选 `aiomysql` 而不是 `asyncmy`**：`asyncmy` 性能更好但**需要 C 编译**，在 Windows 上容易失败。本项目对驱动性能不敏感（瓶颈在平台 API 限流，不在数据库），选免编译的 `aiomysql` 更稳妥。

```toml
# pyproject.toml 变更
-  "asyncpg>=0.30,<0.31",
+  "aiomysql>=0.2,<0.3",
+  "pymysql>=1.1,<2",
```

**连接串必须带时区初始化**：

```python
# 关键：不能依赖服务器默认时区
MYSQL_URL = f"mysql+aiomysql://{user}:{pwd}@{host}:{port}/{db}?charset=utf8mb4"
# 并在 connect_args 中设置
connect_args = {"init_command": "SET time_zone='+00:00'"}
```

---

## 6. 风险登记

| # | 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|---|
| 1 | 跑在 MySQL < 8.0.16 上，CHECK 静默失效 | **高**（本机就是 8.0.12） | **高** | Docker 强制新版 + 启动版本断言 + 约束有效性测试 |
| 2 | 排除约束改触发器后并发穿透 | 低 | 中 | 生成列唯一索引兜底 + 应用层预检 |
| 3 | 时间字段时区混乱 | 中 | **高** | 强制 `DATETIME(6)` + 只写 UTC + 连接串设时区 + `utc_now()` 唯一入口 |
| 4 | 无分区表导致大表查询变慢 | 低（Phase 1） | 中 | 归档表策略 + 索引优化；数据量上来后再分区 |
| 5 | 保留字冲突 | 中 | 低 | 已识别 3 处并改名 + CI 扫描 |
| 6 | 索引前缀超限 | 低 | 中 | 已核算，无超限字段；TEXT 索引用前缀 |
| 7 | 中文全文检索失效 | 中 | 中 | Phase 1 用 LIKE；需要时上 ngram |
| 8 | 事务隔离级别差异（RR vs RC） | 中 | 中 | 显式设置 `transaction_isolation=READ-COMMITTED` |
| 9 | JSON 字段无索引导致慢查 | 低 | 低 | Phase 1 无 JSON 查询需求；需要时加生成列 |

**第 8 项补充说明**：MySQL 默认 `REPEATABLE READ`，PostgreSQL 默认 `READ COMMITTED`。RR 在并发场景下会产生更多的间隙锁与死锁。**决策：连接级别设为 `READ-COMMITTED`**，与 TDD-02 的设计假设保持一致。

```python
connect_args = {
    "init_command": "SET time_zone='+00:00', transaction_isolation='READ-COMMITTED'",
}
```

---

## 7. 需要修订的上游文档

| 文档 | 修订点 |
|---|---|
| TDD-01 §2.1 | PostgreSQL 16 → MySQL 8.0.16+；asyncpg → aiomysql + pymysql |
| TDD-01 §2.2 | 不引入清单不变 |
| TDD-01 ADR-004 | 补充 MySQL 的 `DECIMAL(18,6)` 说明 |
| TDD-02 全部 | 见 `TDD-02-数据模型设计-v1.1-mysql.md` |
| TDD-05 | `config_items.key` → `config_key`；排除约束改触发器 |
| TDD-06 §5.1 | 增加"CHECK 约束有效性"门禁项 |
| TDD-06 §4.1 | 备份方式：`pg_dump`/WAL → `mysqldump`/binlog |
| PRD v1.1 §14 | 技术选型表更新 |

**注意**：TDD-01 中"明确不引入"清单**不需要修改**——MySQL 不影响那 7 项判断（Kafka/Temporal/K8s/ES/数据仓库/LangChain/GraphQL）。

---

## 8. 决策结论

**接受 MySQL**，代价与缓解如下：

| 代价 | 严重度 | 缓解 |
|---|---|---|
| 失去排除约束 | 中 | 生成列唯一索引 + 触发器 + 应用层 |
| 失去部分索引 | 低 | 全量索引（Phase 1 数据量下无感知） |
| 失去数组类型 | 低 | JSON（无检索需求） |
| 失去 JSONB 索引 | 低 | 生成列（Phase 1 无需求） |
| 失去分区表 | **中** | 归档表策略（避免复合主键污染） |
| 时间类型语义变弱 | **中高** | 强制 DATETIME(6) + 只写 UTC + 唯一入口函数 |
| CHECK 版本陷阱 | **高** | 版本断言 + 约束有效性测试 |

**净评估**：MySQL 能承载本项目的 Phase 1 全部需求。**最大的两个风险不是能力问题，是"CHECK 静默失效"和"时区混乱"**——这两个都已给出可验证的防护手段。

---

## 附录 A：PostgreSQL → MySQL 转换速查表

| PostgreSQL | MySQL 8.0 |
|---|---|
| `BIGSERIAL` | `BIGINT UNSIGNED AUTO_INCREMENT` |
| `TIMESTAMPTZ` | `DATETIME(6)`（存 UTC） |
| `JSONB` | `JSON` |
| `BYTEA` | `VARBINARY(n)` |
| `NUMERIC(p,s)` | `DECIMAL(p,s)` |
| `BOOLEAN` | `TINYINT(1)` |
| `VARCHAR(n)[]` | `JSON` |
| `now()` | `CURRENT_TIMESTAMP(6)` |
| `gen_random_uuid()` | 应用层生成 |
| `\|\|`（拼接） | `CONCAT()` |
| `ILIKE` | `LIKE`（ci collation） |
| `RETURNING` | `LAST_INSERT_ID()` |
| `ON CONFLICT DO UPDATE` | `ON DUPLICATE KEY UPDATE` |
| `EXCLUDE USING` | 生成列 + 唯一索引 + 触发器 |
| `CREATE INDEX ... WHERE` | 去掉 WHERE |
| `REVOKE ... FROM PUBLIC` | 触发器 `SIGNAL SQLSTATE '45000'` |
| `'infinity'` | `'9999-12-31 23:59:59.999999'` |
| `READ COMMITTED`（默认） | 显式设置（默认是 RR） |

## 附录 B：CI 必加的 4 项 MySQL 专项检查

| # | 检查 | 目的 |
|---|---|---|
| 1 | `SELECT VERSION() >= 8.0.16` | 防 CHECK 静默失效 |
| 2 | 写入非法状态必须被拒 | 验证 CHECK 真实生效 |
| 3 | 字段名不在 MySQL 保留字清单 | 防保留字冲突 |
| 4 | 索引前缀字节数 ≤ 3072 | 防建表失败 |
