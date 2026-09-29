# TDD-02 · 数据模型设计

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 上游文档 | `TDD-01-技术设计总纲.md`、`PRD-v1.1` 第 15 章 |
| 优先级 | **阻塞级**——本册未评审通过，不得开始编码 |
| 数据库 | PostgreSQL 16 |

---

## 0. 通用约定

### 0.1 命名与类型规范

| 项 | 规范 |
|---|---|
| 表名 | 复数、小写、蛇形：`orders`、`product_listings` |
| 主键 | `id BIGSERIAL PRIMARY KEY`（Phase 1 用自增，避免 UUID 索引膨胀） |
| 金额 | `NUMERIC(18,6)`，Python `Decimal` |
| 时间 | `TIMESTAMPTZ`，存 UTC |
| 布尔 | `BOOLEAN NOT NULL DEFAULT FALSE` |
| 枚举 | `VARCHAR(32)` + CHECK 约束（不用 PostgreSQL ENUM，改起来要 ALTER TYPE） |
| 平台差异字段 | `platform_raw JSONB` |
| 软删除 | `deleted_at TIMESTAMPTZ`，NULL 表示未删 |

### 0.2 每张表必备字段

```sql
tenant_id    BIGINT NOT NULL,            -- 租户隔离（Phase 1 固定为 1）
created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
```

**说明**：`tenant_id` 即使当前单租户也必须加（见 TDD-01 ADR-006）。所有索引的第一列建议以 `tenant_id` 或 `shop_id` 开头，保证多租户下的查询效率。

### 0.3 updated_at 自动维护

```sql
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

对需要 `updated_at` 的表统一挂触发器，避免依赖应用层。

---

## 1. 表清单总览（45 张）

| 分组 | 表 | Phase 1 |
|---|---|---|
| 租户与权限 | `tenants`、`users`、`roles`、`user_roles`、`permissions`、`role_permissions` | ✅ |
| 店铺与凭据 | `shops`、`shop_credentials`、`credential_audit_logs` | ✅ |
| 商品 | `products`、`product_variants`、`product_attributes`、`product_listings`、`platform_categories`、`category_schemas` | ✅ |
| SKU 映射 | `sku_mappings` | ✅ |
| 价格 | `price_history`、`pricing_rules` | ✅（规则表预留） |
| 订单 | `orders`、`order_items`、`refunds`、`order_fees` | ✅ |
| 库存 | `inventory_snapshots`、`inventory_alerts`、`inventory_policies` | ✅ |
| 广告 | `ad_campaigns`、`ad_groups`、`ad_reports` | ⚠️ 建表 |
| 客服 | `customer_messages`、`message_classifications`、`after_sales_cases` | ⚠️ 建表 |
| 选品 | `selection_candidates`、`selection_reviews`、`scoring_configs` | ⚠️ 建表 |
| 竞品 | `competitors`、`competitor_items`、`competitor_snapshots`、`competitor_changes` | ⚠️ 建表 |
| 财务 | `cost_items`、`cost_rules`、`settlement_records`、`settlement_fees`、`profit_snapshots`、`exchange_rates`、`accounting_periods` | ✅ |
| 审批与审计 | `approvals`、`approval_actions`、`audit_events` | ✅ |
| 预警 | `alerts`、`alert_rules` | ✅ |
| 报表 | `report_jobs`、`report_subscriptions` | ✅ |
| 任务与同步 | `task_runs`、`sync_cursors`、`sync_watermarks`、`raw_payloads`、`api_call_logs` | ✅ |
| 配置 | `metric_definitions`、`system_configs`、`feature_flags` | ✅ |
| AI | `ai_invocations`、`ai_eval_datasets` | ⚠️ 建表 |

**✅ = Phase 1 实现业务逻辑；⚠️ = 建表但方法抛 NotImplementedError（见 TDD-01 5.3）**

---

## 2. 租户与权限

```sql
-- 租户
CREATE TABLE tenants (
    id          BIGSERIAL PRIMARY KEY,
    code        VARCHAR(64)  NOT NULL UNIQUE,
    name        VARCHAR(255) NOT NULL,
    status      VARCHAR(32)  NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','suspended','deleted')),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 用户
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id),
    username      VARCHAR(128) NOT NULL,
    email         VARCHAR(255),
    password_hash VARCHAR(255) NOT NULL,       -- bcrypt/argon2
    display_name  VARCHAR(128),
    status        VARCHAR(32) NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','disabled','locked')),
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, username)
);

-- 角色
CREATE TABLE roles (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id),
    code        VARCHAR(64) NOT NULL,   -- owner/operator/cs/finance/sre/auditor
    name        VARCHAR(128) NOT NULL,
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code)
);

CREATE TABLE user_roles (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id     BIGINT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    shop_scope  BIGINT[],          -- NULL=全部店铺；否则限定店铺
    granted_by  BIGINT REFERENCES users(id),
    expires_at  TIMESTAMPTZ,       -- 临时授权到期时间（PRD 3.3）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, role_id)
);

-- 权限点（细粒度，对应 PRD 3.3 的"资源 × 动作"）
CREATE TABLE permissions (
    id          BIGSERIAL PRIMARY KEY,
    code        VARCHAR(128) NOT NULL UNIQUE,  -- listing:create / price:update ...
    resource    VARCHAR(64)  NOT NULL,
    action      VARCHAR(64)  NOT NULL,
    risk_level  VARCHAR(16)  NOT NULL DEFAULT 'low'
                CHECK (risk_level IN ('low','medium','high','critical')),
    description TEXT
);

CREATE TABLE role_permissions (
    role_id       BIGINT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id BIGINT NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

CREATE INDEX idx_user_roles_user ON user_roles(tenant_id, user_id);
CREATE INDEX idx_user_roles_expiry ON user_roles(expires_at) WHERE expires_at IS NOT NULL;
```

**设计要点**：

- `user_roles.shop_scope` 支持"同一用户在不同店铺有不同角色"
- `expires_at` 支撑临时授权与自动回收（PRD 3.3 要求）
- `permissions.risk_level` 与审批矩阵联动：high/critical 权限的操作必须走审批

---

## 3. 店铺与凭据

```sql
CREATE TABLE shops (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        BIGINT NOT NULL REFERENCES tenants(id),
    platform         VARCHAR(32) NOT NULL
                     CHECK (platform IN ('amazon','tiktok','temu','shein',
                                          'shopee','lazada','aliexpress','shopify')),
    region           VARCHAR(16) NOT NULL,     -- na/eu/fe/us/cn/sea...
    shop_name        VARCHAR(255) NOT NULL,
    shop_external_id VARCHAR(255) NOT NULL,    -- 平台侧店铺 ID
    marketplace_ids  VARCHAR(64)[],            -- Amazon 站点
    fulfillment_type VARCHAR(32) NOT NULL DEFAULT 'fba'
                     CHECK (fulfillment_type IN ('fba','fbm','mfn','semi','full','na')),
    currency         VARCHAR(8)  NOT NULL DEFAULT 'USD',
    timezone         VARCHAR(64) NOT NULL DEFAULT 'UTC',
    status           VARCHAR(32) NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','inactive','revoked','error')),
    capabilities     JSONB NOT NULL DEFAULT '{}',  -- 能力矩阵缓存（TDD-03）
    last_sync_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, platform, shop_external_id)
);

CREATE INDEX idx_shops_tenant_status ON shops(tenant_id, status);

-- 凭据（加密存储，PRD 15.3 / 10.3）
CREATE TABLE shop_credentials (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    shop_id         BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    credential_type VARCHAR(32) NOT NULL
                    CHECK (credential_type IN ('access_token','refresh_token',
                                                'app_secret','client_id',
                                                'lwa_client_id','lwa_client_secret')),
    ciphertext      BYTEA NOT NULL,          -- AES-256-GCM 密文
    nonce           BYTEA NOT NULL,
    key_version     SMALLINT NOT NULL DEFAULT 1,   -- 支持密钥轮换
    expires_at      TIMESTAMPTZ,
    last_refreshed_at TIMESTAMPTZ,
    status          VARCHAR(32) NOT NULL DEFAULT 'valid'
                    CHECK (status IN ('valid','expiring','expired','revoked')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, credential_type)
);

