# EC-AutoFlow 技术设计文档（TDD）

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0（设计评审稿） |
| 创建日期 | 2026-09-28 |
| 上游文档 | `PRD-AI全链路电商自动化平台-v1.1.md` |
| 文档编号 | TDD-01（总纲） |
| 状态 | **待评审，未开工** |

---

## 0. 这份文档是什么，不是什么

### 0.1 是什么

把 PRD v1.1 中"描述性需求"翻译成"可执行规格"。开发人员拿到本系列文档，不需要再问"这个字段是什么类型""这个状态能不能跳过去"。

属于本系列的内容：

- 数据库 DDL（含类型、约束、索引、分区）
- 接口契约（请求/响应结构、错误码）
- 状态机定义（状态集合、转换规则、触发条件）
- 配置 schema（阈值、重试、限额）
- 模块依赖与初始化顺序
- 测试策略与验收脚本

### 0.2 不是什么

**本系列不重复 PRD 已写内容**。业务价值、用户角色、竞品分析、产品路线图请查 PRD。发现两者冲突时，**以本系列为准**（因为它更晚、更具体），并回写 PRD 修正。

### 0.3 文档分册

| 编号 | 名称 | 内容 | 阻断开发的严重度 |
|---|---|---|---|
| TDD-01 | 技术设计总纲 | 设计原则、栈锁定、目录结构、依赖、环境 | 高 |
| TDD-02 | 数据模型设计 | 完整 DDL、索引、分区、数据字典 | **阻塞级** |
| TDD-03 | 平台适配器契约 | 接口定义、错误分类、能力矩阵、Mock 实现 | **阻塞级** |
| TDD-04 | 核心状态机 | 上架、审批、同步、订单、售后状态机 | **阻塞级** |
| TDD-05 | 配置与规则引擎 | 阈值配置、评分卡、限流、成本口径参数 | 中 |
| TDD-06 | 非功能设计与测试策略 | 安全、可观测、备份、测试矩阵、验收脚本 | 中 |

**建议评审顺序**：TDD-02 → TDD-04 → TDD-03 → 其余。因为数据模型和状态机一旦定错，返工成本最高。

---

## 1. 设计原则（决定后续所有取舍）

### 1.1 十条硬性原则

| # | 原则 | 具体含义 | 违反后果 |
|---|---|---|---|
| 1 | **平台差异只在适配器内** | 业务代码出现 `if platform == "amazon"` 即视为设计缺陷 | 每加一个平台就要改业务代码 |
| 2 | **写入必须幂等** | 所有写操作携带 `idempotency_key`，重复提交返回首次结果 | 重复上架、重复退款、重复扣费 |
| 3 | **金额只用 Decimal** | 禁止 float；数据库用 `NUMERIC(18,6)`，Python 用 `Decimal` | 财务尾差、对账失败 |
| 4 | **时间只存 UTC** | 数据库 `TIMESTAMPTZ`；展示层按店铺时区转换 | 跨时区报表错位 |
| 5 | **原始数据不可变** | 平台原始报文写入后不修改，规范化数据可重建 | 无法追溯、无法重放 |
| 6 | **事实表只追加** | 价格、库存、排名等变更类数据 append-only | 丢失历史，无法做趋势 |
| 7 | **高危操作走审批** | 通过统一审批状态机，不允许绕过 | 误改价、误退款 |
| 8 | **租户与店铺隔离** | 所有业务表含 `tenant_id`，查询默认带店铺过滤 | 数据越权 |
| 9 | **外部依赖可降级** | 每个外部调用有超时、重试、熔断、降级路径 | 单点故障拖垮全局 |
| 10 | **可观测优先** | 结构化日志 + 指标 + 链路 ID，禁止裸 print | 线上无法排障 |

### 1.2 关于"简单"的取舍

这里要坦白一个重要判断：**v1.1 的完整愿景（8 平台、自动客服、AI 素材、自动写操作）在 Phase 1 不该碰。** 本系列文档明确区分：

- **Phase 1 必做**：数据模型、适配器骨架、审批状态机、只读同步、日报、结算对账
- **Phase 1 预留接口**：广告、客服、竞品、素材的表结构和适配器方法（建表但不实现）
- **Phase 1 明确不做**：任何自动写操作、AI 场景图、多平台写入

