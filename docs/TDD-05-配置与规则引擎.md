# TDD-05 配置与规则引擎

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0（设计评审稿） |
| 创建日期 | 2026-09-28 |
| 上游文档 | PRD v1.1、TDD-01 ~ TDD-04 |
| 文档编号 | TDD-05 |
| 严重度 | 中（不阻塞编码，但阻塞验收——"算出什么数"由本册决定） |
| 状态 | **待评审，未开工** |

---

## 0. 这份文档要解决什么问题

前四册解决了"表怎么建""接口怎么定义""状态怎么流转"。本册解决**"数字怎么算、阈值怎么定"**。

这是最容易被低估的一册。原因是：**同一个"利润"可以有十种算法，每一种都能自圆其说。** 如果不把口径写死，技术上全部正确，业务上互相矛盾。

PRD v1.1 提到三级贡献利润、TACOS、周转天数，但没有给公式。本册给出**可执行、可校验的公式与参数**。

---

## 1. 配置分层总览

### 1.1 四层配置体系

| 层级 | 存放位置 | 修改方式 | 生效时机 | 适用内容 |
|---|---|---|---|---|
| **L1 环境配置** | 环境变量 / `.env` | 改配置 + 重启 | 重启 | 数据库连接、密钥、外部端点 |
| **L2 部署配置** | 配置文件（随镜像） | 发版 | 部署 | 功能开关、日志级别、并发数 |
| **L3 业务配置** | **数据库表** | 界面修改 | 实时（有缓存失效） | 审批阈值、预警规则、成本口径 |
| **L4 数据配置** | 数据库表 | 业务录入 | 实时 | 成本项、汇率、SKU 映射 |

**核心判断：业务参数一律进 L3/L4，禁止进 L1/L2。**

理由：审批金额上限从 5000 改成 8000，不应该需要发版重启。这是 PRD 18 章"可配置"要求的落地。

### 1.2 L3 业务配置的存储方式

**两种方案对比**：

| 方案 | 优点 | 缺点 |
|---|---|---|
| A：每类配置独立表 | 类型安全、可建索引、可加约束 | 表多；新增配置项要写 migration |
| B：统一 `config_items` 键值表 | 灵活、新增无需 migration | 弱类型、易被滥用成"什么都塞" |

**本设计选 A + B 混合**：

- **结构化配置用独立表**（`approval_rules`、`alert_rules`、`cost_rules`、`scoring_rubrics`）
- **简单标量参数用统一表**（`config_items`）

```sql
-- 统一标量配置（补 TDD-02）
CREATE TABLE config_items (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    namespace     VARCHAR(64) NOT NULL,      -- business.approval / business.inventory ...
    key           VARCHAR(128) NOT NULL,
    value         JSONB NOT NULL,            -- 统一 JSONB，读取时按 schema 校验
    value_type    VARCHAR(16) NOT NULL
                  CHECK (value_type IN ('int','decimal','bool','string','duration','json')),
    -- 版本与生效
    effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to   TIMESTAMPTZ,              -- NULL = 当前生效
    -- 元数据
    description   TEXT,
    updated_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 同一时刻同一 key 只能有一个生效值
    EXCLUDE USING gist (
        tenant_id WITH =,
        namespace WITH =,
        key WITH =,
        tstzrange(effective_from, COALESCE(effective_to, 'infinity')) WITH &&
    )
);

CREATE INDEX idx_config_items_lookup ON config_items(tenant_id, namespace, key)
    WHERE effective_to IS NULL;
```

**`EXCLUDE USING gist` 的作用**：防止出现"同一 key 有两个同时生效的值"。这类 bug 极难排查（读到哪个取决于查询顺序），用数据库约束彻底消灭。

**为什么支持时间区间（`effective_from/to`）**：财务口径有时需要"从下月 1 日起生效"。有了时间区间，可以提前录入并在指定时间自动切换，不需要人守着改。

### 1.3 配置读取与缓存

```python
# core/config_runtime.py
class RuntimeConfig:
    """L3 业务配置的运行时访问。

    缓存策略：
    - Redis 缓存，TTL 60 秒
    - 变更时主动失效（写入 config_items 后 DEL 对应 key）
    - 本地进程内存缓存（TTL 10 秒），减少 Redis 压力
    """

    async def get_decimal(self, namespace: str, key: str, default: Decimal) -> Decimal: ...
    async def get_int(self, namespace: str, key: str, default: int) -> int: ...
    async def get_bool(self, namespace: str, key: str, default: bool) -> bool: ...
```

