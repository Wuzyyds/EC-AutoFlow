# TDD-06 非功能设计与测试策略

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0（设计评审稿） |
| 创建日期 | 2026-09-28 |
| 上游文档 | PRD v1.1、TDD-01 ~ TDD-05 |
| 文档编号 | TDD-06 |
| 严重度 | 中（不阻塞编码，但决定"能不能上线"） |
| 状态 | **待评审，未开工** |

---

## 0. 这份文档要解决什么问题

前五册定义了"功能怎么做"。本册定义**"怎么证明它做对了、怎么保证它不出事"**。

具体回答四个问题：

1. **怎么测** —— 测试矩阵，哪些必测、测到什么程度
2. **怎么保证安全** —— 凭据、PII、权限、审计的技术落地
3. **怎么知道它坏了** —— SLO、指标、告警
4. **什么时候算做完了** —— Go/No-Go 门禁

---

## 1. 测试策略

### 1.1 测试金字塔（本项目的具体分配）

| 层级 | 比例 | 覆盖内容 | 运行时机 | 时长要求 |
|---|---|---|---|---|
| 单元测试 | 60% | 状态机、规则、金额计算、转换逻辑 | 每次提交 | < 30 秒 |
| 集成测试 | 25% | Repository、Service、Outbox 消费 | 每次提交 | < 3 分钟 |
| 契约测试 | 10% | 适配器契约、Mock vs 真实 | 每日 / 授权后 | < 5 分钟 |
| 端到端测试 | 5% | 完整业务流程（Mock 驱动） | 每日 / 发布前 | < 15 分钟 |

**关键判断：为什么单元测试要求 30 秒内完成？**

因为超过 30 秒开发者就不跑了。测试套件必须快到"保存即运行"。**慢测试的结局是没人跑，最终 CI 挂了也没人看。**

**实现手段**：
- 状态机测试纯函数化（不碰数据库）
- Repository 测试用 SQLite in-memory 或事务回滚
- 金额/规则测试无 IO

### 1.2 覆盖率要求

| 模块 | 行覆盖 | 分支覆盖 | 说明 |
|---|---|---|---|
| `domain/state_machines/` | **100%** | **100%** | 状态机必须穷举，不允许未测转换 |
| `domain/rules/` | 95% | 90% | 财务与审批规则，错了影响钱 |
| `core/money.py` | **100%** | **100%** | 金额计算必须全测 |
| `core/security/` | **100%** | **100%** | 加密与脱敏 |
| `adapters/` | 85% | 80% | 错误分支必测 |
| `services/` | 80% | 75% | 编排逻辑 |
| `repositories/` | 70% | 60% | CRUD 类代码收益递减 |
| `apps/api/routers/` | 75% | — | 接口层 |
| **整体** | **80%** | **75%** | CI 门禁 |

**覆盖率不是越高越好**：`repositories/` 追求 100% 覆盖率是浪费（大量 getter/setter）。但对状态机和金额计算，**100% 是底线**。

### 1.3 必测清单（不可协商）

**A. 状态机（TDD-04）**

| # | 测试项 | 理由 |
|---|---|---|
| 1 | 穷举所有 (状态 × 事件) 组合 | 未声明的转换必须被拒绝 |
| 2 | 终态无出边 | 防"状态回魂" |
| 3 | guard 条件全部路径 | 权限、额度、能力分支 |
| 4 | 幂等：重复触发同一事件 | 防重复上架/扣费 |
| 5 | 并发：乐观锁冲突 | 双人同时审批 |
| 6 | 联动：outbox 事件正确派发 | 审批→上架 |

**B. 金额计算（TDD-05）**

| # | 测试项 | 理由 |
|---|---|---|
| 1 | 三级贡献利润公式 | 每个口径独立验证 |
| 2 | Decimal 精度（无 float 泄漏） | 财务尾差 |
| 3 | 汇率换算 + 舍入规则 | 尾差来源 |
| 4 | 分项之和 = 合计 | 报表自洽 |
| 5 | 边界：零值、负值、超大值 | 数据健壮性 |
| 6 | 多币种混合场景 | 跨境常见 |

**C. 权限与审计（TDD-01/02）**

| # | 测试项 | 理由 |
|---|---|---|
| 1 | 职责分离：发起人不能审批自己的单 | 数据库约束 + 应用层双重验证 |
| 2 | 租户隔离：跨租户查询返回空 | 数据越权 |
| 3 | 店铺隔离：跨店铺访问被拒 | 同上 |
| 4 | 审计表不可 UPDATE/DELETE | 数据库权限验证 |
| 5 | 权限点校验：无权限操作被拒 | RBAC |

**D. 适配器（TDD-03）**