**预留不等于实现。** 表建好、接口定义好，但方法体抛 `NotImplementedError`，这样后续扩展不动架构。

---

## 2. 技术栈锁定

### 2.1 版本锁定表

选型在 PRD 已定，这里锁到具体版本，避免"你写 3.12 我写 3.11"这类问题。

| 组件 | 版本 | 锁定原因 |
|---|---|---|
| Python | 3.12.x | 用 `type` 语句、`match`；3.13 生态尚未完全跟上 |
| FastAPI | ^0.115 | 需要 Pydantic v2 支持 |
| Pydantic | ^2.9 | v2 性能与校验能力 |
| SQLAlchemy | ^2.0 | 2.0 风格的 `select()`；异步 ORM |
| Alembic | ^1.13 | migration 管理 |
| asyncpg | ^0.30 | PostgreSQL 异步驱动 |
| PostgreSQL | 16.x | 分区表、JSONB、窗口函数 |
| Redis | 7.x | 队列 + 缓存 + 限流 |
| Celery | ^5.4 | 任务队列 |
| httpx | ^0.27 | 异步 HTTP（替代 requests） |
| tenacity | ^9.0 | 重试策略（替代手写退避） |
| Alembic + SQLAlchemy 组合 | — | 不引入 ORM 之外的 Query Builder |
| structlog | ^24 | 结构化日志 |
| pytest + pytest-asyncio | ^8 / ^0.24 | 测试 |
| ruff | ^0.7 | lint + format（替代 black + isort + flake8） |
| mypy | ^1.11 | 类型检查（strict 模式） |
| Node.js | 22 LTS | 前端构建 |
| Vue | ^3.5 | 前端框架 |
| Vite | ^5.4 | 构建工具 |
| Element Plus | ^2.8 | 组件库 |
| Docker Compose | v2 | 本地与初期部署 |

### 2.2 明确不引入的组件（及理由）

**这一节很重要，避免后续被"加个 Kafka 吧"带偏。**

| 组件 | 不引入理由 | 何时再评估 |
|---|---|---|
| Kafka / RabbitMQ | Celery + Redis 足够处理千万级日订单；引入后运维成本翻倍 | 日订单 > 100 万或需要事件流回放 |
| Temporal | MVP 用 Celery Canvas + 状态表就够；两套编排系统是灾难 | 出现跨天长流程、多级人工等待、补偿事务 |
| Kubernetes | 单机 Compose 能满足 Phase 1；K8s 会吃掉大量运维精力 | 需要多节点水平扩展或高可用 |
| Elasticsearch | PostgreSQL 全文检索 + JSONB 能满足商品搜索 | 需要复杂全文检索或日志分析（可先用 Loki） |
| 独立数据仓库 | Phase 1 数据量小，PostgreSQL 直接算 | 需要跨系统分析或数据量超单机 |
| LangChain / LlamaIndex | 直接调 LLM API 更可控；框架抽象层带来调试困难 | 需要复杂 RAG 或 Agent 编排 |
| GraphQL（对外） | pREST 场景 REST 足够；GraphQL 增加前端复杂度 | 前端需要灵活聚合多资源 |

### 2.3 依赖管理

```toml
# 使用 uv 管理依赖（比 pip/poetry 快一个数量级）
# pyproject.toml 关键片段

[project]
name = "ec-autoflow"
requires-python = ">=3.12,<3.13"
dependencies = [
    "fastapi>=0.115,<0.116",
    "pydantic>=2.9,<3",
    "pydantic-settings>=2.5",
    "sqlalchemy[asyncio]>=2.0,<2.1",
    "alembic>=1.13,<2",
    "asyncpg>=0.30,<0.31",
    "celery[redis]>=5.4,<6",
    "redis>=5.0,<6",
    "httpx>=0.27,<0.28",
    "tenacity>=9.0,<10",
    "structlog>=24.1",
    "cryptography>=43",
    "openpyxl>=3.1",         # Excel 报表
    "pillow>=10.4",          # 素材处理
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "ruff>=0.7",
    "mypy>=1.11",
    "faker>=30",
]
```

