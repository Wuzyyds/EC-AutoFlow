# EC-AutoFlow

AI 全链路电商自动化平台 —— 覆盖选品、上架、素材、客服售后、财务核算、平台对接、报表、竞品监控八大模块。

---

## 功能模块

| 模块 | 职责 |
|---|---|
| 选品 | 市场与竞品数据分析，输出选品评分与建议 |
| 上架 | 商品信息生成、批量刊登、上架状态跟踪 |
| 素材 | 商品图片与视频素材的生成与管理 |
| 客服售后 | 消息处理、退款与售后工单流转 |
| 财务核算 | 订单经营、履约贡献、结算对账、会计四类报表 |
| 平台对接 | 各平台 API 适配、凭据管理、限流与重试 |
| 报表 | 经营指标汇总、归因分析、关账 |
| 竞品监控 | 竞品价格与动态采集、预警 |

---

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12+ |
| Web 框架 | FastAPI |
| ORM | SQLAlchemy 2.0 |
| 数据库 | MySQL 8.0.16+ |
| 迁移 | Alembic |
| 队列与缓存 | Celery + Redis 7.x |
| 前端 | Vue 3 + Element Plus |
| 部署 | Docker Compose |

---

## 当前状态

**Phase 1 开发中（骨架阶段）**。基础设施、数据模型、核心契约与状态机已完成，业务编排与入口层待实现。

| 层 | 状态 | 说明 |
|---|---|---|
| `core/` | ✅ 完成 | 配置、枚举（30）、异常、Decimal 金额、强制 UTC 时间、信封加密、脱敏、DB 引擎 |
| `core/models/` | ✅ 完成 | 46 张业务表 ORM 模型（690 列），全部 InnoDB |
| `domain/state_machines/` | ✅ 完成 | 5 个状态机 / 37 个状态 + 声明式框架 |
| `adapters/` | ✅ 完成 | 契约层、能力矩阵（35）、错误分类（26）、Mock 适配器、Amazon 骨架 |
| `migrations/` | ✅ 完成 | `initial_schema` 已建表（48 张，含 alembic_version） |
| `ops/` | ✅ 完成 | 环境健康检查、CHECK 约束降级为触发器的生成脚本（54 条已生效） |
| `repositories/` | ⏳ 待实现 | 数据访问（含租户过滤注入） |
| `services/` | ⏳ 待实现 | 业务编排（含 Outbox 消费） |
| `integrations/` | ⏳ 待实现 | LLM / 通知 / 对象存储 |
| `apps/api/` | ⏳ 待实现 | 接口层（FastAPI） |
| `apps/worker/` | ⏳ 待实现 | 任务层（Celery） |
| `apps/web/` | ⏳ 待实现 | 前端（Vue 3） |
| `tests/` | ⏳ 待实现 | 测试套件（状态机穷举、约束有效性） |

**本地数据库实测**（2026-09-29）：MySQL 8.0.12，48 张表 / 690 列 / 100% InnoDB / 54 个触发器；业务表暂无数据，种子脚本待补。

---

## 快速开始

### 前置要求

| 组件 | 版本要求 | 说明 |
|---|---|---|
| Python | ≥ 3.12 | |
| MySQL | **≥ 8.0.16** | 低于此版本 CHECK 约束不强制执行，见下方"重要约束" |
| Redis | 7.x | 队列与缓存 |

### 初始化

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows
# .venv/bin/python -m pip install -e ".[dev]"       # macOS / Linux

# 2. 复制配置
cp .env.example .env
# 按需修改 .env 中的数据库连接与密钥

# 3. 创建数据库（字符集必须 utf8mb4）
mysql -u root -p -e "CREATE DATABASE ec_autoflow CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"

# 4. 执行迁移（建表）
python -m alembic upgrade head

# 5. 生成约束（CHECK 或触发器，按 MySQL 版本自适应）
python ops/generate_constraints.py --apply