| # | 测试项 | 理由 |
|---|---|---|
| 1 | 所有异常转 AdapterError | 防裸异常逃逸 |
| 2 | raw 字段非空 | 溯源能力 |
| 3 | 429 + Retry-After 退避 | 限流正确性 |
| 4 | 部分成功逐行处理 | 批量场景 |
| 5 | 分页遍历完整不重不漏 | 数据完整性 |
| 6 | 能力不支持抛正确异常 | 降级路径 |

### 1.4 测试数据管理

```python
# tests/fixtures/ 组织
tests/fixtures/
├── factories.py          # 工厂函数（创建测试对象）
├── seed/                 # 种子数据（SQL）
├── payloads/             # 平台报文样本
└── expected/             # 预期结果（用于回归比对）
```

**关键规则**：

| 规则 | 说明 |
|---|---|
| 禁止硬编码 ID | 用工厂函数创建，避免测试间耦合 |
| 禁止测试间共享状态 | 每个测试独立事务，结束回滚 |
| 时间必须可注入 | 用 `freezegun` 或注入 clock，禁止 `datetime.now()` 直接调用 |
| 随机必须可复现 | `faker` 固定 seed |

**"时间可注入"的重要性**：审批超时、可售天数、关账这些逻辑全部依赖时间。如果测试里用真实当前时间，就无法测"31 天后超时"这类场景，只能等 31 天。

```python
# ❌ 错误
def test_approval_timeout():
    approval = create_approval(expires_at=now() + timedelta(hours=4))
    time.sleep(4 * 3600)   # 不可能这么做

# ✅ 正确
def test_approval_timeout(clock):
    approval = create_approval(expires_at=clock.now() + timedelta(hours=4))
    clock.advance(hours=5)
    expire_task.run()
    assert approval.status == "expired"
```

### 1.5 CI 流水线设计

```yaml
# .github/workflows/ci.yml 结构
stages:
  - fast-check:        # < 2 分钟，必须通过才继续
      - ruff check + format
      - mypy (strict)
      - 单元测试
  - integration:       # < 5 分钟
      - 起 PostgreSQL + Redis 容器
      - 集成测试
      - 契约测试（Mock 部分）
  - e2e:               # < 15 分钟
      - Mock 场景端到端
  - security:
      - 依赖漏洞扫描（pip-audit）
      - 密钥泄漏扫描（gitleaks）
      - SQL 注入检查（bandit）
```

**关键设计：fast-check 必须是第一道门**

理由：如果 lint 要等 10 分钟才跑，开发者就去做别的事了。**2 分钟内给出反馈是保持开发节奏的关键。**

**`test` 环境禁止出网**（TDD-01 已定义，此处落实）：

```python
# tests/conftest.py
@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """CI 中强制禁止出网。任何真实网络请求都会失败。

    理由：网络抖动会让 CI 随机失败，最终团队开始忽略失败。
    """
    def guard(*args, **kwargs):
        raise RuntimeError("测试环境禁止网络请求；请使用 Mock 适配器")
    monkeypatch.setattr(httpx.AsyncClient, "send", guard)
```

### 1.6 性能测试

| 场景 | 目标 | 方法 |
|---|---|---|
| API 读接口 P95 | < 300ms | locust |
| API 写接口 P95 | < 800ms | locust |
| 订单同步（1 万单） | < 10 分钟 | 批量脚本 |
| 利润重算（10 万行） | < 5 分钟 | 批量脚本 |
| 日报生成 | < 30 秒 | 定时任务计时 |
| 并发 100 用户 | 无错误 | locust |

**Phase 1 性能目标要诚实**：单机 Compose 部署，不要承诺"支撑百万级"。**PRD v1.1 已修正过度承诺**，本册保持：目标定位为"单店铺日订单 1 万以内、总计 10 店铺"，超出需先扩容。

---

## 2. 安全设计

### 2.1 凭据管理

**凭据的完整生命周期**：

```text
生成/获取 → 加密存储 → 运行时解密 → 使用 → 轮换 → 销毁
    ↑                                              ↓
    └──────────── 全部操作记审计 ←─────────────────┘
```

```sql
-- TDD-02 的 shop_credentials，此处说明关键字段
-- access_token_encrypted BYTEA   -- AES-256-GCM 密文
-- key_version            INTEGER  -- 支持密钥轮换
-- token_expires_at       TIMESTAMPTZ
```

```python
# core/security/crypto.py
class EnvelopeEncryption:
    """信封加密。

    - DEK（数据加密密钥）：每店铺一个，随机生成
    - KEK（密钥加密密钥）：主密钥，来自环境变量
    - 存储：DEK 用 KEK 加密后存库；数据用 DEK 加密

    为什么用信封加密而不是直接用主密钥：
    1. 轮换主密钥不需要重新加密所有数据（只重加密 DEK）
    2. 单店铺密钥泄漏不影响其他店铺
    """

    def encrypt(self, plaintext: bytes, key_version: int) -> EncryptedBlob: ...
    def decrypt(self, blob: EncryptedBlob) -> bytes: ...
```