CREATE INDEX idx_credentials_expiry ON shop_credentials(expires_at)
    WHERE status IN ('valid','expiring');

-- 凭据操作审计（谁在何时读取/轮换）
CREATE TABLE credential_audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT NOT NULL,
    shop_id      BIGINT NOT NULL,
    credential_id BIGINT,
    action       VARCHAR(32) NOT NULL
                 CHECK (action IN ('create','read','rotate','revoke','refresh_fail')),
    actor_id     BIGINT,                 -- NULL 表示系统
    actor_type   VARCHAR(16) NOT NULL CHECK (actor_type IN ('user','system')),
    reason       TEXT,
    ip_address   INET,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_cred_audit_shop ON credential_audit_logs(shop_id, created_at DESC);
```

**安全要点（必须实现）**：

- `ciphertext` 用 AES-256-GCM，密钥来自环境变量，**永不落库**
- `key_version` 支持密钥轮换：轮换时旧密钥仍能解密历史数据
- **读取凭据必须写审计**（`credential_audit_logs`），这是 PRD 3.4 的强制要求
- 任何 API 响应中**禁止返回明文凭据**，只返回 `status` 与 `expires_at`

---

## 4. 商品

```sql
-- 商品主表（我方视角）
CREATE TABLE products (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    internal_sku  VARCHAR(128) NOT NULL,        -- 我方主 SKU，全局唯一
    title         VARCHAR(512) NOT NULL,
    brand         VARCHAR(255),
    category_path VARCHAR(512),                 -- 内部类目路径
    status        VARCHAR(32) NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','active','archived')),
    attributes    JSONB NOT NULL DEFAULT '{}',  -- 规范化属性
    created_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ,
    UNIQUE (tenant_id, internal_sku)
);

-- 变体（父子结构）
CREATE TABLE product_variants (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    product_id      BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    variant_sku     VARCHAR(128) NOT NULL,
    parent_variant_id BIGINT REFERENCES product_variants(id),
    variation_theme JSONB NOT NULL DEFAULT '{}',  -- {"color":"Red","size":"L"}
    is_parent       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, variant_sku)
);

CREATE INDEX idx_variants_product ON product_variants(product_id);

-- 属性明细（支持按属性筛选与追溯来源）
CREATE TABLE product_attributes (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL,
    product_id  BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    attr_key    VARCHAR(128) NOT NULL,
    attr_value  TEXT,
    value_type  VARCHAR(32) NOT NULL DEFAULT 'string'
                CHECK (value_type IN ('string','number','boolean','list','date')),
    source      VARCHAR(32) NOT NULL DEFAULT 'manual'
                CHECK (source IN ('manual','ai','platform','supplier','import')),
    confidence  NUMERIC(4,3),          -- AI 来源时有值
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (product_id, attr_key)
);

-- 商品在平台的刊登实例（一个商品可铺多店铺）
CREATE TABLE product_listings (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT NOT NULL,
    product_id        BIGINT NOT NULL REFERENCES products(id),
    variant_id        BIGINT REFERENCES product_variants(id),
    shop_id           BIGINT NOT NULL REFERENCES shops(id),
    platform_sku      VARCHAR(255),             -- 平台侧 SKU
    platform_item_id  VARCHAR(255),             -- ASIN / item_id
    parent_item_id    VARCHAR(255),             -- 变体父 ID
    listing_status    VARCHAR(32) NOT NULL DEFAULT 'draft'
                      CHECK (listing_status IN ('draft','validating','pending_approval',
                                                'rejected','queued','submitted',
                                                'processing','active','partial_active',
                                                'inactive','failed','deleted')),
    last_error_code   VARCHAR(128),
    last_error_msg    TEXT,
    submission_id     VARCHAR(255),             -- Feed ID / task ID
    price             NUMERIC(18,6),
    currency          VARCHAR(8),
    quantity          INTEGER,
    raw_payload_id    BIGINT,                   -- → raw_payloads.id
    platform_raw      JSONB NOT NULL DEFAULT '{}',
    published_at      TIMESTAMPTZ,
    last_synced_at    TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, platform_sku)
);

CREATE INDEX idx_listings_product ON product_listings(tenant_id, product_id);
CREATE INDEX idx_listings_shop_status ON product_listings(shop_id, listing_status);
CREATE INDEX idx_listings_item ON product_listings(shop_id, platform_item_id);
CREATE INDEX idx_listings_submission ON product_listings(submission_id)
    WHERE submission_id IS NOT NULL;

-- 平台类目缓存
CREATE TABLE platform_categories (
    id            BIGSERIAL PRIMARY KEY,
    platform      VARCHAR(32) NOT NULL,
    region        VARCHAR(16) NOT NULL,
    category_id   VARCHAR(128) NOT NULL,
    parent_id     VARCHAR(128),
    name          VARCHAR(512) NOT NULL,
    is_leaf       BOOLEAN NOT NULL DEFAULT FALSE,
    raw_payload_id BIGINT,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, region, category_id)
);

-- 类目属性 schema 缓存（Amazon Product Type Definitions 结果）
-- 关键：属性 schema 会变，必须版本化并记录抓取时间
CREATE TABLE category_schemas (
    id              BIGSERIAL PRIMARY KEY,
    platform        VARCHAR(32) NOT NULL,
    region          VARCHAR(16) NOT NULL,
    category_id     VARCHAR(128) NOT NULL,
    product_type    VARCHAR(255),
    schema_version  VARCHAR(64) NOT NULL,
    schema_json     JSONB NOT NULL,      -- 完整属性定义
    required_fields VARCHAR(128)[],      -- 冗余必填，便于快速校验
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    UNIQUE (platform, region, category_id, schema_version)
);

CREATE INDEX idx_category_schemas_lookup
    ON category_schemas(platform, region, category_id, fetched_at DESC);
```

**关键设计说明**：

1. **`product_listings.listing_status` 的取值直接对应 TDD-04 上架状态机**，两处必须一致
2. **`category_schemas` 必须版本化**：Amazon 的 Product Type Definition 会变化，缓存不版本化会导致"昨天能上架今天不行"
3. `submission_id` 单独建索引：轮询 Feed 状态时的高频查询
4. `raw_payload_id` 指向原始报文表，实现"规范化数据可重建"

---

## 5. SKU 映射

```sql
-- SKU 映射（PRD 要求"平台 SKU 与内部 SKU 一一对应可查"，且冲突时阻断）
CREATE TABLE sku_mappings (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL,
    internal_sku   VARCHAR(128) NOT NULL,
    shop_id        BIGINT NOT NULL REFERENCES shops(id),
    platform       VARCHAR(32) NOT NULL,
    platform_sku   VARCHAR(255) NOT NULL,
    platform_item_id VARCHAR(255),
    asin           VARCHAR(32),
    mapping_type   VARCHAR(32) NOT NULL DEFAULT 'auto'
                   CHECK (mapping_type IN ('auto','manual','imported')),
    is_primary     BOOLEAN NOT NULL DEFAULT TRUE,   -- 一个 platform_sku 的主映射
    confidence     NUMERIC(4,3),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 一个店铺内 platform_sku 唯一（防止一个平台 SKU 映射到两个内部 SKU）
    UNIQUE (shop_id, platform_sku),
    -- 一个内部 SKU 在一个店铺内只能有一个主映射
    EXCLUDE USING btree (shop_id WITH =, internal_sku WITH =)
        WHERE (is_primary = TRUE)
);

CREATE INDEX idx_sku_map_internal ON sku_mappings(tenant_id, internal_sku);
```

**`EXCLUDE` 约束的作用**：防止一个内部 SKU 在同一店铺被映射到两个平台 SKU。这正是 PRD 6.4 要求的"发生冲突时阻断写入并转人工"。这是数据库层强约束，不依赖应用层检查。

---

## 6. 价格

```sql
-- 价格历史（append-only，PRD 原则 6）
CREATE TABLE price_history (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT NOT NULL,
    shop_id           BIGINT NOT NULL REFERENCES shops(id),
    platform_sku      VARCHAR(255) NOT NULL,
    listing_id        BIGINT REFERENCES product_listings(id),
    list_price        NUMERIC(18,6),
    sale_price        NUMERIC(18,6),
    promotion_price   NUMERIC(18,6),
    coupon_amount     NUMERIC(18,6),
    currency          VARCHAR(8) NOT NULL,
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source            VARCHAR(32) NOT NULL DEFAULT 'sync'
                      CHECK (source IN ('sync','manual','competitor','promotion'))
);