**关键约束：业务代码禁止直接读配置表**，必须经过 `RuntimeConfig`。理由是保证缓存一致性——如果一处直接查库一处走缓存，就会出现"同一个参数两个值"。

**这类 bug 的典型症状**：运营改了阈值，一部分任务生效了、一部分没生效。排查极其痛苦。

---

## 2. 财务口径参数（最高优先级）

### 2.1 三级贡献利润公式

**PRD v1.1 只给了名称，这里给公式。**

```text
【口径 1】净销售额（Net Sales）
净销售额 = 商品销售额
         + 买家承担运费
         − 折扣（coupon / deal / 促销）
         − 退款金额
         − 拒付金额（chargeback）
         − 销售相关税费（sales tax / VAT 等由卖家承担的税）

【口径 2】履约前贡献利润（Pre-Fulfillment Contribution Margin）
履约前贡献利润 = 净销售额
               − 商品销售成本（COGS）
               − 头程分摊（inbound freight allocation）

【口径 3】履约后贡献利润（Post-Fulfillment Contribution Margin）
履约后贡献利润 = 履约前贡献利润
               − 平台佣金（referral fee）
               − 配送费 / 仓储费（FBA fee / shipping fee）
               − 退货处理及不可售损失

【口径 4】广告后贡献利润（Post-Ads Contribution Margin）= 最终口径
广告后贡献利润 = 履约后贡献利润
               − 可归因广告费（attributable ad spend）
               − 促销 / 达人佣金（promotion / affiliate commission）
               − 其他可变费用（other variable fees）

【派生指标】TACOS
TACOS = 广告费 / 净销售额          ← 分母统一为净销售额
```

### 2.2 必须明确的 10 个口径细节

**这些细节是"同一算法出不同结果"的真正原因，必须逐条拍板。**

| # | 争议点 | 本设计的口径 | 理由 |
|---|---|---|---|
| 1 | COGS 是否含税 | **不含可抵扣增值税**，含不可抵扣部分 | 与大多数跨境卖家记账习惯一致 |
| 2 | 头程分摊方式 | **按重量占比**（默认），可选按货值 | 重量与运费强相关；货值法作为备选 |
| 3 | 退款是否扣减当期销售额 | **按 `settle_time` 归属**，不按 `order_time` | 财务口径优先，避免"当月退款调整上月" |
| 4 | 拒付（chargeback）单列还是并退款 | **单列**，因为费率与处理方式不同 | 拒付常伴随罚款，需单独观察 |
| 5 | 汇率使用 | **结算汇率优先**，缺失用月末中间价 | 结算汇率是实际到手汇率，最真实 |
| 6 | 广告费归因 | **平台归因报表为准**，跨期按日报分摊 | 与平台口径保持一致，避免对不上账 |
| 7 | 平台佣金是否含税 | **按平台账单原样**，不做税额拆分 | 平台账单已是最终口径 |
| 8 | 退货处理损失范围 | 含退回运费 + 不可售残值损失 + 换标费 | 完整反映退货真实成本 |
| 9 | 库存成本是否含在途 | **不含**（在途单列） | 在途资金占用单独观察，不混入 COGS |
| 10 | 促销优惠券归属 | 计入"折扣"，从净销售额扣减 | 与平台账单口径一致 |

### 2.3 成本计价方式

```python
# domain/rules/profit.py
class CostMethod(StrEnum):
    FIFO = "fifo"                    # 先进先出
    MOVING_AVERAGE = "moving_average"  # 移动加权平均
    STANDARD = "standard"            # 标准成本（少用，需人工维护）


# 配置项
COST_METHOD = "fifo"                 # 默认
```

**为什么默认 FIFO**：跨境库存有批次概念（一批货到仓成本可能不同），FIFO 更符合实际且税务上更常见。移动加权平均作为备选（适合成本波动小的品类）。

**硬性要求：成本计价方式在关账期内不可变更。** 变更必须先关账，否则期初/期末成本断裂。

### 2.4 关账冻结规则

对应 TDD-02 的 `accounting_periods`：

| 期间状态 | 允许操作 |
|---|---|
| `open` | 全量：录入成本、调整、重算 |
| `closing` | 仅录入，禁止重算 |
| `closed` | **完全冻结**：禁止任何写入，重算需先 `reopen` |
| `reopened` | 同 `open`，但必须有 reopen 审计记录 |