**硬性要求**：

| # | 要求 | 验证方式 |
|---|---|---|
| 1 | 任何日志不得打印明文凭据 | 日志脱敏测试 |
| 2 | 凭据解密操作必须审计 | `credential_audit_logs` 有记录 |
| 3 | 凭据只在内存中存在最小时间 | 代码评审 |
| 4 | 支持密钥轮换且不停机 | 轮换演练 |
| 5 | 凭据读取需 `admin` 权限 | 权限测试 |
| 6 | 生产环境凭据不得出现在 `local`/`test` | 环境隔离检查 |

**"凭据只在内存中存在最小时间"的落地**：

```python
# ❌ 错误：凭据长期挂在对象上
class AmazonClient:
    def __init__(self):
        self.token = decrypt(cred)  # 一直存在

# ✅ 正确：用时才解密，用完即弃
class AmazonClient:
    def _auth_headers(self) -> dict:
        token = decrypt(self._cred_blob)
        return {"x-amz-access-token": token}
```

### 2.2 PII 处理

**PII 字段清单**：

| 表.字段 | 类型 | 处理方式 | 保留期 |
|---|---|---|---|
| `orders.buyer_encrypted` | 收货信息 | AES 加密 | **30 天**（Amazon 要求） |
| `orders.buyer_hash` | 买家标识 | 不可逆哈希 | 永久（用于分析） |
| `orders.buyer_region` | 地区 | 明文（已聚合） | 永久 |
| `customer_messages.content` | 消息正文 | 加密 | 30 天 |
| `refunds` 中的买家信息 | — | 只存 `buyer_hash` | 永久 |

**30 天删除的实现**：

```python
# apps/worker/tasks/pii_cleanup.py
@celery.task
def pii_cleanup() -> None:
    """每日执行：删除超过 30 天的 PII。

    必须：
    1. 置 NULL（而非删行）
    2. 记录删除审计（含被删记录的 ID 列表）
    3. 记录数量指标（用于监控）
    """
    cutoff = now() - timedelta(days=30)
    deleted = await repo.nullify_pii_before(cutoff)
    await audit.record("pii.purged", count=deleted, cutoff=cutoff)
    metrics.gauge("pii.purged.rows", deleted)
```

**为什么"置 NULL 而非删行"**：订单记录本身是业务数据（用于财务分析），不能删。只删除其中的 PII 部分。

**日志脱敏**：

```python
# core/security/masking.py
MASK_RULES = {
    "email": r"^(.{2}).*@(.*)$",           # ab***@gmail.com
    "phone": r"^(\d{3}).*(\d{2})$",        # 138****12
    "name": lambda s: s[0] + "*" * (len(s) - 1),
    "address": lambda s: s[:6] + "...",
    "token": lambda s: "***REDACTED***",
    "card": r"^(\d{4}).*(\d{4})$",
}
```

**硬约束：日志中只允许白名单字段**

```python
# 结构化日志的白名单机制
LOG_WHITELIST = {
    "order": ["id", "shop_id", "order_status", "grand_total", "currency",
              "order_time", "buyer_region"],       # 注意：无 buyer_encrypted
    "listing": ["id", "platform_sku", "listing_status", "price", "currency"],
    "api_call": ["endpoint", "method", "status_code", "duration_ms"],
}
```

**不采用"黑名单"方式**（列出禁止字段），因为新增字段容易漏。**白名单是安全默认。**

### 2.3 权限模型（RBAC）

**权限点格式**：`{资源}:{动作}`

```python
PERMISSIONS = [
    # 商品
    "listing:read", "listing:create", "listing:update", "listing:delete",
    "listing:publish",              # 提交上架（走审批）
    "listing:publish_direct",       # 免审批上架（高权限）
    # 价格
    "price:read", "price:update",
    "price:update_bulk",            # 批量改价（走审批）
    # 订单
    "order:read", "order:export", "order:ship_confirm",
    # 财务
    "finance:read", "finance:read_margin", "finance:cost_manage",
    "finance:period_close", "finance:period_reopen",
    # 售后
    "refund:read", "refund:approve", "refund:execute",
    # 审批
    "approval:submit", "approval:approve", "approval:admin",
    # 凭据
    "credential:view_masked",       # 看掩码
    "credential:view_plaintext",    # 看明文（admin）
    "credential:rotate",
    # 系统
    "system:config_manage", "system:user_manage", "system:kill_switch",
    "system:audit_read",
]
```

**角色定义**：

| 角色 | 权限范围 |
|---|---|
| `admin` | 全部 |
| `ops_lead` | 商品、价格、库存全部 + 审批 |
| `ops` | 商品、价格、库存只读 + 提交审批 |
| `cs` | 订单、售后只读 + 提交退款审批 |
| `cs_lead` | cs + 审批退款 |
| `finance` | 财务全部 + 关账 |
| `analyst` | 全部只读（不含凭据明文） |
| `compliance` | 审计读取 + 合规相关审批 |
| `viewer` | 仅报表查看 |