CREATE INDEX idx_price_hist_lookup
    ON price_history(tenant_id, shop_id, platform_sku, captured_at DESC);

-- 定价规则（Phase 1 建表，Phase 2 使用）
CREATE TABLE pricing_rules (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    name            VARCHAR(255) NOT NULL,
    scope_type      VARCHAR(32) NOT NULL CHECK (scope_type IN ('global','category','sku')),
    scope_value     VARCHAR(255),
    rule_type       VARCHAR(32) NOT NULL
                    CHECK (rule_type IN ('min_margin','max_discount','competitor_follow','fixed')),
    config          JSONB NOT NULL,
    min_margin_pct  NUMERIC(6,3),
    max_change_pct  NUMERIC(6,3),
    priority        INTEGER NOT NULL DEFAULT 100,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_pricing_rules_active ON pricing_rules(tenant_id, is_active, priority);
```

---

## 7. 订单

```sql
CREATE TABLE orders (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          BIGINT NOT NULL,
    shop_id            BIGINT NOT NULL REFERENCES shops(id),
    platform_order_id  VARCHAR(255) NOT NULL,
    order_status       VARCHAR(32) NOT NULL
                       CHECK (order_status IN ('pending','unshipped','partially_shipped',
                                               'shipped','delivered','canceled','returned')),
    fulfillment_channel VARCHAR(32),
    -- 买家信息：PII 最小化（PRD 15.3），能哈希则不明文
    buyer_hash         VARCHAR(128),        -- 买家标识的不可逆哈希
    buyer_region       VARCHAR(64),         -- 仅保留地区，用于分析
    buyer_encrypted    BYTEA,               -- 必须的收货信息，加密存储
    buyer_nonce        BYTEA,
    -- 金额
    item_total         NUMERIC(18,6) NOT NULL DEFAULT 0,
    shipping_total     NUMERIC(18,6) NOT NULL DEFAULT 0,
    discount_total     NUMERIC(18,6) NOT NULL DEFAULT 0,
    tax_total          NUMERIC(18,6) NOT NULL DEFAULT 0,
    grand_total        NUMERIC(18,6) NOT NULL DEFAULT 0,
    currency           VARCHAR(8) NOT NULL,
    -- 口径时间（PRD F5.2.1）
    order_time         TIMESTAMPTZ NOT NULL,     -- 下单时间（经营口径）
    ship_time          TIMESTAMPTZ,
    settle_time        TIMESTAMPTZ,              -- 结算时间（财务口径）
    -- 元数据
    raw_payload_id     BIGINT,
    platform_raw       JSONB NOT NULL DEFAULT '{}',
    synced_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, platform_order_id)
);

CREATE INDEX idx_orders_shop_time ON orders(tenant_id, shop_id, order_time DESC);
CREATE INDEX idx_orders_status ON orders(shop_id, order_status, order_time DESC);
CREATE INDEX idx_orders_settle ON orders(shop_id, settle_time DESC)
    WHERE settle_time IS NOT NULL;
CREATE INDEX idx_orders_buyer_hash ON orders(buyer_hash) WHERE buyer_hash IS NOT NULL;

CREATE TABLE order_items (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    order_id        BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    shop_id         BIGINT NOT NULL,
    platform_sku    VARCHAR(255),
    platform_item_id VARCHAR(255),
    internal_sku    VARCHAR(128),
    title_snapshot  VARCHAR(512),        -- 下单时的商品名快照
    quantity        INTEGER NOT NULL DEFAULT 1,
    unit_price      NUMERIC(18,6) NOT NULL,
    item_amount     NUMERIC(18,6) NOT NULL,
    discount_amount NUMERIC(18,6) NOT NULL DEFAULT 0,
    tax_amount      NUMERIC(18,6) NOT NULL DEFAULT 0,
    currency        VARCHAR(8) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_sku ON order_items(tenant_id, internal_sku);

-- 平台费用明细（对账关键：每笔费用能追溯到订单或结算批次）
CREATE TABLE order_fees (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    shop_id         BIGINT NOT NULL,
    order_id        BIGINT REFERENCES orders(id),
    order_item_id   BIGINT REFERENCES order_items(id),
    fee_type        VARCHAR(64) NOT NULL,
    -- commission / fba_fulfillment / fba_storage / shipping / tax / ads / other
    fee_code        VARCHAR(128),        -- 平台原始费用代码，用于归因追溯
    amount          NUMERIC(18,6) NOT NULL,
    currency        VARCHAR(8) NOT NULL,
    amount_cny      NUMERIC(18,6),       -- 按记账汇率换算
    exchange_rate   NUMERIC(18,8),
    fee_time        TIMESTAMPTZ,
    settlement_id   VARCHAR(128),        -- 关联结算批次
    raw_payload_id  BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_order_fees_order ON order_fees(order_id);
CREATE INDEX idx_order_fees_settlement ON order_fees(shop_id, settlement_id);
CREATE INDEX idx_order_fees_type ON order_fees(tenant_id, fee_type, fee_time DESC);

CREATE TABLE refunds (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT NOT NULL,
    shop_id           BIGINT NOT NULL,
    order_id          BIGINT NOT NULL REFERENCES orders(id),
    platform_refund_id VARCHAR(255),
    refund_type       VARCHAR(32) NOT NULL
                      CHECK (refund_type IN ('full','partial','goodwill','chargeback')),
    reason_code       VARCHAR(128),        -- 平台原始原因码
    reason_category   VARCHAR(64),         -- LLM 归类结果：quality/size/not_liked/shipping/wrong_item
    reason_confidence NUMERIC(4,3),
    amount            NUMERIC(18,6) NOT NULL,
    currency          VARCHAR(8) NOT NULL,
    status            VARCHAR(32) NOT NULL
                      CHECK (status IN ('requested','approved','auto_approved',
                                        'rejected','refunded','closed')),
    risk_level        VARCHAR(16) NOT NULL DEFAULT 'low'
                      CHECK (risk_level IN ('low','medium','high')),
    -- 自动化判定留痕（PRD 12.5）
    auto_decision     JSONB,               -- 命中的规则、判定依据
    handled_by        BIGINT REFERENCES users(id),
    handled_at        TIMESTAMPTZ,
    returned_at       TIMESTAMPTZ,
    restock_status    VARCHAR(32),         -- sellable / unsellable / pending
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, platform_refund_id)
);

CREATE INDEX idx_refunds_order ON refunds(order_id);
CREATE INDEX idx_refunds_status ON refunds(shop_id, status, created_at DESC);
CREATE INDEX idx_refunds_reason ON refunds(tenant_id, reason_category);
```

**PII 处理说明（重要）**：

`orders` 表**不存买家姓名、地址、电话明文**。设计是：

- 能用哈希关联的分析场景 → `buyer_hash`
- 只做地域分析 → `buyer_region`
- 必须留存用于发货的收货信息 → `buyer_encrypted` 加密存储，且受 Amazon 30 天删除规则约束（PRD 15.3）

**这意味着**：ERP 打单所需的明文地址**不经过本系统**，或单独走加密通道。本系统定位是经营分析，不是履约系统（PRD 2.3 已声明不做 WMS）。

---

## 8. 库存

```sql
CREATE TABLE inventory_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    shop_id         BIGINT NOT NULL,
    platform_sku    VARCHAR(255) NOT NULL,
    internal_sku    VARCHAR(128),
    available_qty   INTEGER NOT NULL DEFAULT 0,
    reserved_qty    INTEGER NOT NULL DEFAULT 0,
    inbound_qty     INTEGER NOT NULL DEFAULT 0,
    unfulfillable_qty INTEGER NOT NULL DEFAULT 0,
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_inv_snap_lookup
    ON inventory_snapshots(tenant_id, shop_id, platform_sku, snapshot_at DESC);

CREATE TABLE inventory_alerts (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL,
    shop_id        BIGINT NOT NULL,
    internal_sku   VARCHAR(128) NOT NULL,
    alert_type     VARCHAR(32) NOT NULL
                   CHECK (alert_type IN ('stockout_risk','overstock','slow_moving',
                                          'turnover_abnormal','negative_stock')),
    level          VARCHAR(16) NOT NULL CHECK (level IN ('p0','p1','p2')),
    threshold      JSONB NOT NULL,        -- 触发时的阈值快照
    current_value  NUMERIC(18,6),
    days_of_stock  NUMERIC(10,2),         -- 可售天数
    suggestion     TEXT,                  -- 建议动作
    status         VARCHAR(32) NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','acknowledged','resolved','ignored')),
    resolved_by    BIGINT REFERENCES users(id),
    resolved_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_inv_alerts_open ON inventory_alerts(tenant_id, status, level, created_at DESC);

-- 库存策略（安全库存、补货点）
CREATE TABLE inventory_policies (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        BIGINT NOT NULL,
    internal_sku     VARCHAR(128) NOT NULL,
    shop_id          BIGINT,              -- NULL = 全局
    safety_stock_days INTEGER NOT NULL DEFAULT 15,
    reorder_point    INTEGER,
    lead_time_days   INTEGER NOT NULL DEFAULT 30,
    max_stock_days   INTEGER,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, internal_sku, shop_id)
);
```

---

## 9. 广告（⚠️ Phase 1 建表不实现）

```sql
CREATE TABLE ad_campaigns (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    shop_id         BIGINT NOT NULL,
    platform        VARCHAR(32) NOT NULL,
    campaign_id     VARCHAR(128) NOT NULL,
    campaign_name   VARCHAR(512),
    ad_product      VARCHAR(64),        -- SPONSORED_PRODUCTS / SB / SD
    targeting_type  VARCHAR(32),        -- AUTO / MANUAL
    daily_budget    NUMERIC(18,6),
    currency        VARCHAR(8),
    state           VARCHAR(32),
    platform_raw    JSONB NOT NULL DEFAULT '{}',
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, campaign_id)
);