**为什么用 uv**：依赖解析速度是 pip 的 10-100 倍，且能管理虚拟环境和 Python 版本。CI 中能显著缩短反馈时间。

---

## 3. 项目目录结构

### 3.1 完整结构

```text
ec-autoflow/
├── AGENTS.md                          # 根规则（Codex 必读）
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── docker-compose.yml
├── docker-compose.prod.yml
├── Makefile                           # 统一命令入口
│
├── .codex/                            # Codex 配置
│   ├── config.toml
│   └── hooks.toml
│
├── apps/
│   ├── api/                           # FastAPI 服务
│   │   ├── AGENTS.md
│   │   ├── main.py
│   │   ├── deps.py                    # 依赖注入
│   │   ├── routers/
│   │   │   ├── auth.py
│   │   │   ├── shops.py
│   │   │   ├── listings.py
│   │   │   ├── orders.py
│   │   │   ├── inventory.py
│   │   │   ├── finance.py
│   │   │   ├── reports.py
│   │   │   ├── approvals.py
│   │   │   └── admin.py
│   │   └── schemas/                   # Pydantic DTO（API 层）
│   │       ├── common.py
│   │       ├── listing.py
│   │       ├── order.py
│   │       └── ...
│   │
│   ├── worker/                        # Celery 任务
│   │   ├── AGENTS.md
│   │   ├── celery_app.py
│   │   ├── beat_schedule.py           # 定时任务定义
│   │   └── tasks/
│   │       ├── sync/                  # 数据同步任务
│   │       │   ├── orders.py
│   │       │   ├── inventory.py
│   │       │   ├── listings.py
│   │       │   └── settlement.py
│   │       ├── listing/               # 上架任务
│   │       │   ├── validate.py
│   │       │   ├── submit.py
│   │       │   └── poll_status.py
│   │       ├── finance/
│   │       │   ├── profit_calc.py
│   │       │   └── reconcile.py
│   │       └── report/
│   │           ├── daily_business.py
│   │           ├── inventory_alert.py
│   │           └── render_image.py
│   │
│   └── web/                           # Vue 前端
│       ├── AGENTS.md
│       ├── src/
│       │   ├── api/
│       │   ├── views/
│       │   ├── components/
│       │   └── stores/
│       └── vite.config.ts
│
├── core/                              # 领域核心（框架无关）
│   ├── config.py                      # pydantic-settings 配置
│   ├── constants.py                   # 枚举常量
│   ├── exceptions.py                  # 统一异常体系
│   ├── money.py                       # Decimal 金额工具
│   ├── timeutil.py                    # UTC/时区工具
│   └── security/
│       ├── crypto.py                  # 信封加密
│       └── masking.py                 # PII 脱敏
│
├── domain/                            # 领域模型与业务规则
│   ├── entities/                      # 领域实体（非 ORM）
│   ├── value_objects/                 # 值对象（Money、SKU、Address）
│   ├── rules/                         # 业务规则（可配置）
│   │   ├── approval.py                # 审批规则
│   │   ├── pricing.py                 # 定价规则
│   │   ├── profit.py                  # 成本口径规则
│   │   └── scoring.py                 # 选品评分卡
│   └── state_machines/                # 状态机定义
│       ├── base.py
│       ├── listing.py
│       ├── approval.py
│       └── sync.py
│
├── adapters/                          # 平台适配器
│   ├── AGENTS.md
│   ├── base.py                        # PlatformAdapter 抽象基类
│   ├── capabilities.py                # 能力枚举与声明
│   ├── errors.py                      # 统一错误分类
│   ├── registry.py                    # 适配器注册表
│   ├── mock/                          # Mock 适配器（Phase 1 核心）
│   │   ├── adapter.py
│   │   └── fixtures/
│   └── amazon/
│       ├── AGENTS.md
│       ├── adapter.py
│       ├── auth.py                    # LWA OAuth
│       ├── client.py                  # 带限流与重试的 HTTP 客户端
│       ├── endpoints/                 # 按 API 分组
│       │   ├── listings.py
│       │   ├── catalog.py
│       │   ├── orders.py
│       │   ├── inventory.py
│       │   ├── reports.py
│       │   └── finance.py
│       ├── transform.py               # 原始 → 规范化
│       └── error_map.py               # 错误码映射
│
├── repositories/                      # 数据访问层
│   ├── base.py                        # 通用 CRUD + 租户过滤
│   ├── shop_repo.py
│   ├── listing_repo.py
│   ├── order_repo.py
│   ├── inventory_repo.py
│   ├── finance_repo.py
│   ├── approval_repo.py
│   └── task_repo.py
│
├── services/                          # 业务编排层
│   ├── listing_service.py
│   ├── sync_service.py
│   ├── finance_service.py
│   ├── report_service.py
│   ├── approval_service.py
│   └── notification_service.py
│
├── integrations/                      # 外部系统
│   ├── llm/
│   │   ├── gateway.py                 # 多模型路由
│   │   ├── prompts/                   # 提示词模板（版本化）
│   │   └── evaluators/                # 输出校验
│   ├── notify/
│   │   ├── wecom.py
│   │   └── feishu.py
│   └── storage/
│       └── s3.py
│
├── migrations/                        # Alembic
│   ├── env.py
│   └── versions/
│
├── tests/
│   ├── conftest.py
│   ├── unit/                          # 单元测试
│   ├── contract/                      # 适配器契约测试
│   ├── integration/                   # 集成测试
│   ├── fixtures/                      # 测试数据
│   └── evals/                         # AI 评测
│
├── ops/                               # 运维脚本
│   ├── backup.sh
│   ├── restore.sh
│   └── healthcheck.py
│
└── docs/                              # 设计文档
    ├── TDD-01-技术设计总纲.md
    ├── TDD-02-数据模型设计.md
    ├── TDD-03-平台适配器契约.md
    ├── TDD-04-核心状态机.md
    ├── TDD-05-配置与规则引擎.md
    └── TDD-06-非功能设计与测试策略.md
```