```sql
-- 关账校验（应用层）
-- 任何写入 profit_snapshots 的操作必须先检查 period 是否 closed
```

**关键实现：`profit_snapshots` 表加写入拦截**

```python
async def write_profit_snapshot(snapshot: ProfitSnapshot) -> None:
    period = await repo.get_period(snapshot.shop_id, snapshot.period)
    if period.status == "closed":
        raise BusinessError(
            code="PERIOD_CLOSED",
            message=f"会计期间 {snapshot.period} 已关账，禁止写入",
            action="如需修改，请先申请 reopen（需 admin 权限）",
        )
    ...
```

**为什么这个检查必须在服务层而非数据库触发器**：需要给出清晰的错误提示和操作建议（"请先 reopen"），数据库触发器做不到。

### 2.5 汇率的三种口径

对应 TDD-02 的 `exchange_rates` 表：

| 口径 | 用途 | 优先级 |
|---|---|---|
| `settlement` | 结算对账 | 1（最高） |
| `month_end` | 财务报表 | 2 |
| `daily` | 经营日报 | 3 |
| `order_date` | 订单当天（备选） | 4 |

```python
# 汇率选择优先级（配置化）
RATE_LOOKUP_PRIORITY = {
    "financial_report": ["settlement", "month_end"],
    "daily_report": ["daily", "settlement", "month_end"],
    "order_analysis": ["order_date", "daily"],
}
```

**关键规则：同一报表内必须用同一口径的汇率**。混用会导致"合计不等于分项之和"。这类问题在报表上表现为几毛钱的尾差，但很难追溯。

---

## 3. 审批规则配置

### 3.1 审批触发规则（结构化表）

```sql
-- 补 TDD-02：审批规则配置
CREATE TABLE approval_rules (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT NOT NULL,
    rule_code         VARCHAR(64) NOT NULL,
    name              VARCHAR(255) NOT NULL,
    operation_type    VARCHAR(64) NOT NULL,   -- listing_publish / price_update / refund_execute
    -- 触发条件（满足任一即需审批）
    conditions        JSONB NOT NULL,
    -- 例: {"amount_gt": 500, "discount_pct_gt": 20, "sku_count_gt": 50}
    -- 审批链
    approver_roles    VARCHAR(64)[] NOT NULL,
    required_approvals INTEGER NOT NULL DEFAULT 1,  -- 需要几人通过
    -- 时效
    timeout_hours     INTEGER NOT NULL DEFAULT 24,
    escalate_hours    INTEGER,                -- 超时升级
    -- 状态
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    version           INTEGER NOT NULL DEFAULT 1,
    effective_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, rule_code, version)
);

CREATE INDEX idx_approval_rules_active ON approval_rules(tenant_id, operation_type)
    WHERE is_active = TRUE;
```

### 3.2 默认审批规则（种子数据）

| 规则码 | 操作 | 触发条件 | 审批角色 | 超时 |
|---|---|---|---|---|
| `APR-PRICE-001` | 价格修改 | 降幅 > 20% 或 涉及 SKU > 20 个 | `ops_lead`, `admin` | 4h |
| `APR-PRICE-002` | 价格修改 | 涨幅 > 30% | `ops_lead` | 8h |
| `APR-LIST-001` | 上架 | 数量 > 50 SKU | `ops_lead` | 8h |
| `APR-LIST-002` | 上架 | 类目在受限清单中 | `compliance`, `admin` | 24h |
| `APR-REFUND-001` | 退款 | 金额 > $100 | `cs_lead`, `admin` | 4h |
| `APR-REFUND-002` | 退款 | 金额 > $500 | `finance`, `admin` | 2h |
| `APR-DATA-001` | 数据导出 | 含 PII 字段 | `compliance`, `admin` | 4h |
| `APR-CRED-001` | 凭据访问 | 任何明文读取 | `admin` | 1h |
| `APR-KILL-001` | Kill Switch | 关闭任何自动化 | `admin` | 1h |

**`APR-KILL-001` 的说明**：Kill Switch 的**开启**不需要审批（紧急情况要快），但**关闭**（恢复自动化）需要审批。这是防止"一个人误操作后随手打开"。

### 3.3 免审批额度（分级）