CREATE TABLE ad_groups (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL,
    shop_id        BIGINT NOT NULL,
    campaign_id    VARCHAR(128) NOT NULL,
    ad_group_id    VARCHAR(128) NOT NULL,
    ad_group_name  VARCHAR(512),
    default_bid    NUMERIC(18,6),
    state          VARCHAR(32),
    platform_raw   JSONB NOT NULL DEFAULT '{}',
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, ad_group_id)
);

CREATE TABLE ad_reports (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    shop_id       BIGINT NOT NULL,
    platform      VARCHAR(32) NOT NULL,
    report_date   DATE NOT NULL,
    campaign_id   VARCHAR(128),
    ad_group_id   VARCHAR(128),
    keyword       VARCHAR(512),
    match_type    VARCHAR(32),
    internal_sku  VARCHAR(128),
    impressions   BIGINT DEFAULT 0,
    clicks        BIGINT DEFAULT 0,
    spend         NUMERIC(18,6) DEFAULT 0,
    sales         NUMERIC(18,6) DEFAULT 0,
    orders        INTEGER DEFAULT 0,
    acos          NUMERIC(10,6),
    roas          NUMERIC(10,6),
    currency      VARCHAR(8),
    raw_payload_id BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, report_date, campaign_id, ad_group_id, keyword)
);

CREATE INDEX idx_ad_reports_date ON ad_reports(tenant_id, shop_id, report_date DESC);
CREATE INDEX idx_ad_reports_sku ON ad_reports(tenant_id, internal_sku, report_date DESC);
```

**注意**：`ad_reports` 的唯一键包含 `keyword`，但 PostgreSQL 中 NULL 不相等，可能导致重复。**解决**：用 `COALESCE(keyword, '')` 生成列做唯一约束，或应用层保证无 NULL。建议后者（简单）。

---

## 10. 财务（Phase 1 核心）

```sql
-- 汇率（多口径，PRD F5.2.2）
CREATE TABLE exchange_rates (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL,
    from_currency  VARCHAR(8) NOT NULL,
    to_currency    VARCHAR(8) NOT NULL,
    rate_type      VARCHAR(32) NOT NULL
                   CHECK (rate_type IN ('settlement','booking','transaction')),
    rate           NUMERIC(18,8) NOT NULL,
    rate_date      DATE NOT NULL,
    source         VARCHAR(64),
    is_locked      BOOLEAN NOT NULL DEFAULT FALSE,   -- 关账后锁定
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, from_currency, to_currency, rate_type, rate_date)
);

CREATE INDEX idx_fx_lookup ON exchange_rates(tenant_id, from_currency, to_currency,
                                             rate_type, rate_date DESC);

-- 会计期间（关账管理，PRD F5.2.2）
CREATE TABLE accounting_periods (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    period_code   VARCHAR(16) NOT NULL,      -- 2026-09
    start_date    DATE NOT NULL,
    end_date      DATE NOT NULL,
    status        VARCHAR(32) NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','closing','closed','reopened')),
    closed_by     BIGINT REFERENCES users(id),
    closed_at     TIMESTAMPTZ,
    reopen_reason TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, period_code)
);

-- 成本项
CREATE TABLE cost_items (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL,
    internal_sku   VARCHAR(128),
    shop_id        BIGINT,
    cost_type      VARCHAR(32) NOT NULL
                   CHECK (cost_type IN ('purchase','first_mile','platform_fee','fba_fee',
                                         'ads','refund','tax','duty','marketing',
                                         'fbb_commission','other')),
    amount         NUMERIC(18,6) NOT NULL,
    currency       VARCHAR(8) NOT NULL,
    amount_cny     NUMERIC(18,6),
    exchange_rate  NUMERIC(18,8),
    -- 生效区间（成本会变，必须支持历史成本追溯）
    effective_from DATE NOT NULL,
    effective_to   DATE,
    -- 分摊
    allocation_basis VARCHAR(32)
                     CHECK (allocation_basis IN ('per_unit','by_weight','by_volume',
                                                  'by_value','manual')),
    source         VARCHAR(32) NOT NULL DEFAULT 'manual'
                   CHECK (source IN ('manual','import','erp','api','allocated')),
    rule_version   VARCHAR(32),              -- 使用的分摊规则版本
    remark         TEXT,
    created_by     BIGINT REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_cost_items_sku ON cost_items(tenant_id, internal_sku, effective_from DESC);
CREATE INDEX idx_cost_items_type ON cost_items(tenant_id, cost_type, effective_from DESC);

-- 成本与分摊规则（版本化，PRD 要求规则可追溯）
CREATE TABLE cost_rules (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    rule_code     VARCHAR(64) NOT NULL,
    name          VARCHAR(255) NOT NULL,
    rule_type     VARCHAR(32) NOT NULL
                  CHECK (rule_type IN ('inventory_valuation','first_mile_allocation',
                                        'ads_allocation','refund_allocation',
                                        'fx_policy','tax_policy')),
    config        JSONB NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    is_active     BOOLEAN NOT NULL DEFAULT FALSE,
    effective_from DATE NOT NULL,
    approved_by   BIGINT REFERENCES users(id),   -- 财务签字人（PRD 20.4）
    approved_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, rule_code, version)
);

-- 结算记录（平台结算批次）
CREATE TABLE settlement_records (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT NOT NULL,
    shop_id           BIGINT NOT NULL,
    platform          VARCHAR(32) NOT NULL,
    settlement_id     VARCHAR(128) NOT NULL,   -- 平台结算批次 ID
    period_start      DATE,
    period_end        DATE,
    settlement_date   DATE,
    gross_amount      NUMERIC(18,6) NOT NULL DEFAULT 0,
    total_fees        NUMERIC(18,6) NOT NULL DEFAULT 0,
    total_tax         NUMERIC(18,6) NOT NULL DEFAULT 0,
    net_amount        NUMERIC(18,6) NOT NULL DEFAULT 0,
    currency          VARCHAR(8) NOT NULL,
    net_amount_cny    NUMERIC(18,6),
    exchange_rate     NUMERIC(18,8),
    exchange_rate_type VARCHAR(32) DEFAULT 'settlement',
    raw_payload_id    BIGINT,
    platform_raw      JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, settlement_id)
);

CREATE INDEX idx_settlement_period
    ON settlement_records(tenant_id, shop_id, settlement_date DESC);