### 3.2 分层职责与依赖方向

```text
┌──────────────────────────────────────────────────┐
│  apps/api  ·  apps/worker  ·  apps/web           │  ← 入口层
└────────────────────┬─────────────────────────────┘
                     │ 只能向下依赖
┌────────────────────▼─────────────────────────────┐
│  services/                                       │  ← 业务编排
└────────────────────┬─────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────┐
│  domain/（规则、状态机）                          │  ← 业务规则
└────────────────────┬─────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────┐
│  repositories/  ·  adapters/                     │  ← 数据与外部
└──────────────────────────────────────────────────┘
```

**依赖规则（写进 AGENTS.md 由 Codex 强制）**：

- 上层可依赖下层，**下层禁止依赖上层**
- `domain/` 不得 import `sqlalchemy`、`httpx`、`fastapi`（保持框架无关）
- `adapters/` 不得 import `services/`
- 跨层调用必须经过 `services/`
- 同层横向调用尽量避免；必须时通过明确接口

### 3.3 关键目录的 AGENTS.md 要点

每个子目录的 AGENTS.md 只写**该目录独有的**约束，通用规则继承根 AGENTS.md（避免撑爆 32KiB 上限）。

**adapters/amazon/AGENTS.md 必须包含**：

```markdown
- 区域 Base URL 三套（NA/EU/FE），必须按 shop.region 路由，硬编码即 bug
- 所有调用经 client.py，禁止裸 httpx，否则绕过限流
- Feeds 是异步的：提交成功 ≠ 上架成功，必须轮询 processingReport
- 属性 schema 必须从 Product Type Definitions API 动态获取
- 错误码映射统一放 error_map.py，不允许在业务逻辑里判断原始错误码
- 测试必须覆盖 429 / 401 / 500 / 207 四条分支
```

---

## 4. 环境与配置

### 4.1 多环境定义

| 环境 | 用途 | 数据 | 外部依赖 |
|---|---|---|---|
| `local` | 本地开发 | 种子数据 | 全部 Mock |
| `test` | CI 自动化测试 | 内存/PG 临时库 | 全部 Mock，禁止出网 |
| `sandbox` | 平台沙箱联调 | 平台沙箱数据 | 真实沙箱 API |
| `staging` | 预生产 | 脱敏生产副本 | 真实 API（只读） |
| `prod` | 生产 | 真实 | 真实 API |

**硬性约束**：