```python
# business.approval 命名空间
DEFAULT_APPROVAL_PARAMS = {
    # 上架
    "listing.auto_approve_max_sku_count": 20,
    "listing.restricted_categories": ["grocery", "health", "baby"],

    # 价格
    "price.auto_approve_max_drop_pct": Decimal("20"),
    "price.auto_approve_max_rise_pct": Decimal("30"),

    # 退款
    "refund.auto_approve_max_amount": Decimal("50"),
    "refund.auto_execute_max_amount": Decimal("30"),   # 自动执行（更低）
    "refund.auto_execute_max_buyer_refund_rate": Decimal("0.10"),
    "refund.auto_execute_min_product_defect_rate": Decimal("0.02"),
}
```

**注意 `auto_approve`（免审批）与 `auto_execute`（自动执行）是两个不同额度**，且 `auto_execute` 更严格。理由：免审批只是"不用人点同意"，仍可能由系统执行；自动执行是"系统直接打款给买家"，风险更高。

**这个区分在 PRD 中没有明确**，本册固化。需评审确认。

---

## 4. 预警规则配置

### 4.1 预警类型清单

| 类型 | 规则码 | 默认阈值 | 级别 | 建议动作 |
|---|---|---|---|---|
| 竞品降价 | `ALT-COMP-PRICE-001` | 竞品价格低于我方 > 5% | p2 | 查看跟价建议 |
| 竞品大促 | `ALT-COMP-PROMO-001` | 竞品出现 Deal/Coupon | p2 | 评估是否跟进 |
| 排名下滑 | `ALT-RANK-001` | BSR 排名下滑 > 30% | p1 | 检查广告与评价 |
| 排名骤升 | `ALT-RANK-002` | BSR 排名上升 > 50%（可能是好事） | p2 | 检查库存是否充足 |
| 差评爆发 | `ALT-REVIEW-001` | 24h 内新增差评 ≥ 3 条 | p1 | 检查产品质量问题 |
| 评分下降 | `ALT-REVIEW-002` | 评分下降 > 0.3 | p1 | 同上 |
| 库存预警 | `ALT-INV-001` | 可售天数 < 安全天数 | p1 | 补货 |
| 库存滞销 | `ALT-INV-002` | 可售天数 > 180 天 | p2 | 清库存 |
| 断货风险 | `ALT-INV-003` | 可售天数 < 7 天且日均销量上升 | p0 | 紧急补货 |
| 利润红线 | `ALT-PROFIT-001` | 广告后贡献利润率为负 | p1 | 检查成本与定价 |
| 广告异常 | `ALT-AD-001` | ACOS 上升 > 50% 且花费 > 阈值 | p1 | 检查关键词 |
| 退款异常 | `ALT-REFUND-001` | 某 SKU 退款率 > 10% | p1 | 检查产品质量 |
| 同步失败 | `ALT-SYNC-001` | 同步连续失败 | p0 | 检查授权 |
| 结算差异 | `ALT-SETTLE-001` | 对账差异 > 0.5% | p1 | 人工核对 |

### 4.2 可售天数（关键指标定义）

**这是最容易算错的指标，必须给出精确公式。**

```text
【可售天数 Days of Supply】
可售天数 = 当前可用库存 / 日均销量

【日均销量的三种口径（配置可选，默认口径 B）】
口径 A（快）：近 7 天日均销量
口径 B（稳）：近 30 天日均销量           ← 默认
口径 C（保守）：近 30 天日均销量 × 加权（近期权重高）

【边界处理】
- 无销量（日均 = 0）→ 可售天数显示为 "∞"，且触发滞销预警
- 库存为 0 → 显示 0，立即触发断货预警
- 新品（上架 < 30 天）→ 用口径 A，并标注"新品数据不足"
- 季节性商品 → 支持人工指定参考周期

【必须使用净销量】
净销量 = 销量 − 退货量      ← 不用毛销量，否则高退货商品会低估补货需求
```

**为什么必须用净销量**：服装类目退货率可能 30%。用毛销量算可售天数，会持续高估需求，最终压库存。

### 4.3 预警去重与抑制

```python
# 去重键构成
dedup_key = f"{alert_type}:{shop_id}:{related_id}"

# 冷却期（配置）
ALERT_COOLDOWN = {
    "p0": timedelta(minutes=15),    # 紧急：短冷却
    "p1": timedelta(hours=2),
    "p2": timedelta(hours=12),
}

# 静默期（免打扰）
QUIET_HOURS = {
    "p0": None,                     # 紧急不静默
    "p1": ("22:00", "08:00"),       # 夜间不推
    "p2": ("20:00", "09:00"),
}
```