**关键设计：三个权限必须分离**

| 权限 | 分离理由 |
|---|---|
| `credential:view_plaintext` | 只有 admin，且必须审计 |
| `finance:period_close` vs `period_reopen` | 关账容易，重开难（防随意改动已关账数据） |
| `listing:publish` vs `publish_direct` | 普通用户走审批，管理员可直发 |

### 2.4 审计设计

**审计的三层结构**：

| 层 | 表 | 内容 | 保留期 |
|---|---|---|---|
| 业务事实 | `audit_events` | 谁改了什么、审批了什么 | 36 个月 |
| 状态流转 | `state_transitions` | 状态怎么变的 | 24 个月 |
| 技术元数据 | `api_call_logs` | 调了哪个接口、耗时、错误分类 | 90 天 |

**三层分离的理由**：查询场景不同。查"谁把价格改了"用第一层；查"这单卡在哪一步"用第二层；查"为什么超时"用第三层。混在一张表里，字段互相冗余且查询低效。

**append-only 的强制手段（三层防护）**：

```sql
-- 1. 数据库权限
REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC;
REVOKE UPDATE, DELETE ON state_transitions FROM PUBLIC;

-- 2. 应用层（ORM 只读模型）
-- 3. 定期校验（对账检查行数只增不减）
```

```python
# ops/verify_append_only.py
def verify_append_only() -> None:
    """每日校验：审计表行数只增不减、无 UPDATE 痕迹。

    实现：比对每日快照的行数与最大 ID。
    发现异常 → 立即告警（可能有人绕过权限直连数据库修改）
    """
```

**为什么需要第三层校验**：数据库权限可以被有 DBA 权限的人绕过。**定期校验是最后的防线**，也是合规审计需要的证据。

### 2.5 其他安全措施

| 项 | 措施 |
|---|---|
| 传输加密 | 全站 HTTPS，平台 API 强制 TLS 1.2+ |
| 数据库加密 | 静态加密（云盘加密）+ 敏感字段列级加密 |
| SQL 注入 | 全部参数化查询；禁止字符串拼接 SQL |
| 依赖漏洞 | `pip-audit` 纳入 CI |
| 密钥泄漏 | `gitleaks` 纳入 CI |
| 会话管理 | JWT + 短期 access token + refresh token |
| 密码策略 | bcrypt（cost ≥ 12）；最小长度 12 |
| 速率限制 | 登录接口限流，防爆破 |
| CSRF | 前端用 SameSite cookie + token |
| 文件上传 | 类型白名单 + 大小限制 + 病毒扫描（Phase 2） |

---

## 3. 可观测性设计

### 3.1 SLI / SLO 定义

| # | SLI（指标） | SLO（目标） | 测量窗口 | 错误预算 |
|---|---|---|---|---|
| 1 | API 可用性 | 99.5% | 30 天 | 3.6 小时 |
| 2 | API 读 P95 延迟 | < 300ms | 7 天 | — |
| 3 | 订单同步及时性 | 95% 的订单在 30 分钟内同步 | 7 天 | — |
| 4 | 数据新鲜度 | 95% 时间 lag < 60 分钟 | 7 天 | — |
| 5 | 同步任务成功率 | ≥ 99%（首次）/ ≥ 99.9%（最终） | 7 天 | — |
| 6 | 上架任务成功率 | ≥ 98%（首次）/ ≥ 99.5%（最终） | 7 天 | — |
| 7 | 报表准时率 | ≥ 99%（非 100%，见下） | 30 天 | — |
| 8 | 审批平均时长 | < 4 小时（P50） | 30 天 | — |

**关于目标值的诚实说明**：

- **报表准时率不设 100%**。原因：外部依赖（平台 API 故障）不可控。设 100% 会导致必然无法达成，团队逐渐忽视告警。**PRD v1.1 已修正此过度承诺**。
- **首次成功率与最终成功率必须分开**（PRD 20.1 要求）。首次反映系统质量，最终反映系统韧性。合并会掩盖重试风暴。
- **错误预算（Error Budget）**：当错误预算耗尽（如可用性低于 99.5%），**暂停新功能开发，优先修复稳定性**。这是一个组织性约定，不只是技术指标。

### 3.2 指标清单

**RED 指标（请求型）**：

```
api_requests_total{endpoint, method, status}           # Rate
api_request_duration_seconds{endpoint, quantile}       # Duration
api_errors_total{endpoint, error_category}             # Errors
```

**USE 指标（资源型）**：

```
process_cpu_usage, process_memory_usage
db_pool_active_connections, db_pool_wait_seconds
redis_connected_clients, redis_memory_used_bytes
celery_queue_depth{queue}
celery_worker_active_tasks
```