- `test` 环境**必须禁止出网**（通过 httpx transport 拦截），否则 CI 会因网络抖动随机失败
- `staging` 对平台只读，禁止任何写操作（在适配器层加开关）
- 生产数据禁止直接复制到 `local`/`test`，必须脱敏

### 4.2 配置分层（pydantic-settings）

配置来源优先级：环境变量 > `.env` > 默认值。敏感配置**只允许环境变量**，不进文件。

```python
# core/config.py 结构示意
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_nested_delimiter="__", extra="forbid"
    )

    env: Literal["local", "test", "sandbox", "staging", "prod"]
    debug: bool = False

    database: DatabaseSettings        # url, pool_size, echo
    redis: RedisSettings              # url, db
    storage: StorageSettings          # endpoint, bucket, access_key
    crypto: CryptoSettings            # master_key（仅环境变量）
    llm: LLMSettings                  # provider, api_key, model, timeout
    notify: NotifySettings            # wecom_webhook, feishu_webhook
    platforms: dict[str, PlatformSettings]  # 按平台分组的凭据与限流
    business: BusinessSettings        # 审批阈值、预警阈值、成本口径

    @model_validator(mode="after")
    def validate_prod_safety(self):
        # prod 环境强制校验：禁止 debug、必须配置加密密钥
        ...
```

**关键设计**：`business` 分组承载所有**可配置业务参数**（审批金额上限、毛利红线、预警阈值），避免这些值硬编码在代码里。

### 4.3 敏感配置处理

| 配置类型 | 存放位置 | 加密方式 |
|---|---|---|
| 数据库密码 | 环境变量 | 由部署平台提供 |
| 平台 App Secret | 环境变量 | 由部署平台提供 |
| 店铺 Token | **数据库**（`shop_credentials`） | AES-256-GCM 信封加密，密钥来自环境变量 |
| LLM API Key | 环境变量 | 由部署平台提供 |
| 主加密密钥 | 环境变量 / KMS | 不落盘 |

**为什么店铺 Token 放数据库而不用环境变量**：店铺数量会增长，环境变量改一次要重启服务。放数据库可动态增删，但必须加密存储。

### 4.4 Makefile 统一命令

```makefile
.PHONY: install dev test lint typecheck migrate seed run-worker run-api

install:      uv sync --all-groups
dev:          docker compose up -d && make migrate
test:         uv run pytest -q --cov=. --cov-report=term-missing
lint:         uv run ruff check . && uv run ruff format --check .
typecheck:    uv run mypy .
migrate:      uv run alembic upgrade head
seed:         uv run python ops/seed.py
run-api:      uv run uvicorn apps.api.main:app --reload
run-worker:   uv run celery -A apps.worker.celery_app worker -l info
```

**约定**：所有开发动作必须通过 Makefile。Codex 生成的命令若不在 Makefile 中，必须补进去。

---

## 5. 模块依赖与开发顺序

### 5.1 依赖关系图

```text
TDD-02 数据模型 ──────┬──→ 基础：所有模块都依赖
                     │
TDD-04 状态机 ───────┼──→ 上架、审批、同步
                     │
TDD-03 适配器契约 ───┴──→ 同步、上架

开发顺序（严格）：
1. core/        配置、异常、金额、时间、加密    ← 无依赖
2. domain/      规则、状态机                    ← 依赖 core
3. repositories/ 数据访问                      ← 依赖 core + 数据模型
4. adapters/    接口 + Mock 实现               ← 依赖 core + domain
5. services/    业务编排                       ← 依赖以上全部
6. apps/api     接口层                         ← 依赖 services
7. apps/worker  任务层                         ← 依赖 services
8. apps/web     前端                           ← 依赖 api
```

### 5.2 为什么 Mock 适配器是 Phase 1 核心

这是本设计最重要的一个判断，必须解释清楚。

**问题**：真实平台授权可能 1-2 个月才能拿到，但开发不能等。

**方案**：`adapters/mock/` 实现完整的 `PlatformAdapter` 接口，用预置的、真实的平台响应样本（fixture）驱动。它的价值不是"假数据占位"，而是：

1. **锁定接口契约**：Mock 和 Amazon 适配器实现同一接口，接口错了会立刻暴露
2. **可测试**：Mock 能模拟 429、超时、部分成功、字段缺失等异常分支，真实沙箱做不到
3. **验证数据模型**：用真实响应结构建表，避免等授权后大改 DDL
4. **可演示**：能跑通完整业务链路给评审看