**关键设计：p0 不静默，但 p2 必须静默**

PRD 12.4 要求"禁止默认 @所有人"。本册落实为具体的分级推送策略。**如果 p2 半夜推送，团队会关闭通知，最终 p0 也收不到——这是预警系统失效的最常见原因。**

### 4.4 推送渠道策略

| 级别 | 企业微信 | 邮件 | 短信 | @提及 |
|---|---|---|---|---|
| p0 | ✅ 立即 | ✅ | ✅ | ✅ 责任人 + 备份人 |
| p1 | ✅ 立即 | ⚠️ 汇总（每小时） | ❌ | ⚠️ 仅责任人 |
| p2 | ⚠️ 日报汇总 | ❌ | ❌ | ❌ |

**"日报汇总"的用意**：p2 数量大，逐条推送会淹没重要信息。汇总成一张日报，带跳转链接。这也符合 PRD 12.4 的"分级推送"要求。

---

## 5. 选品评分卡

### 5.1 评分卡结构（结构化表）

```sql
-- 补 TDD-02：评分卡配置
CREATE TABLE scoring_rubrics (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    rubric_code   VARCHAR(64) NOT NULL,
    name          VARCHAR(255) NOT NULL,
    -- 维度定义
    dimensions    JSONB NOT NULL,
    -- 例: [
    --   {"key":"demand","name":"需求规模","weight":0.25,
    --    "metrics":[{"metric":"monthly_search_volume","direction":"higher_better",
    --                "buckets":[[1000,0],[5000,40],[20000,80],[50000,100]]}]},
    --   ...
    -- ]
    total_weight  NUMERIC(4,3) NOT NULL DEFAULT 1.000,
    -- 阈值
    pass_threshold NUMERIC(5,2) NOT NULL DEFAULT 60,
    grade_thresholds JSONB NOT NULL DEFAULT
        '{"A":[80,100],"B":[70,80],"C":[60,70],"D":[0,60]}',
    version       INTEGER NOT NULL DEFAULT 1,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, rubric_code, version)
);
```

**`version` 字段的必要性**：评分卡必须版本化。否则改了权重后，历史评分无法解释（"为什么这个商品当时评了 75 分"）。TDD-02 的 `product_scores` 表应记录 `rubric_version`。

### 5.2 默认评分卡（6 维度）

| 维度 | 权重 | 关键指标 | 数据来源 |
|---|---|---|---|
| 需求规模 | 25% | 月搜索量、类目销量中位数 | 平台数据 + 第三方工具 |
| 竞争强度 | 20% | 头部集中度、评论数中位数、广告密度 | 平台数据 |
| 利润空间 | 25% | 预估贡献利润率、价格带位置 | 成本数据 + 平台售价 |
| 增长趋势 | 15% | 搜索量趋势、类目增速 | 趋势数据（Google Trends ⚠️） |
| 进入壁垒 | 10% | 是否需要认证、专利风险、品牌集中度 | 人工标注 + 平台数据 |
| 风险因素 | 5% | 季节性、物流限制、合规风险 | 人工标注 |

**评分计算**：

```text
总分 = Σ (维度得分 × 权重)
维度得分 = Σ (指标得分 × 指标权重)   # 维度内指标权重和为 1
指标得分 = 按 buckets 分段映射到 0-100
```

**缺失数据的处理（关键）**：

```python
# 数据缺失时的降级策略（配置化）
MISSING_DATA_POLICY = {
    "omit": "剔除该维度，按剩余权重重归一化",     # 默认
    "zero": "记 0 分",
    "neutral": "记 50 分",
    "block": "不评分，标记数据不足",
}

# 默认：权重 ≥ 20% 的维度缺失 → BLOCK（不评分）
#       权重 < 20% 的维度缺失 → OMIT（剔除 + 归一化）
```

**为什么不能简单记 0 分**：如果"需求规模"数据缺失记 0 分，这个商品永远评不上 A 级——但实际上可能是数据源问题，商品本身很好。**区分"数据缺失"与"指标很差"是评分卡设计的关键。**

**`OMIT` 后必须重归一化**：剔除 25% 权重的维度后，剩余 75% 权重要放大到 100%，否则所有分数都偏低。这是评分卡最常见的实现错误。

### 5.3 数据来源的合规与可靠性标注

**PRD v1.1 已修正：Google Trends API 仍是 Alpha 申请制。** 本册落实为数据源分级：