-- 结算费用明细（对账核心：差异必须能追到具体条目）
CREATE TABLE settlement_fees (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    shop_id         BIGINT NOT NULL,
    settlement_record_id BIGINT NOT NULL REFERENCES settlement_records(id) ON DELETE CASCADE,
    settlement_id   VARCHAR(128) NOT NULL,
    fee_type        VARCHAR(64) NOT NULL,
    fee_code        VARCHAR(128),
    fee_description VARCHAR(512),
    amount          NUMERIC(18,6) NOT NULL,
    currency        VARCHAR(8) NOT NULL,
    related_order_id VARCHAR(255),
    related_sku     VARCHAR(255),
    posted_date     DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_settle_fees_record ON settlement_fees(settlement_record_id);
CREATE INDEX idx_settle_fees_order ON settlement_fees(shop_id, related_order_id);
CREATE INDEX idx_settle_fees_type ON settlement_fees(tenant_id, fee_type, posted_date DESC);

-- 利润快照（多维度，PRD F5.2 贡献利润口径）
CREATE TABLE profit_snapshots (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_id             BIGINT NOT NULL,
    dim_type              VARCHAR(32) NOT NULL
                          CHECK (dim_type IN ('sku','shop','site','category','total','channel')),
    dim_key               VARCHAR(255) NOT NULL,
    shop_id               BIGINT,
    period_type           VARCHAR(16) NOT NULL CHECK (period_type IN ('day','week','month')),
    period_code           VARCHAR(16) NOT NULL,   -- 2026-09-28 / 2026-W39 / 2026-09
    -- 收入
    gross_sales           NUMERIC(18,6) NOT NULL DEFAULT 0,
    discounts             NUMERIC(18,6) NOT NULL DEFAULT 0,
    refunds               NUMERIC(18,6) NOT NULL DEFAULT 0,
    chargebacks           NUMERIC(18,6) NOT NULL DEFAULT 0,
    net_sales             NUMERIC(18,6) NOT NULL DEFAULT 0,
    -- 成本（对应 PRD F5.2 的层级）
    cogs                  NUMERIC(18,6) NOT NULL DEFAULT 0,
    first_mile_cost       NUMERIC(18,6) NOT NULL DEFAULT 0,
    platform_commission   NUMERIC(18,6) NOT NULL DEFAULT 0,
    fulfillment_fee       NUMERIC(18,6) NOT NULL DEFAULT 0,
    storage_fee           NUMERIC(18,6) NOT NULL DEFAULT 0,
    refund_cost           NUMERIC(18,6) NOT NULL DEFAULT 0,
    -- 贡献利润（三级）
    pre_fulfillment_profit NUMERIC(18,6) NOT NULL DEFAULT 0,
    post_fulfillment_profit NUMERIC(18,6) NOT NULL DEFAULT 0,
    post_ads_profit        NUMERIC(18,6) NOT NULL DEFAULT 0,
    -- 可变费用
    ads_spend             NUMERIC(18,6) NOT NULL DEFAULT 0,
    promotion_cost        NUMERIC(18,6) NOT NULL DEFAULT 0,
    other_variable_cost   NUMERIC(18,6) NOT NULL DEFAULT 0,
    tax_amount            NUMERIC(18,6) NOT NULL DEFAULT 0,
    fx_loss               NUMERIC(18,6) NOT NULL DEFAULT 0,
    -- 比率
    post_fulfillment_margin NUMERIC(10,6),
    post_ads_margin         NUMERIC(10,6),
    tacos                   NUMERIC(10,6),   -- 广告费 / net_sales（全系统统一）
    -- 数量
    units_sold            INTEGER NOT NULL DEFAULT 0,
    currency              VARCHAR(8) NOT NULL DEFAULT 'CNY',
    -- 归因与版本（PRD F5.2.2）
    cost_rule_version     VARCHAR(64),
    allocation_pool       JSONB,     -- 待分摊池明细
    accounting_period_id  BIGINT REFERENCES accounting_periods(id),
    is_final              BOOLEAN NOT NULL DEFAULT FALSE,   -- 关账后为 TRUE
    detail                JSONB NOT NULL DEFAULT '{}',
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, dim_type, dim_key, period_type, period_code, currency)
);

CREATE INDEX idx_profit_lookup
    ON profit_snapshots(tenant_id, dim_type, period_type, period_code DESC);
CREATE INDEX idx_profit_sku
    ON profit_snapshots(tenant_id, dim_key, period_code DESC)
    WHERE dim_type = 'sku';
```

**关键设计说明**：

1. **三级贡献利润字段独立存储**：`pre_fulfillment_profit` / `post_fulfillment_profit` / `post_ads_profit`，不可只存一个 `profit`。这样才能支持"这个 SKU 广告前赚钱、广告后亏钱"的分析（PRD F5.2 明确要求）。
2. **`is_final` 标记关账**：关账后数据冻结，任何调整走新记录，不覆盖历史（PRD F5.2.2）。
3. **`cost_rule_version` 必须记录**：否则无法复现历史利润计算。
4. **`allocation_pool` 保存待分摊明细**：PRD 明确"广告无法直接归因到 SKU 时进入待分摊池，不得静默平均分摊"，这个字段就是留痕。
5. **TACOS 分母唯一**：`net_sales`，与 PRD 11.4 指标字典一致。

---

## 11. 审批与审计（PRD 3.4/3.5 落地）

```sql
-- 审批单
CREATE TABLE approvals (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          BIGINT NOT NULL,
    approval_no        VARCHAR(64) NOT NULL UNIQUE,
    approval_type      VARCHAR(64) NOT NULL,
    -- listing_publish / listing_delist / price_update / ad_budget_change
    -- refund_execute / credential_access / data_export / bulk_operation
    risk_level         VARCHAR(16) NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    status             VARCHAR(32) NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('draft','pending','approved','rejected',
                                          'expired','canceled','executed','failed')),
    shop_id            BIGINT REFERENCES shops(id),
    resource_type      VARCHAR(64),
    resource_id        VARCHAR(255),
    -- 变更前后值（PRD 3.4 要求）
    payload_before     JSONB,
    payload_after      JSONB NOT NULL,
    -- 建议来源
    suggestion_source  VARCHAR(32) NOT NULL DEFAULT 'human'
                       CHECK (suggestion_source IN ('human','ai','rule','alert')),
    ai_confidence      NUMERIC(4,3),
    rule_version       VARCHAR(64),
    -- 幂等与限额
    idempotency_key    VARCHAR(128) NOT NULL,
    amount             NUMERIC(18,6),        -- 涉及金额时
    quantity           INTEGER,              -- 涉及数量时
    -- 流程
    requested_by       BIGINT NOT NULL REFERENCES users(id),
    request_reason     TEXT,
    approved_by        BIGINT REFERENCES users(id),
    approved_at        TIMESTAMPTZ,
    reject_reason      TEXT,
    -- 执行
    executed_at        TIMESTAMPTZ,
    execution_result   JSONB,
    rollback_status    VARCHAR(32) CHECK (rollback_status IN ('none','partial','done','failed')),
    expires_at         TIMESTAMPTZ,          -- 审批超时（PRD 3.4）
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 职责分离：发起人不得为审批人（数据库层强制）
    CONSTRAINT chk_separation_of_duties
        CHECK (approved_by IS NULL OR approved_by <> requested_by),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX idx_approvals_pending ON approvals(tenant_id, status, risk_level, created_at DESC);
CREATE INDEX idx_approvals_resource ON approvals(tenant_id, resource_type, resource_id);
CREATE INDEX idx_approvals_expiry ON approvals(expires_at)
    WHERE status = 'pending' AND expires_at IS NOT NULL;