**关键要求**：Mock 的 fixture 必须是**从官方文档或真实响应中提取的真实结构**，不能自己编。第一版 Mock 用官方文档示例，后续拿到授权后用真实响应替换。

**风险提示**：Mock 与真实的差异是真实存在的。所以设计上要求 —— 拿到授权后，**用真实响应做一轮契约比对测试**，逐字段验证。

### 5.3 Phase 1 交付边界（代码级）

| 模块 | Phase 1 状态 | 说明 |
|---|---|---|
| 数据模型 | ✅ 完整实现 | 所有表建好，含预留字段 |
| 适配器接口 | ✅ 完整定义 | 全部方法签名 + 错误分类 |
| Mock 适配器 | ✅ 完整实现 | 支撑全链路测试 |
| Amazon 适配器 | ⚠️ 骨架 + 认证 | 拿到授权后填充，只做只读 |
| 上架状态机 | ✅ 完整实现 | 但终止在"待审批"，不自动提交 |
| 审批状态机 | ✅ 完整实现 | 含超时、委托、撤回 |
| 同步任务 | ✅ 完整实现 | 订单、库存、Listing、结算只读 |
| 利润计算 | ✅ 完整实现 | 依赖财务口径参数（TDD-05） |
| 报表 | ✅ 日报 + 库存预警 | 图片卡片渲染 |
| AI 能力 | ⚠️ 仅网关 + 版本管理 | 不接业务，为 Phase 2 预留 |
| 前端 | ⚠️ 核心页面 | 总览、审批、报表查看 |
| 广告 / 客服 / 竞品 / 素材 | ❌ 建表不实现 | 方法抛 NotImplementedError |

---

## 6. 关键设计决策记录（ADR）

记录重要决策及理由，避免后续反复讨论。

### ADR-001：为什么用 Celery 而不是 asyncio 后台任务

**决策**：Celery + Redis

**理由**：
- 需要定时任务（Beat），asyncio 无内建调度
- 需要任务持久化、重试、DLQ，Celery 开箱即有
- 任务需要跨进程/跨机器扩展
- 需要可视化监控（Flower）

**代价**：Celery 的任务序列化要求参数可序列化，不能直接传 ORM 对象。**约束**：任务参数只传 ID 和基础类型，任务内重新查库。

### ADR-002：为什么同步与异步混用

**决策**：API 层全异步（FastAPI + asyncpg），Celery 任务层同步（SQLAlchemy Sync Session）

**理由**：
- FastAPI 异步能支撑高并发读
- Celery worker 是同步模型，强行异步会增加复杂度且无收益
- 两套 Session 共用同一份模型定义

**代价**：需要维护两套 Session 配置。**约束**：`repositories/` 必须同时提供 async 和 sync 两套接口，或在任务层用 `asyncio.run()` 包装（不推荐，有事件循环冲突风险）。**推荐做法**：repository 提供 sync 版本给 worker，async 版本给 API。

### ADR-003：为什么不引入 ORM 之外的 Query Builder

**决策**：SQLAlchemy 2.0 + 原生 SQL（复杂报表）

**理由**：报表类查询用 ORM 表达会非常别扭且低效；直接用 SQL 更清晰

**约束**：原生 SQL 必须放在 `repositories/` 内，且必须参数化（禁止字符串拼接）

### ADR-004：金额统一为 Decimal，数据库用 NUMERIC(18,6)

**决策**：所有金额字段 `NUMERIC(18,6)`

**理由**：6 位小数足以容纳汇率换算中间结果；18 位整数部分支持大金额

**约束**：Python 侧统一封装 `core/money.py`，禁止直接运算裸 Decimal（避免忘记 quantize）

### ADR-005：为什么 Phase 1 必须做 Mock 适配器

见 5.2 节。**核心理由**：授权等待期不能浪费，且 Mock 能测真实沙箱测不了的异常分支。

### ADR-006：租户字段现在就加，即使当前单租户

**决策**：所有业务表含 `tenant_id`，Phase 1 固定为默认租户