# 6. 环境自检（连通性、字符集、表结构、Redis）
python ops/healthcheck.py
```

### 运行

> 入口层（`apps/`）尚未实现，以下命令待对应模块完成后可用。

```bash
python -m uvicorn apps.api.main:app --reload                # API 服务
python -m celery -A apps.worker.celery_app worker -l info   # 任务 worker
python -m pytest                                            # 测试
```

### 使用 Makefile（需安装 make）

```bash
make help          # 查看全部命令
make setup         # 一键初始化
make test          # 跑测试
make run-api       # 起 API
```

---

## 重要约束

以下几条是踩过坑后固化的硬规则，**修改代码前请先读**。

### 1. MySQL 版本必须 ≥ 8.0.16

MySQL 8.0.16 之前的版本会**解析 `CHECK` 语法但完全忽略它** —— 不报错、不警告、不生效。

本项目对此的处置是双保险：

- 能用 CHECK 就用 CHECK（8.0.16+）
- 用不了就自动降级为**触发器**（`ops/generate_constraints.py` 按版本自适应）

无论哪种方式，都必须通过"约束有效性测试"验证：

```bash
python -m pytest tests/integration/test_constraints_enforced.py -v
```

### 2. 时间只写 UTC

数据库时间列一律 `DATETIME(6)`，存 **naive UTC**。

- 获取当前时间只能用 `core.timeutil.utc_now()`
- **禁止 `datetime.now()`**（返回本地 naive 时间）
- 写库前经 `to_db()` 校验，传入 naive 值会直接报错

为什么这么严：MySQL 没有 `TIMESTAMPTZ`，时区信息一旦丢失就无法恢复。
让错误在写入前暴露，好过三个月后报表对不上账。

### 3. 金额只用 Decimal

- 数据库 `DECIMAL(18,6)`，Python `Decimal`
- 舍入统一 `ROUND_HALF_UP`（财务惯例），**不用 Python 默认的银行家舍入**
- 分摊用 `core.money.allocate()`，它保证分项之和精确等于总额

### 4. 枚举值存 UPPER_SNAKE

数据库存 `'ACTIVE'`、`'PENDING_APPROVAL'`，Python 侧用 `StrEnum`。

**唯一真相是 `core/constants.py`**。禁止在任何地方硬编码枚举字符串字面量。

### 5. 适配器方法不得抛非 AdapterError 异常

所有平台异常必须经 `translate_errors` 装饰器转换为 `AdapterError`。
业务层只看 `error.category`，不看平台原始错误码。

### 6. 平台差异只在适配器内

业务代码出现 `if platform == "amazon"` 即为设计缺陷。
分支条件应该是**能力**（`Capability`），不是**平台名**。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/PRD-AI全链路电商自动化平台-v1.1.md`](docs/PRD-AI全链路电商自动化平台-v1.1.md) | 产品需求（立项基线） |
| [`docs/TDD-01-技术设计总纲.md`](docs/TDD-01-技术设计总纲.md) | 设计原则、技术栈、目录结构、ADR |
| [`docs/TDD-02-数据模型设计.md`](docs/TDD-02-数据模型设计.md) | 数据模型（PostgreSQL 版，MySQL 版见 ADR-007） |
| [`docs/TDD-03-平台适配器契约.md`](docs/TDD-03-平台适配器契约.md) | 适配器接口、错误分类、能力矩阵 |
| [`docs/TDD-04-核心状态机.md`](docs/TDD-04-核心状态机.md) | 5 个状态机定义与转换表 |
| [`docs/TDD-05-配置与规则引擎.md`](docs/TDD-05-配置与规则引擎.md) | 财务口径、审批阈值、预警规则 |
| [`docs/TDD-06-非功能设计与测试策略.md`](docs/TDD-06-非功能设计与测试策略.md) | 测试、安全、可观测、上线门禁 |
| [`docs/ADR-007-数据库变更为MySQL.md`](docs/ADR-007-数据库变更为MySQL.md) | **数据库选型变更与全部适配细节** |

**遇到文档与代码冲突时以代码为准，但必须回写文档。**

---

## 目录结构

