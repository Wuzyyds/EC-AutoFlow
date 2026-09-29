"""基础设施表：配置、同步、任务、告警、原始报文、API 日志、能力矩阵。

配置分层（TDD-05 §1.1）：
    L1 环境配置 → 环境变量（core/config.py）
    L2 部署配置 → 随镜像的配置文件
    **L3 业务配置 → 本文件的 config_items / approval_rules / scoring_rubrics**
    **L4 数据配置 → 本文件的 cost_items / exchange_rates**

    L3/L4 放数据库的原因：审批金额上限从 5000 改成 8000，
    不应该需要发版重启。这是 PRD 18 章"可配置"要求的落地。

`config_items` 的时间区间设计（ADR-007 §3.7）：
    PostgreSQL 用 `EXCLUDE USING gist` + `tstzrange` 保证"同一时刻
    同一 key 只有一个生效值"。MySQL 不支持排除约束，替代方案是
    生成列 + 唯一索引 + 触发器（见 ops/generate_constraints.py）。

字段改名说明（ADR-007 §3.11 保留字）：
    `config_items.key` → `config_key`（KEY 是 MySQL 保留字）
    `alert_rules.condition` → `condition_expr`（CONDITION 是保留字）
    `alerts.level` → `alert_level`（显式命名，与 alert_rules.level 一致）
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Computed, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.constants import (
    AlertLevel,
    AlertStatus,
    CapabilityState,
    ConfigValueType,
    IntegrityStatus,
    RuleScopeType,
    SyncStatus,
    TaskStatus,
)
from core.db import Base
from core.models.mixins import TimestampMixin
from core.models.types import (
    BigIntCol,
    BigIntColNullable,
    BigIntPK,
    BoolCol,
    IntCol,
    JsonCol,
    JsonColNullable,
    JsonArrayCol,
    MoneyColNullable,
    TimestampCol,
    TimestampColNullable,
)

__all__ = [
    "ConfigItem",
    "ApprovalRule",
    "ScoringRubric",
    "FeatureFlag",
    "PlatformCapability",
    "RawPayload",
    "TaskRun",
    "SyncCursor",
    "SyncWatermark",
    "ApiCallLog",
    "AlertRule",
    "Alert",
]


# ============================================================
# 配置
# ============================================================


class ConfigItem(Base, TimestampMixin):
    """统一标量配置（含时间区间）。

    `effective_from` / `effective_to` 支持"从下月 1 日起生效"这类需求 ——
    可以提前录入并在指定时间自动切换，不需要人守着改。

    唯一性用生成列实现（ADR-007 §3.7）：
        active_key = IF(effective_to IS NULL, CONCAT(namespace, ':', config_key), NULL)
        UNIQUE (tenant_id, active_key)
    保证每个 key 只有一个"当前生效值"。
    """

    __tablename__ = "config_items"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    #: 命名空间，如 business.approval / business.finance
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 配置键。**改名为 config_key** —— KEY 是 MySQL 保留字
    config_key: Mapped[str] = mapped_column(String(128), nullable=False)

    #: 值（统一 JSONB，读取时按 value_type 校验）
    value: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)

    value_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ConfigValueType.STRING.value,
        server_default=ConfigValueType.STRING.value,
    )

    effective_from: Mapped[datetime] = mapped_column(
        TimestampCol, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    effective_to: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    #: 生成列 —— 保证"每个 key 只有一个当前生效值"（ADR-007 §3.7）
    #:
    #: 用 CHAR(58) 而非字面量 ':' —— SQLAlchemy 的 text() 会把冒号
    #: 当成绑定参数占位符，即使它在引号内也可能被误解析。
    #:
    #: 表达式含义：effective_to 为空（当前生效）时取 "namespace:key"，
    #: 否则为 NULL。配合唯一索引实现"同一 key 只能有一个当前生效值"。
    active_key: Mapped[str | None] = mapped_column(
        String(192),
        Computed(
            "IF(effective_to IS NULL, CONCAT(namespace, CHAR(58), config_key), NULL)",
            persisted=True,
        ),
        nullable=True,
        comment="生成列：effective_to IS NULL 时为 namespace:config_key，否则 NULL",
    )

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    __table_args__ = (
        Index("uk_config_items_active", "tenant_id", "active_key", unique=True),
        Index("idx_config_items_lookup", "tenant_id", "namespace", "config_key"),
        {"comment": "统一标量配置（含时间区间）"},
    )


class ApprovalRule(Base, TimestampMixin):
    """审批触发规则。"""

    __tablename__ = "approval_rules"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: 操作类型：LISTING_PUBLISH / PRICE_UPDATE / REFUND_EXECUTE / ...
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 触发条件（满足任一即需审批）
    #: 例：{"amount_gt": 500, "discount_pct_gt": 20, "sku_count_gt": 50}
    conditions: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)

    #: 审批链
    approver_roles: Mapped[list] = mapped_column(JsonArrayCol, nullable=False, default=list)
    required_approvals: Mapped[int] = mapped_column(IntCol, nullable=False, default=1, server_default="1")

    #: 时效
    timeout_hours: Mapped[int] = mapped_column(IntCol, nullable=False, default=24, server_default="24")
    escalate_hours: Mapped[int | None] = mapped_column(IntCol, nullable=True)

    is_active: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=True, server_default="1")
    version: Mapped[int] = mapped_column(IntCol, nullable=False, default=1, server_default="1")

    effective_from: Mapped[datetime] = mapped_column(
        TimestampCol, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    effective_to: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "rule_code", "version", name="uk_approval_rules_version"),
        Index("idx_approval_rules_active", "tenant_id", "operation_type"),
        {"comment": "审批触发规则"},
    )


class ScoringRubric(Base, TimestampMixin):
    """选品评分卡（**版本化**）。

    必须版本化：否则改了权重后，历史评分无法解释
    （"为什么这个商品当时评了 75 分"）。
    """

    __tablename__ = "scoring_rubrics"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    rubric_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: 维度定义
    #: [{"key":"demand","name":"需求规模","weight":0.25,
    #:   "metrics":[{"metric":"monthly_search_volume","direction":"higher_better",
    #:               "buckets":[[1000,0],[5000,40],[20000,80],[50000,100]]}]}]
    dimensions: Mapped[list] = mapped_column(JsonArrayCol, nullable=False, default=list)

    total_weight: Mapped[Decimal] = mapped_column(
        MoneyColNullable, nullable=False, default=Decimal("1")
    )
    pass_threshold: Mapped[Decimal] = mapped_column(
        MoneyColNullable, nullable=False, default=Decimal("60")
    )
    grade_thresholds: Mapped[dict] = mapped_column(
        JsonCol, nullable=False, default=dict
    )

    version: Mapped[int] = mapped_column(IntCol, nullable=False, default=1, server_default="1")
    is_active: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=True, server_default="1")

    __table_args__ = (
        UniqueConstraint("tenant_id", "rubric_code", "version", name="uk_scoring_rubrics_version"),
        {"comment": "选品评分卡"},
    )


class FeatureFlag(Base, TimestampMixin):
    """功能开关。

    `expires_at` 的用意：开关不设过期时间会永久留在代码里，
    最终形成"开关地狱"（没人敢删，也没人知道开还是关）。
    **强制过期促使清理。**
    """

    __tablename__ = "feature_flags"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    flag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")

    #: 灰度比例（0–100）
    rollout_pct: Mapped[int] = mapped_column(IntCol, nullable=False, default=100, server_default="100")
    #: 白名单店铺
    allowed_shops: Mapped[list] = mapped_column(JsonArrayCol, nullable=False, default=list)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 强制清理（避免永久开关）
    expires_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "flag_key", name="uk_feature_flags_key"),
        Index("idx_feature_flags_expiry", "expires_at"),
        {"comment": "功能开关"},
    )


class PlatformCapability(Base, TimestampMixin):
    """平台能力矩阵落库。

    代码里的 `capabilities()` 是权威来源，本表是**运行期视图** ——
    供前端展示与任务调度决策，避免前端 import Python 代码。

    `disabled_until` 的用途：平台某接口临时故障时，
    可以只标记该能力不可用（降级为 MANUAL），而不用改代码发版。
    这是一个性价比很高的设计。
    """

    __tablename__ = "platform_capabilities"

    id: Mapped[BigIntPK]

    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)

    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CapabilityState.UNSUPPORTED.value,
        server_default=CapabilityState.UNSUPPORTED.value,
    )

    #: 运行期临时降级
    disabled_until: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 官方文档链接（可追溯）
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: 实测确认时间（未实测的能力此列为空，前端应显示"待确认"）
    verified_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    #: 声明该能力的代码版本
    code_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("platform", "capability", name="uk_platform_capabilities_key"),
        Index("idx_platform_capabilities_state", "platform", "state"),
        {"comment": "平台能力矩阵"},
    )


# ============================================================
# 原始数据与任务
# ============================================================


class RawPayload(Base):
    """原始报文归档（**不可变**，原则 5）。

    写入后不修改。规范化数据可从原始报文重建 ——
    这是"平台改了结构但我们改错了转换逻辑"时唯一的救命稻草。
    """

    __tablename__ = "raw_payloads"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    shop_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    #: 报文来源类型：ORDER / LISTING / SETTLEMENT / FEED_REPORT / ...
    payload_type: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 关联的业务实体（可选）
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 原始内容（可能是 JSON 或 CSV 文本）
    content: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)
    content_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="application/json", server_default="application/json"
    )

    #: 平台请求 ID
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 含 PII 的报文需 30 天删除（PRD 15.3）
    contains_pii: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TimestampColNullable, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )

    __table_args__ = (
        Index("idx_raw_payloads_entity", "tenant_id", "entity_type", "entity_id"),
        Index("idx_raw_payloads_expiry", "expires_at"),
        Index("idx_raw_payloads_type", "tenant_id", "platform", "payload_type", "created_at"),
        {"comment": "原始报文归档（不可变）"},
    )


class TaskRun(Base, TimestampMixin):
    """任务执行记录。"""

    __tablename__ = "task_runs"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Celery task id
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    shop_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    resource: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TaskStatus.RUNNING.value, server_default=TaskStatus.RUNNING.value
    )

    started_at: Mapped[datetime] = mapped_column(
        TimestampCol, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    finished_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(IntCol, nullable=True)

    #: 首次尝试 vs 重试（PRD 20.1 要求分开统计成功率）
    is_first_attempt: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=True, server_default="1")
    retry_count: Mapped[int] = mapped_column(IntCol, nullable=False, default=0, server_default="0")

    records_processed: Mapped[int] = mapped_column(IntCol, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    __table_args__ = (
        Index("idx_task_runs_name", "tenant_id", "task_name", "started_at"),
        Index("idx_task_runs_status", "status", "started_at"),
        Index("idx_task_runs_trace", "trace_id"),
        {"comment": "任务执行记录"},
    )


class SyncCursor(Base, TimestampMixin):
    """同步游标（增量同步水位）。

    状态机见 domain.state_machines.sync（4 态）。

    **关键实现约束**：游标只在**全部成功**时前移。
    每页都写游标会导致中途失败时丢数据。
    """

    __tablename__ = "sync_cursors"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)

    #: 资源类型：ORDERS / INVENTORY / LISTINGS / SETTLEMENT
    resource: Mapped[str] = mapped_column(String(64), nullable=False)

    cursor_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="TIMESTAMP", server_default="TIMESTAMP"
    )
    #: 游标值（时间戳 / token / page token）
    cursor_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_success_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    consecutive_failures: Mapped[int] = mapped_column(IntCol, nullable=False, default=0, server_default="0")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SyncStatus.IDLE.value, server_default=SyncStatus.IDLE.value
    )
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("shop_id", "resource", name="uk_sync_cursors_shop_resource"),
        Index("idx_sync_cursors_status", "tenant_id", "status"),
        {"comment": "同步游标"},
    )


class SyncWatermark(Base, TimestampMixin):
    """同步水位与完整性检查。

    `last_full_check_at` 对应 PRD 10.2.2 要求的"最后完整核对时间"。
    `fresh_lag_minutes` 是最重要的可观测指标 ——
    任何数据异常排查的第一步都是看它。
    """

    __tablename__ = "sync_watermarks"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)
    shop_id: Mapped[int] = mapped_column(BigIntCol, nullable=False)
    resource: Mapped[str] = mapped_column(String(64), nullable=False)

    last_full_check_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    last_incremental_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    #: 已同步数据的最晚业务时间
    data_max_time: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    #: 数据新鲜度（用于 SLO）
    fresh_lag_minutes: Mapped[int | None] = mapped_column(IntCol, nullable=True)
    record_count: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    integrity_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=IntegrityStatus.UNKNOWN.value,
        server_default=IntegrityStatus.UNKNOWN.value,
    )
    gap_detail: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)

    __table_args__ = (
        UniqueConstraint("shop_id", "resource", name="uk_sync_watermarks_shop_resource"),
        {"comment": "同步水位与完整性"},
    )


class ApiCallLog(Base):
    """API 调用日志（字段白名单，脱敏）。

    默认只存摘要，采样时才存脱敏报文（TDD-01 §4.1、PRD 15.5）。
    **绝不记录 Token 与买家明文。**

    `error_category` 是统一错误分类（adapters.errors.ErrorCategory），
    据此可以做"哪个平台的哪类错误最多"的聚合分析。
    """

    __tablename__ = "api_call_logs"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    shop_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)

    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)

    status_code: Mapped[int | None] = mapped_column(IntCol, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 统一错误分类（adapters.errors.ErrorCategory 的值）
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(IntCol, nullable=True)
    retry_count: Mapped[int] = mapped_column(IntCol, nullable=False, default=0, server_default="0")
    rate_limited: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=False, server_default="0")

    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 默认只存白名单字段的摘要
    request_summary: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)
    response_summary: Mapped[dict | None] = mapped_column(JsonColNullable, nullable=True)
    #: 采样时指向原始报文
    raw_payload_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    called_at: Mapped[datetime] = mapped_column(
        TimestampCol, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    #: 日志保留策略
    expires_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)

    __table_args__ = (
        Index("idx_api_logs_lookup", "tenant_id", "platform", "called_at"),
        Index("idx_api_logs_error", "error_category", "called_at"),
        Index("idx_api_logs_expiry", "expires_at"),
        {"comment": "API 调用日志（脱敏）"},
    )


# ============================================================
# 预警
# ============================================================


class AlertRule(Base, TimestampMixin):
    """预警规则。"""

    __tablename__ = "alert_rules"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False)

    scope_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RuleScopeType.GLOBAL.value, server_default=RuleScopeType.GLOBAL.value
    )
    scope_value: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 触发条件。**改名为 condition_expr** —— CONDITION 是 MySQL 保留字
    condition_expr: Mapped[dict] = mapped_column(JsonCol, nullable=False, default=dict)

    level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AlertLevel.P2.value, server_default=AlertLevel.P2.value
    )

    #: 通知渠道（PRD 12.4：分级推送，禁止默认 @所有人）
    notify_channels: Mapped[list] = mapped_column(JsonArrayCol, nullable=False, default=list)
    notify_roles: Mapped[list] = mapped_column(JsonArrayCol, nullable=False, default=list)

    #: 超时升级
    escalate_after_minutes: Mapped[int | None] = mapped_column(IntCol, nullable=True)
    #: 防刷屏
    cooldown_minutes: Mapped[int] = mapped_column(IntCol, nullable=False, default=60, server_default="60")
    #: 金额门槛，防小额噪音
    min_amount: Mapped[Decimal | None] = mapped_column(MoneyColNullable, nullable=True)

    is_active: Mapped[bool] = mapped_column(BoolCol, nullable=False, default=True, server_default="1")
    version: Mapped[int] = mapped_column(IntCol, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("tenant_id", "rule_code", "version", name="uk_alert_rules_version"),
        Index("idx_alert_rules_active", "tenant_id", "alert_type"),
        {"comment": "预警规则"},
    )


class Alert(Base, TimestampMixin):
    """预警实例。

    `suggestion_actions` 存**结构化建议动作**，前端可直接渲染成按钮，
    机器人可直接带操作链接。这解决了 PRD 12.4
    "预警必须带建议动作"的落地问题 ——
    只报警不给建议的预警，等于把问题原样丢回给人。
    """

    __tablename__ = "alerts"

    id: Mapped[BigIntPK]
    tenant_id: Mapped[int] = mapped_column(BigIntCol, nullable=False, index=True)

    alert_type: Mapped[str] = mapped_column(String(64), nullable=False)

    #: **改名为 alert_level**（显式命名，与 alert_rules.level 区分）
    alert_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AlertLevel.P2.value, server_default=AlertLevel.P2.value
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 建议动作（PRD 12.4 强制要求，不能只报警）
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: [{"label":"跟价到$25.49","action":"price_adjust",...}]
    suggestion_actions: Mapped[list | None] = mapped_column(JsonColNullable, nullable=True)

    related_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shop_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    rule_id: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)

    #: 去重键，防重复告警
    dedup_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AlertStatus.OPEN.value, server_default=AlertStatus.OPEN.value
    )

    notified_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    notified_channels: Mapped[list | None] = mapped_column(JsonColNullable, nullable=True)

    acknowledged_by: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(BigIntColNullable, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TimestampColNullable, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_alerts_open", "tenant_id", "status", "alert_level", "created_at"),
        Index("idx_alerts_dedup", "dedup_key", "created_at"),
        Index("idx_alerts_type", "tenant_id", "alert_type", "created_at"),
        {"comment": "预警实例"},
    )