**业务指标（最重要）**：

```
sync_last_success_timestamp{shop_id, resource}         # 数据新鲜度
sync_freshness_lag_minutes{shop_id, resource}
sync_records_synced_total{shop_id, resource}
listing_submissions_total{platform, result}
listing_success_rate{platform, attempt_type}   # first / final
orders_synced_total{shop_id}
profit_calculation_duration_seconds
alerts_generated_total{level, type}
alerts_acknowledged_total{level}
approvals_pending_count{risk_level}            # 待办积压
approvals_duration_seconds{risk_level, quantile}  # 审批效率
adapter_errors_total{platform, error_category}    # 平台健康度
quota_usage_ratio{platform, shop_id}             # 配额水位
```

**关键设计：`sync_last_success_timestamp` 是最重要的指标**

它是"数据新不新鲜"的唯一真相。**任何数据异常排查的第一步都是看这个指标。** 告警规则：超过预期周期的 2 倍未更新 → p0 告警。

### 3.3 结构化日志

```python
# 统一日志格式（structlog）
{
    "timestamp": "2026-09-28T12:00:00.000Z",
    "level": "info",
    "event": "listing.submitted",
    "trace_id": "abc123",
    "tenant_id": 1,
    "shop_id": 5,
    "platform": "amazon",
    "listing_id": 1234,
    "submission_id": "feed-xyz",
    "duration_ms": 234,
    "message": "Listing 已提交"
}
```

**硬性要求**：

| # | 要求 |
|---|---|
| 1 | `trace_id` 贯穿全链路（API → Service → Adapter → Task） |
| 2 | 禁止 `print`；禁止 f-string 拼接（用结构化字段） |
| 3 | 事件名用 `{域}.{动作}` 格式（`listing.submitted`） |
| 4 | 错误日志必须含 `error_category` 与 `retryable` |
| 5 | 日志字段遵守白名单（2.2 节） |
| 6 | 生产日志级别 INFO，禁止 DEBUG（例外需临时开启并记录） |

**`trace_id` 的传递实现**：

```python
# middleware 注入 trace_id
# API 请求 → contextvar → Service → Adapter → 记入 api_call_logs
# Celery 任务 → 从任务参数读取 → 同理

# 好处：一个 trace_id 能串起
#   API 请求日志 + SQL 慢查询 + 平台调用日志 + 任务执行日志
```

**这是排障的核心基础设施**。没有它，跨服务/跨任务的排查靠猜。

### 3.4 告警规则（本项目自身）

| 告警 | 条件 | 级别 | 动作 |
|---|---|---|---|
| API 错误率 | 5xx > 1%（5 分钟） | p0 | 页面 + 通知 |
| API 延迟 | P95 > 1s（10 分钟） | p1 | 通知 |
| 数据库连接池 | 使用率 > 90% | p1 | 通知 |
| 队列积压 | depth > 1000（10 分钟） | p1 | 通知 |
| 同步停滞 | `sync_last_success` > 2× 周期 | p0 | 通知 |
| 平台错误激增 | `adapter_errors` 某分类 > 10/min | p1 | 通知 |
| 配额告警 | `quota_usage_ratio` > 80% | p1 | 通知 |
| 审批积压 | `approvals_pending` > 50 | p2 | 日报 |
| 磁盘空间 | > 85% | p1 | 通知 |
| 备份失败 | 备份任务失败 | p0 | 立即通知 |
| PII 清理失败 | 任务失败 | p0 | 立即通知（合规风险） |
| 审计校验失败 | 行数异常减少 | p0 | 立即通知（安全事件） |

**最后两条必须 p0**：PII 未按期删除是合规违规；审计表异常是安全事件。两者都不能"明天再看"。

### 3.5 链路追踪

**Phase 1 不引入 Jaeger/Zipkin**（符合 TDD-01 不引入清单）。用 `trace_id` + 结构化日志实现轻量追踪。

```bash
# 排障示例：查一个 trace 的全链路
grep "trace_id=abc123" /var/log/ec-autoflow/*.log | jq -s 'sort_by(.timestamp)'
```

**升级时机**：当日均请求 > 10 万或排查耗时明显增加时，再评估引入 OpenTelemetry。

---

## 4. 备份与容灾

### 4.1 备份策略

| 对象 | 方式 | 频率 | 保留 | RPO |
|---|---|---|---|---|
| PostgreSQL | 全量 + WAL 归档 | 每日全量 + 持续 WAL | 30 天 | ≤ 15 分钟 |
| Redis | RDB 快照 | 每小时 | 7 天 | ≤ 1 小时（可重建） |
| 文件存储 | 对象存储版本化 | 实时 | 90 天 | 0 |
| 配置 | 数据库备份覆盖 + Git | 随代码 | 永久 | — |
| 加密密钥 | 独立安全存储 | 手动 | 永久 | — |