```
.
├── core/               # 基础层（框架无关工具）
│   ├── config.py       # 分层配置
│   ├── constants.py    # 全部枚举（唯一真相）
│   ├── exceptions.py   # 异常体系
│   ├── money.py        # Decimal 金额工具
│   ├── timeutil.py     # UTC 时间工具
│   └── security/       # 加密与脱敏
├── domain/             # 领域层（无外部依赖）
│   ├── state_machines/ # 5 个状态机
│   ├── rules/          # 业务规则
│   └── value_objects/  # 值对象
├── adapters/           # 平台适配器
│   ├── base.py         # 抽象契约
│   ├── errors.py       # 统一错误分类
│   ├── capabilities.py # 能力矩阵
│   └── mock/           # Mock 适配器（Phase 1 核心）
├── repositories/       # 数据访问
├── services/           # 业务编排
├── integrations/       # 外部系统（LLM、通知、存储）
├── apps/               # 入口层
│   ├── api/            # FastAPI
│   ├── worker/         # Celery
│   └── web/            # Vue 3
├── migrations/         # Alembic
├── ops/                # 运维脚本
└── tests/              # 测试
```

### 依赖方向（不可逆）

```
apps/ → services/ → domain/ → core/
                  ↘ repositories/ · adapters/
```

`domain/` **不得** import `sqlalchemy`、`httpx`、`fastapi`。

---

## 数据库迁移

```bash
python -m alembic revision --autogenerate -m "描述"   # 生成迁移
python -m alembic upgrade head                        # 应用
python -m alembic downgrade -1                        # 回滚一步
```

**迁移必须向后兼容**（TDD-06 §5.3）：

- 新增表 / 可空字段 → 安全
- 删除字段 → **危险**，需分两次发布（先停用，再删除）
- 重命名字段 → **危险**，需分步（加新 → 双写 → 切读 → 删旧）

原因：回滚时旧代码会找不到字段直接崩溃。

---

## 版本控制与回滚

本仓库为**私有仓库**，用于单人开发的版本管理与故障回滚。

### 推送门禁

代码**不随改随推**。每次推送前必须依次通过四道检查：

| 顺序 | 检查项 | 通过标准 |
|---|---|---|
| 1 | 功能完成 | 该功能可运行、自测通过 |
| 2 | 测试通过 | `python -m pytest` 全绿 |
| 3 | 代码审核 | 依赖方向未越界、枚举未硬编码、命名符合规范 |
| 4 | 安全审核 | 无凭据硬编码、无 PII 泄漏、日志已脱敏 |

四项齐备后才执行 `git push`。与功能无关的文件（本地配置、缓存、临时产物）一律不入库。

### 提交信息约定

| 前缀 | 用途 |
|---|---|
| `feat:` | 新功能 |
| `fix:` | 缺陷修复 |
| `refactor:` | 重构（不改变外部行为） |
| `db:` | 迁移、约束、种子数据 |
| `docs:` | 文档 |
| `chore:` | 构建、依赖、配置 |

### 代码回滚

```bash
git log --oneline                 # 找到目标提交
git revert <commit>               # 安全回滚：生成反向提交，保留历史（推荐）
git reset --hard <commit>         # 强制回退：丢弃后续提交，仅限本地未推送时使用
```

### 数据库回滚（必须与代码回滚配套）

```bash
python -m alembic current          # 查看当前版本
python -m alembic downgrade -1     # 回滚一步
python -m alembic upgrade head     # 重新应用
```

**代码回滚与迁移回滚必须同步**。若只回滚代码不回滚迁移，或反之，会出现"代码找不到字段"或"字段无人使用"的不一致。这也是上文要求迁移必须向后兼容的原因。

### 绝不提交的内容

| 内容 | 原因 |
|---|---|
| `.env` | 含数据库密码与主加密密钥（KEK） |
| `.venv/` | 本地虚拟环境，体积大且可重建 |
| `__pycache__/`、各类缓存 | 运行时产物 |
| `.workbuddy-ai/` | AI 工作区数据，非项目资产 |

`.gitignore` 已覆盖以上全部。新增本地文件前，先确认它未被 git 跟踪：

```bash
git status --short          # 查看未跟踪与已修改文件
git check-ignore -v <path>  # 确认某文件是否被忽略
```