-- 审批动作流水（append-only）
CREATE TABLE approval_actions (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    approval_id   BIGINT NOT NULL REFERENCES approvals(id) ON DELETE CASCADE,
    action        VARCHAR(32) NOT NULL
                  CHECK (action IN ('submit','approve','reject','cancel','expire',
                                     'execute','execute_fail','rollback','comment')),
    actor_id      BIGINT REFERENCES users(id),
    actor_type    VARCHAR(16) NOT NULL CHECK (actor_type IN ('user','system')),
    comment       TEXT,
    metadata      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_approval_actions ON approval_actions(approval_id, created_at);

-- 业务审计（append-only，PRD 3.5）
-- 与 api_call_logs 职责分离：这里记业务事实，那里记排障元数据
CREATE TABLE audit_events (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL,
    event_type     VARCHAR(64) NOT NULL,
    -- credential.read / listing.publish / price.update / refund.execute
    -- permission.grant / data.export / config.change / login / approval.*
    risk_level     VARCHAR(16) NOT NULL DEFAULT 'low'
                   CHECK (risk_level IN ('low','medium','high','critical')),
    actor_id       BIGINT,
    actor_type     VARCHAR(16) NOT NULL CHECK (actor_type IN ('user','system','api')),
    shop_id        BIGINT,
    resource_type  VARCHAR(64),
    resource_id    VARCHAR(255),
    before_hash    VARCHAR(64),        -- 变更前值哈希（不存明文）
    after_hash     VARCHAR(64),
    changed_fields VARCHAR(64)[],      -- 变更字段名列表
    detail         JSONB,              -- 脱敏后的详情
    trace_id       VARCHAR(64),        -- 链路关联
    ip_address     INET,
    user_agent     VARCHAR(512),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_events_type ON audit_events(tenant_id, event_type, created_at DESC);
CREATE INDEX idx_audit_events_actor ON audit_events(tenant_id, actor_id, created_at DESC);
CREATE INDEX idx_audit_events_resource ON audit_events(tenant_id, resource_type, resource_id);
CREATE INDEX idx_audit_events_risk ON audit_events(risk_level, created_at DESC)
    WHERE risk_level IN ('high','critical');

-- 审计表只允许 INSERT，禁止 UPDATE/DELETE（应用层 + 数据库权限双重限制）
REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC;
REVOKE UPDATE, DELETE ON approval_actions FROM PUBLIC;
REVOKE UPDATE, DELETE ON credential_audit_logs FROM PUBLIC;
```

**两个必须在数据库层强制的约束**：

1. **`chk_separation_of_duties`**：发起人 ≠ 审批人。这是 PRD 3.4 要求"发起人与审批人不得为同一人"，用 CHECK 约束实现，不依赖应用层。
2. **`REVOKE UPDATE, DELETE`**：审计表 append-only。应用账号只有 INSERT 权限。

---

## 12. 预警

```sql
CREATE TABLE alert_rules (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    rule_code       VARCHAR(64) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    alert_type      VARCHAR(64) NOT NULL,
    scope_type      VARCHAR(32) NOT NULL CHECK (scope_type IN ('global','shop','category','sku')),
    scope_value     VARCHAR(255),
    -- 触发条件
    condition       JSONB NOT NULL,     -- {"field":"price_change_pct","op":">","value":10}
    level           VARCHAR(16) NOT NULL CHECK (level IN ('p0','p1','p2')),
    -- 通知（PRD 12.4：分级推送，禁止默认 @所有人）
    notify_channels VARCHAR(64)[] NOT NULL DEFAULT '{wecom}',
    notify_roles    VARCHAR(64)[] NOT NULL,
    escalate_after_minutes INTEGER,     -- 超时升级
    cooldown_minutes INTEGER NOT NULL DEFAULT 60,  -- 防刷屏
    min_amount      NUMERIC(18,6),      -- 金额门槛，防小额噪音
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, rule_code, version)
);

CREATE TABLE alerts (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    alert_type      VARCHAR(64) NOT NULL,
    level           VARCHAR(16) NOT NULL CHECK (level IN ('p0','p1','p2')),
    title           VARCHAR(512) NOT NULL,
    content         TEXT,
    -- 建议动作（PRD 12.4 强制要求，不能只报警）
    suggestion      TEXT,
    suggestion_actions JSONB,      -- [{"label":"跟价到$25.49","action":"price_adjust",...}]
    related_type    VARCHAR(64),
    related_id      VARCHAR(255),
    shop_id         BIGINT,
    rule_id         BIGINT REFERENCES alert_rules(id),
    dedup_key       VARCHAR(128),  -- 去重键，防重复告警
    status          VARCHAR(32) NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','notified','acknowledged','resolved',
                                       'ignored','suppressed')),
    notified_at     TIMESTAMPTZ,
    notified_channels VARCHAR(64)[],
    acknowledged_by BIGINT REFERENCES users(id),
    acknowledged_at TIMESTAMPTZ,
    resolved_by     BIGINT REFERENCES users(id),
    resolved_at     TIMESTAMPTZ,
    resolution_note TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_alerts_open ON alerts(tenant_id, status, level, created_at DESC);
CREATE INDEX idx_alerts_dedup ON alerts(dedup_key, created_at DESC)
    WHERE dedup_key IS NOT NULL;
```

**`suggestion_actions` 字段的用意**：存结构化建议动作，前端可直接渲染成按钮，机器人可直接带操作链接。这解决了 PRD 12.4 "预警必须带建议动作"的落地问题。

---

## 13. 任务、同步与原始数据

```sql
-- 原始报文归档（不可变，PRD 原则 5）
CREATE TABLE raw_payloads (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    shop_id       BIGINT,
    platform      VARCHAR(32),
    source_type   VARCHAR(64) NOT NULL,    -- api_response / report_file / webhook / import
    endpoint      VARCHAR(512),
    request_id    VARCHAR(255),
    payload_hash  VARCHAR(64) NOT NULL,    -- SHA-256，用于去重与校验
    payload       BYTEA NOT NULL,          -- 压缩后存储
    encoding      VARCHAR(16) NOT NULL DEFAULT 'gzip',
    content_type  VARCHAR(64),
    size_bytes    BIGINT,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ,             -- 按保留策略设置
    UNIQUE (payload_hash, shop_id)
);

CREATE INDEX idx_raw_payloads_lookup
    ON raw_payloads(tenant_id, shop_id, source_type, captured_at DESC);
CREATE INDEX idx_raw_payloads_expiry ON raw_payloads(expires_at)
    WHERE expires_at IS NOT NULL;

-- 任务执行记录
CREATE TABLE task_runs (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL,
    task_name      VARCHAR(255) NOT NULL,
    task_type      VARCHAR(64) NOT NULL,   -- sync / listing / finance / report / alert
    trigger_type   VARCHAR(32) NOT NULL CHECK (trigger_type IN ('schedule','manual','retry','event')),
    celery_task_id VARCHAR(128),
    trace_id       VARCHAR(64),
    status         VARCHAR(32) NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running','success','failed','timeout','canceled')),
    shop_id        BIGINT,
    params         JSONB,
    result_summary JSONB,
    -- 统计
    processed_count INTEGER DEFAULT 0,
    success_count   INTEGER DEFAULT 0,
    failed_count    INTEGER DEFAULT 0,
    -- 错误
    error_type     VARCHAR(64),
    error_msg      TEXT,
    error_trace    TEXT,
    -- 时间
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    duration_ms    BIGINT,
    -- 重试链
    retry_count    INTEGER NOT NULL DEFAULT 0,
    parent_run_id  BIGINT REFERENCES task_runs(id),
    -- 首次成功 vs 最终成功（PRD 20.1 要求分开统计）
    is_first_attempt BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX idx_task_runs_lookup ON task_runs(tenant_id, task_name, started_at DESC);
CREATE INDEX idx_task_runs_status ON task_runs(status, started_at DESC)
    WHERE status IN ('running','failed');
CREATE INDEX idx_task_runs_trace ON task_runs(trace_id) WHERE trace_id IS NOT NULL;

-- 同步游标（增量同步水位，PRD 10.2.2）
CREATE TABLE sync_cursors (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    shop_id         BIGINT NOT NULL,
    resource        VARCHAR(64) NOT NULL,   -- orders / inventory / listings / settlement
    cursor_type     VARCHAR(32) NOT NULL DEFAULT 'timestamp',
    cursor_value    TEXT,                   -- 时间戳 / token / page token
    last_success_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    status          VARCHAR(32) NOT NULL DEFAULT 'idle'
                    CHECK (status IN ('idle','running','error','paused')),
    error_msg       TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, resource)
);

CREATE INDEX idx_sync_cursors_status ON sync_cursors(tenant_id, status);