| 数据源 | 可靠性 | 获取方式 | 降级方案 |
|---|---|---|---|
| 平台官方 API | 高 | 授权后调用 | 无（核心依赖） |
| 平台后台数据导出 | 高 | 人工/文件导入 | — |
| 第三方选品工具 API | 中 | 采购 | 关闭该维度 |
| Google Trends | ⚠️ 低（Alpha） | 申请制，未普遍开放 | 用平台搜索数据替代 |
| 网页抓取 | ⚠️ 合规风险 | 需评估 | 禁止绕过平台限制 |

**硬约束（PRD 已定，本册固化）**：

> 达到平台限制即降频、停采或切换授权数据源；**禁止使用多账号或代理 IP 规避平台限制**。

**每个指标必须登记数据来源与可靠性**，写入 `scoring_rubrics.dimensions` 的 `source_reliability` 字段。评分结果需携带"数据置信度"，低置信度结果在界面上明确标注。

**这与 PRD 的错误修正呼应**：v1.0 写"单源差异超 25% 标低置信度"是错误逻辑（单源无法算差异）。正确逻辑是：**多源才有偏差；单源一律标"单源低置信度"。**

---

## 6. 限流与配额配置

### 6.1 三层限流

| 层级 | 限制对象 | 实现位置 | 配置来源 |
|---|---|---|---|
| L1 平台配额 | 平台 API 的调用上限 | 适配器 `client.py` | 平台文档 + 实测 |
| L2 店铺级 | 单店铺并发与速率 | 适配器 `client.py` | 配置表 |
| L3 全局级 | 系统总并发（保护自身） | Celery 队列并发 | 部署配置 |

```python
# 平台限流配置示例（Amazon）
AMAZON_RATE_LIMITS = {
    # endpoint_pattern: (rate_per_second, burst)
    "getOrders":              (0.0167, 20),    # 1/分钟（实测值，可能变化）
    "getOrderItems":          (0.5, 30),
    "createFeed":             (0.0083, 15),
    "getFeed":                (0.0083, 15),
    "searchCatalogItems":     (2.0, 2),
    "getListingsItem":        (5.0, 10),
    # ...
}
```

**关键说明：这些数值会变，且平台通常不公告。**

**应对方式（三层防护）**：

1. **配置化**：数值在配置表，可热更新，不需发版
2. **自适应**：记录实际遇到的 429，若某 endpoint 频繁 429，自动降速（乘 0.8）
3. **尊重 `Retry-After`**：平台明确告诉你就等多久，不要自作聪明

```python
# 自适应降速伪代码
if rate_limited_count_1h > 10:
    effective_rate *= 0.8
    await repo.update_rate_limit(endpoint, effective_rate)
    logger.warning("rate_limit.adaptive_throttle", endpoint=endpoint,
                   new_rate=effective_rate)
```

**为什么不直接查最新文档**：文档更新滞后于实际限制是常态。自适应比查文档可靠。

### 6.2 配额用尽处理

```python
QUOTA_EXHAUSTED_STRATEGY = {
    "daily": "计算下次重置时间，任务延迟到该时间",      # 日配额
    "monthly": "告警 + 降级（暂停非核心任务）",        # 月配额
    "burst": "短退避重试",                            # 突发限制
}
```

**"暂停非核心任务"的优先级定义**（配置化）：

| 优先级 | 任务类型 | 配额紧张时 |
|---|---|---|
| P0 | 订单同步、结算对账 | 保证 |
| P1 | 库存同步、Listing 状态 | 降频 |
| P2 | 竞品监控、排名追踪 | 暂停 |
| P3 | 历史数据回补、报表重算 | 暂停 |

---

## 7. 库存策略配置

```sql
-- TDD-02 已有 inventory_policies，此处补充字段说明
-- 关键：安全库存的多种算法
```

| 策略 | 公式 | 适用场景 |
|---|---|---|
| 固定天数 | 安全库存 = 日均销量 × 固定天数 | 简单，数据不足时用 |
| 服务水平法 | 安全库存 = Z × σ × √LT | 有销量波动数据 |
| 动态调整 | 基于近 30 天波动率自动调整 | 成熟 SKU |

```python
DEFAULT_INVENTORY_POLICY = {
    "safety_stock_days": 14,          # 默认安全天数
    "safety_stock_method": "fixed_days",
    "reorder_lead_time_days": 35,     # 补货周期（含生产+头程）
    "low_stock_alert_days": 21,
    "overstock_alert_days": 180,
    "critical_stock_alert_days": 7,
}
```