**RPO ≤ 15 分钟的实现**：PostgreSQL 的 `archive_mode = on` + WAL 持续归档。恢复时可恢复到任意时间点（PITR）。

**RTO ≤ 4 小时的实现**：

```bash
# ops/restore.sh
1. 从备份恢复 PostgreSQL（约 30-60 分钟）
2. 回放 WAL 到目标时间点
3. 启动 Redis（清空，由任务重建）
4. 启动 API + Worker
5. 健康检查
6. 触发一次全量同步（补齐恢复期间的数据缺口）
```

**恢复后必须做全量同步**：因为恢复期间平台数据仍在变化，本地会出现缺口。**这一步最容易被遗漏，导致恢复后数据不完整却没人发现。**

### 4.2 备份验证（关键）

**未验证的备份等于没有备份。**

```python
# ops/verify_backup.py（每周执行）
def verify_backup() -> None:
    """备份可恢复性验证。

    1. 将最新备份恢复到临时实例
    2. 校验关键表行数（与生产对比，差异 < 1%）
    3. 校验数据完整性（外键、约束）
    4. 执行抽样查询
    5. 销毁临时实例，记录结果
    """
```

**这是必须自动化的**。手工验证的结局是永远不验证，然后在真需要时发现备份是坏的。

**演练要求**：

| 频率 | 内容 |
|---|---|
| 每周 | 备份可恢复性自动验证 |
| 每月 | 恢复演练（恢复到临时环境，记录 RTO 实测） |
| 每季 | 全流程灾难演练（含通知、决策、恢复、验证） |

### 4.3 降级策略

| 故障 | 降级方案 | 用户可见影响 |
|---|---|---|
| 平台 API 不可用 | 用缓存数据 + 标记"数据可能过期" | 数据不新鲜 |
| 数据库主库故障 | 切只读从库 | 无法写入，只能查看 |
| Redis 故障 | 任务暂停，API 降级为无缓存 | 变慢，任务延迟 |
| LLM 服务不可用 | 关闭 AI 功能，走人工 | AI 功能不可用 |
| 通知服务不可用 | 降级为邮件 + 站内消息 | 通知延迟 |
| 存储不可用 | 报表暂停生成，保留数据 | 无新报表 |

**关键原则：降级必须"可见"**。用户必须知道数据可能过期、功能已降级。**静默降级比不降级更危险**——用户会基于过期数据做决策还以为数据是新的。

```python
# 降级状态暴露到 API
{
    "data": [...],
    "degraded": True,
    "degraded_reason": "平台 API 不可用，数据为 2026-09-28 10:00 缓存",
    "data_freshness": "stale"
}
```

---

## 5. 上线门禁（Go / No-Go）

### 5.1 Go / No-Go 检查清单

**以下每项必须全部通过才能上线。**

**A. 功能完整性**

- [ ] Phase 1 功能清单（TDD-01 §5.3）全部实现
- [ ] 所有 `NotImplementedError` 仅出现在 Phase 2 预留方法中
- [ ] 端到端流程可用 Mock 完整跑通

**B. 测试**

- [ ] 单元测试全绿
- [ ] 集成测试全绿
- [ ] 覆盖率达标（整体 ≥ 80%，状态机/金额 100%）
- [ ] 状态机穷举测试通过
- [ ] 权限与租户隔离测试通过
- [ ] 性能测试达标

**C. 安全**

- [ ] 无明文凭据出现在日志/代码/配置
- [ ] PII 脱敏生效（抽样验证）
- [ ] PII 30 天清理任务运行正常
- [ ] 审计表 append-only 生效（尝试 UPDATE 失败）
- [ ] 依赖漏洞扫描无高危
- [ ] 密钥扫描无泄漏
- [ ] 权限矩阵逐角色验证

**D. 可观测性**

- [ ] 所有关键指标已采集
- [ ] 告警规则已配置并测试触发
- [ ] `trace_id` 全链路贯通
- [ ] 日志格式统一且脱敏

**E. 运维**

- [ ] 备份任务运行正常
- [ ] **备份恢复已验证**（不只是"备份成功了"）
- [ ] 恢复演练完成并记录 RTO
- [ ] Kill Switch 测试有效
- [ ] 健康检查端点可用
- [ ] 部署脚本可重复执行（幂等）

**F. 文档**

- [ ] 6 册 TDD 与代码一致
- [ ] API 文档（OpenAPI）完整
- [ ] 运维手册（启动、停止、回滚、排障）
- [ ] 数据字典与 TDD-02 一致
- [ ] 已知限制清单（诚实列出）

**G. 数据**

- [ ] 种子数据完整（角色、权限、默认规则）
- [ ] Alembic migration 可从零执行
- [ ] migration 回滚测试通过

### 5.2 上线后观察期