-- 同步水位与完整性检查（PRD 10.2.2 要求"最后完整核对时间"）
CREATE TABLE sync_watermarks (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_id             BIGINT NOT NULL,
    shop_id               BIGINT NOT NULL,
    resource              VARCHAR(64) NOT NULL,
    last_full_check_at    TIMESTAMPTZ,     -- 最后一次全量对账
    last_incremental_at   TIMESTAMPTZ,
    data_max_time         TIMESTAMPTZ,     -- 已同步数据的最晚业务时间
    fresh_lag_minutes     INTEGER,         -- 数据新鲜度（用于 SLO）
    record_count          BIGINT,
    integrity_status      VARCHAR(32) NOT NULL DEFAULT 'unknown'
                          CHECK (integrity_status IN ('ok','gaps_detected','stale','unknown')),
    gap_detail            JSONB,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shop_id, resource)
);

-- API 调用日志（字段白名单，PRD 15.5 要求脱敏）
CREATE TABLE api_call_logs (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL,
    shop_id        BIGINT,
    platform       VARCHAR(32),
    endpoint       VARCHAR(512) NOT NULL,
    method         VARCHAR(16) NOT NULL,
    status_code    INTEGER,
    error_code     VARCHAR(128),
    error_category VARCHAR(64),        -- 统一错误分类（TDD-03）
    duration_ms    INTEGER,
    retry_count    INTEGER DEFAULT 0,
    rate_limited   BOOLEAN DEFAULT FALSE,
    request_id     VARCHAR(255),       -- 平台返回的请求 ID
    trace_id       VARCHAR(64),
    -- 默认只存摘要，采样时存脱敏报文
    request_summary  JSONB,            -- 白名单字段
    response_summary JSONB,
    raw_payload_id   BIGINT,           -- 采样时指向原始报文
    called_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ         -- 日志保留策略
);

CREATE INDEX idx_api_logs_lookup ON api_call_logs(tenant_id, platform, called_at DESC);
CREATE INDEX idx_api_logs_error ON api_call_logs(error_category, called_at DESC)
    WHERE error_category IS NOT NULL;
CREATE INDEX idx_api_logs_expiry ON api_call_logs(expires_at) WHERE expires_at IS NOT NULL;
```

**`is_first_attempt` 字段的用意**：直接对应 PRD 20.1 要求"首次成功率与最终成功率必须分开统计"。没有这个字段就只能靠 `retry_count = 0` 推断，不够严谨。

---

## 14. 配置与指标字典

```sql
-- 指标字典（PRD 11.4，唯一口径来源）
CREATE TABLE metric_definitions (
    id             BIGSERIAL PRIMARY KEY,
    metric_code    VARCHAR(64) NOT NULL UNIQUE,
    metric_name    VARCHAR(255) NOT NULL,
    formula        TEXT NOT NULL,          -- 人类可读公式
    formula_sql    TEXT,                   -- 可执行定义（可选）
    data_source    VARCHAR(255),
    owner_module   VARCHAR(64),
    unit           VARCHAR(32),
    precision      SMALLINT DEFAULT 6,
    description    TEXT,
    -- 口径一致性（PRD 修复项：TACOS 分母、周转天数口径）
    is_canonical   BOOLEAN NOT NULL DEFAULT TRUE,   -- 是否唯一正式定义
    superseded_by  VARCHAR(64),            -- 被哪个指标取代
    version        INTEGER NOT NULL DEFAULT 1,
    approved_by    BIGINT REFERENCES users(id),     -- 财务签字
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 系统配置（阈值、开关，避免硬编码）
CREATE TABLE system_configs (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    config_key    VARCHAR(128) NOT NULL,
    config_value  JSONB NOT NULL,
    value_type    VARCHAR(32) NOT NULL DEFAULT 'json',
    category      VARCHAR(64),             -- approval / alert / sync / limits / profit
    description   TEXT,
    is_sensitive  BOOLEAN NOT NULL DEFAULT FALSE,
    version       INTEGER NOT NULL DEFAULT 1,
    updated_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, config_key)
);

CREATE INDEX idx_system_configs_cat ON system_configs(tenant_id, category);

-- 功能开关（Kill Switch，PRD 3.5）
CREATE TABLE feature_flags (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    flag_key      VARCHAR(128) NOT NULL,
    scope_type    VARCHAR(32) NOT NULL DEFAULT 'global'
                  CHECK (scope_type IN ('global','platform','shop','capability')),
    scope_value   VARCHAR(255),
    is_enabled    BOOLEAN NOT NULL DEFAULT FALSE,
    -- Kill Switch 语义：disabled 表示立即停止该类写操作
    is_kill_switch BOOLEAN NOT NULL DEFAULT FALSE,
    reason        TEXT,
    changed_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, flag_key, scope_type, scope_value)
);

CREATE INDEX idx_feature_flags_lookup ON feature_flags(tenant_id, flag_key, scope_type, scope_value);
```

**Kill Switch 设计说明**：`feature_flags` 表同时承载功能开关和 Kill Switch。当 `is_kill_switch = true` 且 `is_enabled = false` 时，对应范围（全局/平台/店铺/能力）的所有写操作**必须在适配器层被拦截**。这实现了 PRD 3.5 要求的四级紧急停止。

---

## 15. AI 相关（⚠️ Phase 1 建表不实现）

```sql
-- AI 调用记录（PRD 16.2 版本可追溯）
CREATE TABLE ai_invocations (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           BIGINT NOT NULL,
    use_case            VARCHAR(64) NOT NULL,   -- message_classify / title_generate / ...
    risk_level          VARCHAR(16) NOT NULL CHECK (risk_level IN ('low','medium','high')),
    -- 版本溯源（PRD 16.2 强制）
    provider            VARCHAR(64) NOT NULL,
    model_id            VARCHAR(128) NOT NULL,
    model_version       VARCHAR(64),
    prompt_template_id  VARCHAR(128),
    prompt_version      INTEGER,
    eval_dataset_version VARCHAR(64),
    rule_version        VARCHAR(64),
    output_schema_version VARCHAR(32),
    -- 输入输出（哈希优先，原文按 PII 政策可选）
    input_hash          VARCHAR(64) NOT NULL,
    input_summary       JSONB,
    output_json         JSONB,
    output_text         TEXT,
    -- 质量
    confidence          NUMERIC(4,3),
    was_validated       BOOLEAN DEFAULT FALSE,
    validation_errors   JSONB,
    was_overridden      BOOLEAN DEFAULT FALSE,   -- 人工改写（PRD 16.5 监测指标）
    override_reason     TEXT,
    -- 成本
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    estimated_cost_usd  NUMERIC(12,6),
    latency_ms          INTEGER,
    was_degraded        BOOLEAN DEFAULT FALSE,
    fallback_used       VARCHAR(64),
    -- 关联
    related_type        VARCHAR(64),
    related_id          VARCHAR(255),
    trace_id            VARCHAR(64),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ai_inv_use_case ON ai_invocations(tenant_id, use_case, created_at DESC);
CREATE INDEX idx_ai_inv_model ON ai_invocations(model_id, prompt_version, created_at DESC);
CREATE INDEX idx_ai_inv_cost ON ai_invocations(created_at DESC);

CREATE TABLE ai_eval_datasets (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    dataset_code  VARCHAR(64) NOT NULL,
    version       VARCHAR(32) NOT NULL,
    use_case      VARCHAR(64) NOT NULL,
    language      VARCHAR(16),
    risk_level    VARCHAR(16),
    sample_count  INTEGER NOT NULL DEFAULT 0,
    file_path     VARCHAR(512),
    metrics       JSONB,          -- 最近一次评测结果
    is_frozen     BOOLEAN NOT NULL DEFAULT FALSE,   -- 冻结后不可改（PRD 16.3）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, dataset_code, version)
);
```

---

## 16. 索引与分区策略

### 16.1 索引原则

1. **所有业务查询索引以 `tenant_id` 或 `shop_id` 开头**（多租户前缀）
2. **时间范围查询用 DESC 排序索引**（取最新 N 条是最高频场景）
3. **部分索引**用于稀疏条件（如 `WHERE status = 'pending'`）
4. **避免过度索引**：每个索引都拖慢写入，Phase 1 只建必要索引

### 16.2 分区表（大表必做）

以下表数据量增长最快，**Phase 1 就建分区**，避免后期迁移困难：

```sql
-- 订单按月分区
CREATE TABLE orders (
    ...
) PARTITION BY RANGE (order_time);