**`reorder_lead_time_days` 默认 35 天**：跨境典型周期（生产 15 天 + 头程海运 20 天）。这解释了为什么"可售 21 天"就要预警——补货来不及了。

**这是跨境与国内电商的关键差异**，不能套用国内经验（国内 Lead Time 通常 3-7 天）。

---

## 8. 功能开关（Feature Flags）

```sql
-- 补 TDD-02：功能开关
CREATE TABLE feature_flags (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT,                    -- NULL = 全局
    flag_key      VARCHAR(128) NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    -- 灰度
    rollout_pct   INTEGER NOT NULL DEFAULT 100
                  CHECK (rollout_pct BETWEEN 0 AND 100),
    allowed_shops BIGINT[],                  -- 白名单店铺
    -- 元数据
    description   TEXT,
    owner         VARCHAR(64),
    expires_at    TIMESTAMPTZ,               -- 强制清理（避免永久开关）
    updated_by    BIGINT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, flag_key)
);
```

**`expires_at` 的用意**：功能开关如果不设过期时间，会永久留在代码里，最终形成"开关地狱"（没人敢删，也没人知道开还是关）。**强制过期促使清理。**

**Phase 1 需要的开关**：

| 开关 | 默认 | 用途 |
|---|---|---|
| `enable.auto_price_update` | false | 自动改价（Phase 1 关闭） |
| `enable.auto_refund_execute` | false | 自动退款执行（Phase 1 关闭） |
| `enable.competitor_monitor` | false | 竞品监控（Phase 1 关闭） |
| `enable.ai_listing_copy` | false | AI 文案（Phase 1 关闭） |
| `enable.strict_schema_validation` | true | 严格 schema 校验 |
| `enable.shadow_compare` | false | Mock 影子比对（授权后开启） |

### 8.1 Kill Switch

**Kill Switch 与普通功能开关的区别**：

| 维度 | 功能开关 | Kill Switch |
|---|---|---|
| 粒度 | 单个功能 | 全部自动化 |
| 用途 | 灰度发布 | 紧急止血 |
| 开启权限 | 运营 | admin |
| 恢复权限 | 运营 | **需审批**（`APR-KILL-001`） |
| 审计 | 普通 | 强制详细审计 |

```python
KILL_SWITCH_SCOPES = [
    "global",                  # 全部自动化停
    "platform:amazon",         # 单平台
    "shop:{shop_id}",          # 单店铺
    "operation:write",         # 所有写操作
    "operation:refund",        # 所有退款
    "operation:price",         # 所有改价
]
```

**Kill Switch 必须是"一键生效、立即生效"**。实现上走 Redis 直读（绕过缓存），确保秒级生效。

---

## 9. 配置的审计与回滚

### 9.1 配置变更必须审计

所有 L3 配置变更写入 `audit_events`：

```python
# 配置变更审计字段
{
    "event_type": "config.changed",
    "namespace": "business.approval",
    "key": "refund.auto_execute_max_amount",
    "before": "30.00",
    "after": "50.00",
    "actor_id": 12,
    "reason": "旺季放宽自动退款额度",
    "trace_id": "..."
}
```

**`reason` 是必填**。配置变更不写原因，三个月后没人知道为什么是 50 而不是 30。

### 9.2 配置回滚

`config_items` 的时间区间设计天然支持回滚：把当前版本的 `effective_to` 设为 now()，把历史版本的 `effective_to` 设为 NULL，即完成回滚。

**必须有界面操作，不能让人手工改 SQL。** 手工改 SQL 会破坏时间区间的连续性约束。

---

## 10. 与 PRD v1.1 的差异说明

| 项 | PRD v1.1 | 本设计 | 理由 |
|---|---|---|---|
| 财务公式 | 只有三级利润名称 | 完整公式 + 10 个口径细节 | 名称无法执行 |
| COGS 是否含税 | 未指定 | 不含可抵扣增值税 | 需评审确认 |
| 汇率口径 | 提"多口径" | 4 种口径 + 选择优先级 | 落地 |
| 免审批 vs 自动执行 | 未区分 | **明确分为两个额度** | 风险等级不同 |
| 可售天数 | 提到但无公式 | 精确公式 + 4 种边界处理 | 最易算错的指标 |
| 净销量 | 未提及 | **明确规定用净销量** | 高退货品类会误算 |
| 评分卡缺失数据 | 未提及 | 4 种策略 + 重归一化 | 评分卡最常见 bug |
| 预警静默 | 提"分级推送" | p0 不静默 / p2 静默 | 具体化 |
| 限流数值 | 未提及 | 配置化 + 自适应降速 | 平台数值会变 |
| 功能开关 | 未提及 | `feature_flags` 表 + 强制过期 | 防开关地狱 |
| 配置时间区间 | 未提及 | `effective_from/to` + 排他约束 | 支持定时生效 |