| 阶段 | 时长 | 要求 |
|---|---|---|
| 灰度 | 1-3 天 | 单店铺、只读、观察指标 |
| 扩大 | 3-7 天 | 多店铺、只读 + 手动写 |
| 全量 | 持续 | 全部功能，每日 review 指标 |

**观察期核心指标**：同步新鲜度、错误率、配额使用、用户反馈。

**任何 p0 告警 → 立即回滚**。这是硬规则，不讨论"再看看"。

### 5.3 回滚方案

```bash
# ops/rollback.sh
1. 切回上一个应用版本（镜像 tag）
2. 数据库 migration 回滚（若本次有 migration）
3. 清理 Redis 缓存（避免旧格式缓存）
4. 健康检查
5. 验证核心流程
```

**关键约束：migration 必须向后兼容**

| 场景 | 要求 |
|---|---|
| 新增表 | 安全（旧版本忽略） |
| 新增可空字段 | 安全 |
| 新增非空字段（有默认值） | 安全 |
| 删除字段 | **危险**：需分两次发布（先停止使用，再删除） |
| 修改字段类型 | **危险**：需详细评估 |
| 重命名字段 | **危险**：需分步（加新 → 双写 → 切读 → 删旧） |

**"删除字段需分两次发布"是铁律**。因为回滚时旧代码会找不到字段直接崩溃。

---

## 6. 运维手册要点（骨架）

### 6.1 日常运维

| 任务 | 频率 | 操作 |
|---|---|---|
| 检查数据新鲜度 | 每日 | 看 `sync_last_success_timestamp` |
| 检查待审批 | 每日 | 看 `approvals_pending_count` |
| 检查配额水位 | 每日 | 看 `quota_usage_ratio` |
| 检查磁盘 | 每日 | 自动告警 |
| 证书续期 | 每月 | 自动 + 提醒 |
| 依赖更新 | 每月 | 评估 + 测试 + 灰度 |
| 密钥轮换 | 每季度 | 按流程执行 |

### 6.2 常见故障处理

| 症状 | 排查顺序 |
|---|---|
| 数据不更新 | ① `sync_last_success` → ② `sync_cursors.status` → ③ `api_call_logs` 错误 → ④ 凭据有效期 |
| 上架卡住 | ① `listing_status` → ② `state_transitions` 历史 → ③ `submission_id` 轮询状态 |
| 报表数字不对 | ① 关账状态 → ② 汇率口径 → ③ 成本项生效时间 → ④ `profit_snapshots` 明细 |
| 任务积压 | ① 队列深度 → ② worker 数量 → ③ 单任务耗时 → ④ 是否被限流 |
| 频繁告警 | ① 去重键是否生效 → ② 冷却期配置 → ③ 阈值是否过松 |

### 6.3 关键运维命令

```bash
# Makefile 中定义，不允许临时敲命令
make status            # 系统状态总览
make sync-health       # 同步健康检查
make queue-depth       # 队列深度
make quota-status      # 配额水位
make kill-switch-on    # 紧急停止（需确认）
make kill-switch-off   # 恢复（需审批）
make backup-now        # 手动备份
make verify-backup     # 备份验证
make rollback          # 回滚
```

**"不允许临时敲命令"的理由**：临时命令不会记录、不会复用、出错了无法追溯。**所有运维动作必须走 Makefile**，这既是效率也是审计。

---

## 7. 与 PRD v1.1 的差异说明

| 项 | PRD v1.1 | 本设计 | 理由 |
|---|---|---|---|
| 覆盖率要求 | 未指定 | 分层要求 + 整体 80% | 可验证的门禁 |
| 状态机测试 | 未指定 | **100% 穷举** | 状态机是核心风险 |
| 时间可注入 | 未提及 | 强制要求 | 否则超时逻辑无法测 |
| 凭据加密 | 提"加密存储" | 信封加密 + 密钥轮换 | 落地细节 |
| PII 清理 | 提"30 天删除" | 置 NULL + 审计 + 指标 | 落地细节 |
| 日志白名单 | 提"脱敏" | 明确白名单机制 | 白名单比黑名单安全 |
| 审计三层 | 提"append-only" | 三层分离 + 三重防护 | 分层设计 |
| SLO 目标值 | 已修正过度承诺 | 8 项 SLI + 错误预算 | 落地 |
| 备份验证 | 未提及 | **强制验证可恢复性** | 未验证的备份等于没有 |
| 上线门禁 | 提"Go/No-Go" | 7 大类 40+ 项清单 | 可执行 |
| 回滚兼容 | 未提及 | migration 向后兼容铁律 | 回滚的前提 |
| 性能目标 | 提"支撑规模" | 明确单机目标（1 万单/日） | 诚实定位 |

---

## 8. 评审检查清单

### 必须拍板