CREATE TABLE orders_2026_09 PARTITION OF orders
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
-- 后续月份由定时任务自动创建

-- price_history 按月分区
-- inventory_snapshots 按月分区
-- api_call_logs 按周分区（量大，保留期短）
-- audit_events 按月分区（保留期长）
-- competitor_snapshots 按月分区（Phase 3）
```

**自动创建分区的任务**：`apps/worker/tasks/maintenance/create_partitions.py`，每月 25 日创建下下个月的分区。

### 16.3 数据保留策略

| 表 | 保留期 | 处理方式 |
|---|---|---|
| `api_call_logs` | 90 天 | 删除分区 |
| `raw_payloads` | 180 天（含 PII 的 30 天） | 按 `expires_at` 清理 |
| `orders` | 永久（分区归档） | 冷热分离 |
| `price_history` | 24 个月 | 归档 |
| `inventory_snapshots` | 12 个月 | 降采样（保留日快照） |
| `audit_events` | 36 个月 | 归档到冷存储 |
| `ai_invocations` | 12 个月 | 聚合后删除明细 |

**PII 特殊规则（PRD 15.3）**：`orders.buyer_encrypted`、`customer_messages.content` 等 PII 字段遵循 **30 天删除**（Amazon 官方要求），通过定时任务置 NULL 并记录删除审计。

---

## 17. 数据字典（关键字段枚举值）

### 17.1 状态枚举汇总

| 表.字段 | 枚举值 |
|---|---|
| `product_listings.listing_status` | draft / validating / pending_approval / rejected / queued / submitted / processing / active / partial_active / inactive / failed / deleted |
| `orders.order_status` | pending / unshipped / partially_shipped / shipped / delivered / canceled / returned |
| `refunds.status` | requested / approved / auto_approved / rejected / refunded / closed |
| `approvals.status` | draft / pending / approved / rejected / expired / canceled / executed / failed |
| `task_runs.status` | running / success / failed / timeout / canceled |
| `sync_cursors.status` | idle / running / error / paused |
| `alerts.status` | open / notified / acknowledged / resolved / ignored / suppressed |
| `accounting_periods.status` | open / closing / closed / reopened |

### 17.2 费用类型枚举

| `fee_type` | 含义 |
|---|---|
| `commission` | 平台佣金 |
| `fba_fulfillment` | FBA 配送费 |
| `fba_storage` | 仓储费 |
| `shipping` | 运费 |
| `tax` | 税费（平台代扣） |
| `ads` | 广告费 |
| `refund_admin` | 退款手续费 |
| `chargeback` | 拒付 |
| `fbb_commission` | 半托管佣金 |
| `other` | 其他 |

---

## 18. 迁移与初始化

### 18.1 Alembic 迁移顺序

```text
001_init_tenants_users_roles      租户权限基础
002_init_shops_credentials        店铺与凭据
003_init_products_listings        商品与刊登
004_init_sku_mappings             映射
005_init_orders                   订单（含分区）
006_init_inventory                库存
007_init_finance                  财务（汇率/期间/成本/结算/利润）
008_init_approvals_audit          审批与审计
009_init_alerts                   预警
010_init_tasks_sync               任务与同步
011_init_configs                  配置与指标字典
012_init_ai                        AI 记录
013_init_reserved_ads_cs_comp     广告/客服/竞品（建表不实现）
014_init_audit_revoke             审计表权限收紧
015_init_seed_data                种子数据（角色、权限、指标、规则）
```

**迁移铁律**：

- 已合并到主分支的迁移**禁止修改**，只能新增
- 每个迁移必须提供 `downgrade()`
- 数据迁移与结构迁移分开（避免锁表时间过长）
- 大表加索引用 `CREATE INDEX CONCURRENTLY`

### 18.2 种子数据清单

`ops/seed.py` 必须初始化：

| 数据 | 内容 |
|---|---|
| 租户 | 默认租户 |
| 角色 | owner / operator / cs / finance / sre / auditor |
| 权限点 | 全部权限，含 risk_level |
| 指标字典 | PRD 11.4 全部指标（含 TACOS 唯一口径） |
| 成本规则 | 默认规则（未激活，待财务签字） |
| 预警规则 | 库存断货、利润转负、授权失效等默认规则 |
| 会计期间 | 当前月份 |

---

## 19. 评审检查清单（TDD-02 专用）

### 阻塞项（必须确认才能开工）

- [ ] **45 张表是否覆盖 Phase 1 全部场景？** 有无遗漏？
- [ ] **`orders` 不存买家明文的设计是否接受？** 这决定 ERP 打单流程是否需要调整
- [ ] **`product_listings.listing_status` 的 12 个状态是否与业务实际一致？**
- [ ] **三级贡献利润字段（pre/post fulfillment/post ads）划分是否正确？**
- [ ] **`sku_mappings` 的 EXCLUDE 约束是否会误伤正常业务？**（如同一内部 SKU 合法映射到两个平台 SKU）
- [ ] **PII 30 天删除的实现方式是否可接受？**
- [ ] **分区策略是否认同 Phase 1 就上？**
- [ ] **数据保留期是否符合合规与业务需求？**

### 建议确认

- [ ] 金额精度 `NUMERIC(18,6)` 是否足够？
- [ ] 主键用 BIGSERIAL 还是 UUID？（本设计选 BIGSERIAL，理由是索引更小）
- [ ] `tenant_id` 现在就加是否认同？
- [ ] 审计表 REVOKE 权限是否会与运维需求冲突？

### 待外部输入

- [ ] 财务口径参数（TDD-05 补充）：成本计价方法、头程分摊基数、关账日
- [ ] 首批店铺的平台、区域、履约类型
- [ ] 是否需要支持多币种结算

---

## 附录：与 PRD v1.1 第 15 章的映射

| PRD 表 | 本设计对应 | 变化 |
|---|---|---|
| `shops` | `shops` | 增加 region、fulfillment_type、capabilities、timezone |
| `shop_credentials` | `shop_credentials` + `credential_audit_logs` | 增加加密字段拆分、密钥版本、审计表 |
| `products` | `products` + `product_variants` + `product_attributes` | 拆出变体表 |
| `product_listings` | `product_listings` | 增加 submission_id、错误字段、状态枚举 |
| `sku_mapping` | `sku_mappings` | 增加 EXCLUDE 约束、is_primary |
| `price_history` | `price_history` + `pricing_rules` | 增加规则表 |
| `orders` | `orders` | **PII 改为哈希+加密**；增加分区；金额拆分为多字段 |
| `order_items` | `order_items` | 增加 title_snapshot、折扣拆分 |
| `refunds` | `refunds` | 增加自动化判定留痕、风险等级、原因归类 |
| （无） | `order_fees` | **新增**：费用归因核心表 |
| `inventory_snapshots` | `inventory_snapshots` + `inventory_policies` | 增加策略表 |
| `ad_campaigns` / `ad_reports` | 同名 + `ad_groups` | 拆出广告组 |
| `customer_messages` | 同名 | 保留 |
| `selection_candidates` | 同名 + `scoring_configs` | 增加配置表 |
| `cost_items` | `cost_items` + `cost_rules` + `exchange_rates` + `accounting_periods` | **大幅扩展**：规则版本化、汇率多口径、关账 |
| `settlement_records` | 同名 + `settlement_fees` | 拆出费用明细（对账必需） |
| `profit_snapshots` | 同名 | **重构**：三级贡献利润 + 成本项拆分 + 关账标记 |
| （无） | `approvals` + `approval_actions` | **新增**：审批状态机落地 |
| （无） | `audit_events` | **新增**：业务审计 |
| （无） | `task_runs` / `sync_cursors` / `sync_watermarks` / `raw_payloads` | **新增**：任务与同步治理 |
| `api_call_logs` | 同名 | 改为摘要 + 采样 |
| （无） | `feature_flags` | **新增**：Kill Switch |
| （无） | `ai_invocations` / `ai_eval_datasets` | **新增**：AI 治理 |
| `metric_definitions` | 同名 | 增加 approved_by、is_canonical |
| `system_configs` | 同名 | 保留 |

**净增约 15 张表**，均为落实 PRD v1.1 中"要求了但没落地"的章节（审批、审计、同步治理、Kill Switch、AI 治理）。