---

## 11. 评审检查清单

### 必须拍板（阻塞验收）

- [ ] **Q1** 2.1 节的三级贡献利润公式是否符合你们的财务口径？（**Q1 不解决，算出来的数不可用**）
- [ ] **Q2** 2.2 节 10 个口径细节，逐条确认。特别是：**退款按 `settle_time` 归属**、**汇率优先用结算汇率**、**广告费按平台归因**
- [ ] **Q3** COGS 是否含税？（选项：不含可抵扣增值税 / 全含 / 按实际）
- [ ] **Q4** `auto_approve`（免审批）与 `auto_execute`（自动执行）分开，是否认同？（3.3）
- [ ] **Q5** 可售天数的日均销量口径默认 30 天，是否合适？（4.2）
- [ ] **Q6** 数据源合规红线（禁止多账号/代理 IP 规避限制）是否接受？

### 建议确认

- [ ] 审批规则的默认阈值（价格降幅 20%、退款 $50）是否合理？
- [ ] 预警的静默时段（p1 夜间 22:00-08:00）是否合适？
- [ ] 选品评分卡的 6 维度与权重是否符合你们的选品逻辑？
- [ ] 库存的默认 Lead Time 35 天是否符合你们的实际周期？
- [ ] Phase 1 的功能开关清单（全部关闭写操作）是否接受？

### 需外部输入

- [ ] **财务口径参数由谁签字确认？**（PRD 18.0 提到 RACI，需明确人）
- [ ] 成本计价方式（FIFO / 移动加权）由谁决定？
- [ ] 是否已有第三方选品工具采购计划？（决定评分卡维度能否落地）
- [ ] 结算汇率数据从哪里来？（平台账单 / 银行 / 第三方）

---

## 附录 A：配置命名空间总览

| 命名空间 | 承载内容 | 示例 key |
|---|---|---|
| `business.approval` | 审批阈值 | `refund.auto_execute_max_amount` |
| `business.finance` | 财务口径 | `cogs.include_deductible_tax` |
| `business.inventory` | 库存策略 | `safety_stock_days` |
| `business.alert` | 预警策略 | `alert.cooldown.p1_minutes` |
| `business.scoring` | 评分卡 | `rubric.default_code` |
| `business.rate_limit` | 限流 | `amazon.getOrders.rps` |
| `ops.sync` | 同步策略 | `orders.lookback_days` |
| `ops.retry` | 重试策略 | `max_retries.default` |

**命名规范**：`{域}.{对象}.{属性}`，全小写，点分隔。**禁止用下划线分隔域和对象**（混淆层级）。

---

## 附录 B：需回写 TDD-02 的表

本册产出 **5 张新表**，均需回写 TDD-02：

| 表名 | 用途 | 优先级 |
|---|---|---|
| `config_items` | 统一标量配置（含时间区间） | 高 |
| `approval_rules` | 审批触发规则 | 高 |
| `scoring_rubrics` | 选品评分卡 | 中 |
| `feature_flags` | 功能开关 | 中 |
| `kill_switch_states` | Kill Switch 状态（或复用 feature_flags） | 中 |

**加上 TDD-03 的 1 张（`platform_capabilities`）、TDD-04 的 2 张（`state_transitions`、`domain_events`），累计需回写 TDD-02 共 8 张表。** 建议 TDD-02 直接输出 v1.1 版本，一次性纳入。

---

## 附录 C：待补充项

| 项 | 说明 | 何时补充 |
|---|---|---|
| 各平台实际限流数值 | 需实测 | 授权后 |
| 广告归因规则细节 | Phase 2 | Phase 2 |
| AI 用例的具体阈值 | Phase 2 | Phase 2 |
| 季节性商品的参考周期配置 | 需业务输入 | 评审后 |
| 成本分摊的多种算法 | 按业务需求 | 评审后 |