**理由**：后期加租户字段需要改所有表、所有索引、所有查询，成本极高；现在加的成本几乎为零

**约束**：`repositories/base.py` 统一注入租户过滤，业务代码不手写

---

## 7. 与 PRD v1.1 的差异说明

本设计对 PRD 做了以下调整，需评审确认：

| 项 | PRD v1.1 | 本设计 | 理由 |
|---|---|---|---|
| 表数量 | 约 30 张（描述式） | 约 45 张（含审计、审批、配置、同步水位） | PRD 未列审批状态、同步游标、配置版本等必要表 |
| 适配器接口 | 泛化方法签名 | 精确到参数类型与错误分类 | 泛化签名无法直接编码 |
| Mock 适配器 | 未提及 | 列为 Phase 1 核心交付 | 解决授权等待期问题 |
| 金额精度 | 未指定 | 统一 NUMERIC(18,6) | 避免浮点与精度争议 |
| 时间处理 | 提"UTC 存储" | 明确 TIMESTAMPTZ + 店铺时区展示 | 落地细节 |
| Phase 1 前端 | 9 个页面 | 收敛为 4 个核心页面 | 资源约束下的取舍 |

---

## 8. 评审检查清单

评审 TDD 系列时，请重点确认以下问题：

### 必须拍板（阻塞开发）

- [ ] **Q1** 数据模型是否覆盖 Phase 1 全部场景？有无遗漏的表或字段？（对立项最关键）
- [ ] **Q2** 状态机的状态集合与转换规则是否与业务实际一致？（尤其是上架与审批）
- [ ] **Q3** 适配器接口的粒度是否合适？过粗会导致业务代码写死逻辑，过细会导致适配器臃肿
- [ ] **Q4** 财务口径参数（TDD-05）由谁签字确认？
- [ ] **Q5** Mock fixture 的数据来源是否可接受？（官方文档示例 vs 真实响应）

### 建议确认

- [ ] 目录结构是否符合团队习惯？
- [ ] 技术栈与不引入清单是否认同？
- [ ] Phase 1 功能边界（5.3 节）是否接受裁剪？
- [ ] 6 个 ADR 决策是否认同？

### 需外部输入

- [ ] 团队规模与角色分工（决定并行度）
- [ ] 部署环境（境内/境外，影响是否需代理层）
- [ ] 首批平台与店铺数量

---

## 附录 A：术语与命名约定

| 概念 | 命名 | 说明 |
|---|---|---|
| 租户 | `tenant_id` | 顶层隔离单位 |
| 内部 SKU | `internal_sku` | 我方主键，全局唯一 |
| 平台 SKU | `platform_sku` | 平台侧标识，店铺内唯一 |
| 平台商品 ID | `platform_item_id` | ASIN / item_id 等 |
| 幂等键 | `idempotency_key` | 写入去重 |
| 请求追踪 | `trace_id` | 全链路日志关联 |
| 平台 | `platform` | `amazon` / `tiktok` / `temu` / ... |
| 区域 | `region` | `na` / `eu` / `fe` |
| 业务口径时间 | `order_time` | 下单时间（经营视角） |
| 结算口径时间 | `settle_time` | 结算时间（财务视角） |

**命名规范**：

- 数据库：表名复数小写蛇形（`orders`），字段蛇形（`created_at`）
- Python：类 PascalCase，函数/变量 snake_case，常量 UPPER_SNAKE
- 枚举值：数据库存 UPPER_SNAKE 字符串，Python 侧用 `StrEnum`
- API 路径：kebab-case 复数（`/api/v1/shop-credentials`）

---

## 附录 B：本文档待补充项

TDD-01 完成后，以下分册需继续编写：

| 分册 | 关键内容 | 预估篇幅 |
|---|---|---|
| TDD-02 | 45 张表完整 DDL + 数据字典 + 索引 + 分区策略 | 高 |
| TDD-03 | 适配器接口 + 错误分类 + 能力矩阵 + Mock 实现 | 高 |
| TDD-04 | 5 个状态机（上架/审批/同步/订单/售后） | 中 |
| TDD-05 | 配置 schema + 评分卡 + 成本口径参数 | 中 |
| TDD-06 | 安全、可观测、备份、测试矩阵、验收脚本 | 中 |