- [ ] **Q1** 覆盖率要求（整体 80%、状态机/金额 100%）是否接受？（会显著影响工期）
- [ ] **Q2** SLO 目标值（可用性 99.5%、P95 300ms）是否符合预期？
- [ ] **Q3** RPO ≤ 15 分钟 / RTO ≤ 4 小时是否满足业务要求？
- [ ] **Q4** 性能目标（10 店铺、单店日 1 万单）是否覆盖 Phase 1 需求？
- [ ] **Q5** 上线门禁 40+ 项是否全部纳入发布流程？可否裁剪？

### 建议确认

- [ ] 每周备份验证 + 每月恢复演练的频率是否可行？
- [ ] 权限矩阵（8 个角色）是否符合组织实际？
- [ ] 告警规则的阈值是否合适？
- [ ] Phase 1 是否真的不引入链路追踪系统？

### 需外部输入

- [ ] **部署环境**（境内/境外）——影响备份策略、证书、是否需要代理层
- [ ] 是否有合规要求（如等保、SOC2）——影响审计与加密强度
- [ ] 值班机制由谁负责——影响告警渠道与级别定义
- [ ] 上线时间窗口——影响观察期安排

---

## 附录 A：6 册 TDD 索引与状态

| 编号 | 名称 | 状态 | 严重度 |
|---|---|---|---|
| TDD-01 | 技术设计总纲 | ✅ 已完成 | 高 |
| TDD-02 | 数据模型设计 | ✅ 已完成（**待出 v1.1，含 8 张补充表**） | 阻塞级 |
| TDD-03 | 平台适配器契约 | ✅ 已完成 | 阻塞级 |
| TDD-04 | 核心状态机 | ✅ 已完成 | 阻塞级 |
| TDD-05 | 配置与规则引擎 | ✅ 已完成 | 中 |
| TDD-06 | 非功能设计与测试策略 | ✅ 已完成（本册） | 中 |

---

## 附录 B：回写 TDD-02 的完整清单（累计 8 张表 + 3 个字段）

| # | 类型 | 名称 | 来源册 | 用途 |
|---|---|---|---|---|
| 1 | 表 | `platform_capabilities` | TDD-03 | 能力矩阵落库 |
| 2 | 表 | `state_transitions` | TDD-04 | 状态转换事件 |
| 3 | 表 | `domain_events` | TDD-04 | 事务性 outbox |
| 4 | 表 | `config_items` | TDD-05 | 统一标量配置 |
| 5 | 表 | `approval_rules` | TDD-05 | 审批触发规则 |
| 6 | 表 | `scoring_rubrics` | TDD-05 | 选品评分卡 |
| 7 | 表 | `feature_flags` | TDD-05 | 功能开关 |
| 8 | 表 | `kill_switch_states` | TDD-05 | Kill Switch 状态 |
| 9 | 字段 | `orders.anomaly_flag` | TDD-04 | 订单异常换向标记 |
| 10 | 字段 | `orders.anomaly_detail` | TDD-04 | 异常详情 |
| 11 | 字段 | `approvals.retry_count` | TDD-04 | 执行重试次数 |

**建议**：TDD-02 直接产出 v1.1，一次性纳入全部补充项，避免多次往返。

---

## 附录 C：外部事实登记（延续 PRD v1.1 §21.5）

**本文档中依赖外部事实的条目，需在实施前核实。**

| # | 事实陈述 | 来源 | 置信度 | 核实时机 | 若为假的影响 |
|---|---|---|---|---|---|
| 1 | Amazon PII 需 30 天内删除 | 官方开发者协议条款 | 高 | 实施前 | 合规风险 |
| 2 | Amazon Messaging API 无历史消息读取 | 官方 API 文档 | 高 | 已核实 | 客服模块降级 |
| 3 | Amazon 无卖家侧执行退款 API | 官方 API 文档 | 中高 | 实施前 | 自动化退款降级 |
| 4 | Amazon SP-API 限流具体数值 | 官方文档（会变） | 中 | 实施时 | 需自适应降速 |
| 5 | 各平台错误码清单 | 官方文档 | 中 | 实施时 | 需补映射 |
| 6 | Temu / 速卖通能力矩阵标注 ⚠️ 项 | **未核实** | **低** | **技术预研** | 架构假设可能错误 |

**第 6 条最需要关注**：Temu 与速卖通的能力矩阵有较多"未知"。**在技术预研实测前，这两个平台的排期不应被视为可靠。**

---

## 附录 D：待补充项

| 项 | 说明 | 何时补充 |
|---|---|---|
| 各平台限流实测数值 | 授权后实测 | 授权后 |
| 性能基准实测数据 | 实现后压测 | 实现后 |
| 值班手册细化 | 需组织输入 | 上线前 |
| 合规映射（等保/SOC2） | 需合规输入 | 评审后 |
| OpenAPI 文档 | 实现时生成 | 实现时 |
