"""全系统枚举常量。

命名规范（TDD-01 附录 A）：
    数据库存 UPPER_SNAKE 字符串，Python 侧用 StrEnum。

设计约束：
    1. 枚举值是**唯一真相**。DDL 的约束、状态机、API 契约全部从这里派生，
       禁止在别处硬编码字符串字面量。
    2. 每个业务状态枚举提供 `label`（中文）与 `terminal`（是否终态），
       供前端渲染与状态机校验复用。
    3. 新增枚举值必须同步三处：本文件 + `_LABELS` 标签字典
       + 状态机转换表（domain/state_machines/）。
       DDL 约束由 ops/generate_constraints.py 自动生成，无需手工维护。

关于标签为什么用集中式字典而不是类属性：
    Python 的 Enum 元类对类体内的数据属性极不宽容 ——
      - `_LABELS = {...}`（单前缀）→ 被当成枚举成员，抛
        `TypeError: {} is not a string`
      - `_LABELS_ = {...}`（sunder）→ 被保留，抛
        `ValueError: _sunder_ names ... are reserved for future Enum use`
    只有 `_ClassName__name`（name-mangled private）能幸免，但写法丑陋。
    因此标签统一放在模块级 `_LABELS`，由 `LabeledEnum.label` 按类名查找。
"""

from enum import StrEnum

__all__ = [
    "LabeledEnum",
    # 租户与权限
    "TenantStatus",
    "UserStatus",
    # 店铺与凭据
    "ShopStatus",
    "CredentialStatus",
    "CredentialHealth",
    # 商品与刊登
    "ProductStatus",
    "ListingStatus",
    # 订单
    "OrderStatus",
    # 售后
    "RefundStatus",
    "RefundType",
    "RiskLevel",
    "RestockStatus",
    # 审批
    "ApprovalStatus",
    "ApprovalType",
    "ApprovalRiskLevel",
    "SuggestionSource",
    "ApprovalAction",
    "RollbackStatus",
    "ActorType",
    # 同步与任务
    "SyncStatus",
    "IntegrityStatus",
    "TaskStatus",
    # 预警
    "AlertStatus",
    "AlertLevel",
    "RuleScopeType",
    # 财务
    "PeriodStatus",
    # 平台
    "Platform",
    "Region",
    "CapabilityState",
    # 基础设施
    "EventStatus",
    "ConfigValueType",
    # 辅助
    "ENUM_COLUMNS",
    "enum_of",
    "label_of",
]


# ============================================================
# 基础 Mixin
# ============================================================


class LabeledEnum(StrEnum):
    """带中文标签的枚举基类。

    标签来自模块级 `_LABELS` 字典（见文件末尾）。
    未登记的枚举值会回退为枚举名本身 —— 这样新增值不会因忘记加标签而报错，
    但应尽快补上（有测试会检查覆盖率）。
    """

    @property
    def label(self) -> str:
        """中文标签，用于前端展示与报表。"""
        return _LABELS.get(type(self).__name__, {}).get(self.value, self.value)

    @classmethod
    def db_values(cls) -> list[str]:
        """全部合法数据库值。DDL 约束生成器依赖此方法。"""
        return [m.value for m in cls]

    @classmethod
    def is_valid(cls, value: str) -> bool:
        """校验一个字符串是否为合法枚举值。"""
        return value in cls._value2member_map_


# ============================================================
# 租户与权限
# ============================================================


class TenantStatus(LabeledEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"


class UserStatus(LabeledEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    LOCKED = "LOCKED"


# ============================================================
# 店铺与凭据
# ============================================================


class ShopStatus(LabeledEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"


class CredentialStatus(LabeledEnum):
    """凭据的授权状态（平台侧授权是否有效）。"""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    REVOKED = "REVOKED"
    ERROR = "ERROR"


class CredentialHealth(LabeledEnum):
    """凭据的健康度（Token 是否临近过期）。"""

    VALID = "VALID"
    EXPIRING = "EXPIRING"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"

    @property
    def needs_action(self) -> bool:
        """是否需要人工介入（重新授权）。"""
        return self in (CredentialHealth.EXPIRED, CredentialHealth.REVOKED)


# ============================================================
# 商品与刊登
# ============================================================


class ProductStatus(LabeledEnum):
    """内部商品主表状态（我方视角，非平台状态）。"""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ListingStatus(LabeledEnum):
    """上架状态机（TDD-04 §2）。

    12 个状态，与 TDD-02 的 product_listings.listing_status 严格一致。
    任何改动必须同步 domain/state_machines/listing.py 的转换表。
    """

    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    REJECTED = "REJECTED"
    QUEUED = "QUEUED"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    ACTIVE = "ACTIVE"
    PARTIAL_ACTIVE = "PARTIAL_ACTIVE"
    INACTIVE = "INACTIVE"
    FAILED = "FAILED"
    DELETED = "DELETED"

    @property
    def terminal(self) -> bool:
        """是否**严格终态**（无任何出边）。

        只有 DELETED 满足。

        修正说明：TDD-04 §2.1 的表格把 ACTIVE / INACTIVE / DELETED
        都标为"终态"，但 §2.2 的转换表里 ACTIVE 有 `delist` 出边、
        INACTIVE 有 `relist` / `delete` 出边 —— 按状态机理论它们不是终态。
        本实现按理论修正：`terminal` 用于"终态封闭性测试"，
        业务上的"可长期停留"用 `stable`。
        """
        return self is ListingStatus.DELETED

    @property
    def stable(self) -> bool:
        """业务稳定态：可长期停留、无需系统主动推进。

        与 terminal 的区别：stable 允许**人工触发**的出边
        （下架、重新上架、删除），而 terminal 完全无出边。
        """
        return self in (
            ListingStatus.ACTIVE,
            ListingStatus.INACTIVE,
            ListingStatus.DELETED,
        )

    @property
    def sellable(self) -> bool:
        """商品是否在售（含部分上线 —— 部分上线时确实在产生订单）。"""
        return self in (ListingStatus.ACTIVE, ListingStatus.PARTIAL_ACTIVE)

    @property
    def awaiting_human(self) -> bool:
        """是否在等待人工介入。用于"待办"看板统计。"""
        return self in (
            ListingStatus.PENDING_APPROVAL,
            ListingStatus.REJECTED,
            ListingStatus.PARTIAL_ACTIVE,
            ListingStatus.FAILED,
        )


# ============================================================
# 订单
# ============================================================


class OrderStatus(LabeledEnum):
    """订单状态（平台驱动，本系统只做镜像 —— TDD-04 §5）。

    本系统不主动流转订单状态，同步任务负责更新。
    状态机仅用于校验同步结果是否合理。
    """

    PENDING = "PENDING"
    UNSHIPPED = "UNSHIPPED"
    PARTIALLY_SHIPPED = "PARTIALLY_SHIPPED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELED = "CANCELED"
    RETURNED = "RETURNED"

    @property
    def counts_as_sale(self) -> bool:
        """是否计入销售额。

        CANCELED 不计入；RETURNED 计入销售额但同时计入退款（净额为零），
        这样退货率分析才有分母。
        """
        return self is not OrderStatus.CANCELED


# ============================================================
# 售后
# ============================================================


class RefundStatus(LabeledEnum):
    """退款状态机（TDD-04 §6）。

    混合驱动：平台可写、本系统在支持时也可写。
    Amazon 不支持卖家侧执行退款，状态全部由平台同步驱动。
    """

    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    AUTO_APPROVED = "AUTO_APPROVED"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"
    CLOSED = "CLOSED"

    @property
    def terminal(self) -> bool:
        return self in (RefundStatus.REJECTED, RefundStatus.CLOSED)

    @property
    def money_leaving(self) -> bool:
        """是否已产生实际资金流出（用于财务核算）。"""
        return self is RefundStatus.REFUNDED


class RefundType(LabeledEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    GOODWILL = "GOODWILL"
    CHARGEBACK = "CHARGEBACK"

    @property
    def is_dispute(self) -> bool:
        """是否属于争议类（拒付会伴随罚款，需单独观察 —— TDD-05 §2.2 第 4 条）。"""
        return self is RefundType.CHARGEBACK


class RiskLevel(LabeledEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RestockStatus(LabeledEnum):
    """退货后的库存处置状态。"""

    SELLABLE = "SELLABLE"
    UNSELLABLE = "UNSELLABLE"
    PENDING = "PENDING"


# ============================================================
# 审批
# ============================================================


class ApprovalType(LabeledEnum):
    """审批单类型。对应 TDD-02 approvals.approval_type。"""

    LISTING_PUBLISH = "LISTING_PUBLISH"
    LISTING_DELIST = "LISTING_DELIST"
    PRICE_UPDATE = "PRICE_UPDATE"
    AD_BUDGET_CHANGE = "AD_BUDGET_CHANGE"
    REFUND_EXECUTE = "REFUND_EXECUTE"
    CREDENTIAL_ACCESS = "CREDENTIAL_ACCESS"
    DATA_EXPORT = "DATA_EXPORT"
    BULK_OPERATION = "BULK_OPERATION"
    KILL_SWITCH_RESUME = "KILL_SWITCH_RESUME"


class ApprovalStatus(LabeledEnum):
    """审批状态机（TDD-04 §3）。

    关键设计：APPROVED 与 EXECUTED 必须分离 ——
    "人已同意" 不等于 "事已办成"。
    合并会丢失"审批通过但执行失败"的追踪。
    """

    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        """终态。FAILED 不是终态 —— 允许重试执行。"""
        return self in (
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.CANCELED,
            ApprovalStatus.EXECUTED,
        )

    @property
    def actionable(self) -> bool:
        """是否仍可被操作（审批人可见）。"""
        return self is ApprovalStatus.PENDING


class ApprovalRiskLevel(LabeledEnum):
    """审批风险等级。决定审批超时时间与审批链长度。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def timeout_hours(self) -> int:
        """超时小时数（TDD-04 §3.5）。"""
        return {
            ApprovalRiskLevel.LOW: 24,
            ApprovalRiskLevel.MEDIUM: 8,
            ApprovalRiskLevel.HIGH: 4,
            ApprovalRiskLevel.CRITICAL: 1,
        }[self]


class SuggestionSource(LabeledEnum):
    """审批建议的来源。审计需要区分"人提的"还是"AI 建议的"。"""

    HUMAN = "HUMAN"
    AI = "AI"
    RULE = "RULE"
    ALERT = "ALERT"


class ApprovalAction(LabeledEnum):
    """审批动作流水（append-only）。"""

    SUBMIT = "SUBMIT"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    CANCEL = "CANCEL"
    EXPIRE = "EXPIRE"
    EXECUTE = "EXECUTE"
    EXECUTE_FAIL = "EXECUTE_FAIL"
    ROLLBACK = "ROLLBACK"
    COMMENT = "COMMENT"


class RollbackStatus(LabeledEnum):
    NONE = "NONE"
    PARTIAL = "PARTIAL"
    DONE = "DONE"
    FAILED = "FAILED"


class ActorType(LabeledEnum):
    """操作者类型。审计必须能区分人与系统。"""

    USER = "USER"
    SYSTEM = "SYSTEM"
    PLATFORM = "PLATFORM"


# ============================================================
# 同步与任务
# ============================================================


class SyncStatus(LabeledEnum):
    """同步游标状态机（TDD-04 §4）。

    连续失败达阈值会自动转 PAUSED —— 因为 Token 失效这类问题
    重试 100 次也没用，只会刷爆日志和告警。
    """

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    ERROR = "ERROR"
    PAUSED = "PAUSED"

    @property
    def blocks_schedule(self) -> bool:
        """是否应阻止下一次调度。"""
        return self is SyncStatus.PAUSED


class IntegrityStatus(LabeledEnum):
    """数据完整性（水位检查结果）。"""

    OK = "OK"
    GAPS_DETECTED = "GAPS_DETECTED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"

    @property
    def trustworthy(self) -> bool:
        """数据是否可被下游信任。STALE 意味着报表要打降级标记。"""
        return self is IntegrityStatus.OK


class TaskStatus(LabeledEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELED = "CANCELED"

    @property
    def terminal(self) -> bool:
        return self is not TaskStatus.RUNNING


# ============================================================
# 预警
# ============================================================


class AlertLevel(LabeledEnum):
    """预警级别。

    推送策略（TDD-05 §4.4）：P0 不静默、P1 夜间静默、P2 只进日报。
    如果 P2 半夜推送，团队会关闭通知，最终 P0 也收不到。
    """

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"

    @property
    def cooldown_minutes(self) -> int:
        """同一告警的去重冷却期。"""
        return {AlertLevel.P0: 15, AlertLevel.P1: 120, AlertLevel.P2: 720}[self]

    @property
    def allow_quiet_hours(self) -> bool:
        """是否允许在免打扰时段静默。P0 永不被静默。"""
        return self is not AlertLevel.P0

    @property
    def aggregate_only(self) -> bool:
        """是否只进汇总日报（不逐条推送）。"""
        return self is AlertLevel.P2


class AlertStatus(LabeledEnum):
    OPEN = "OPEN"
    NOTIFIED = "NOTIFIED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    IGNORED = "IGNORED"
    SUPPRESSED = "SUPPRESSED"

    @property
    def terminal(self) -> bool:
        return self in (
            AlertStatus.RESOLVED,
            AlertStatus.IGNORED,
            AlertStatus.SUPPRESSED,
        )


class RuleScopeType(LabeledEnum):
    """规则作用范围。"""

    GLOBAL = "GLOBAL"
    SHOP = "SHOP"
    CATEGORY = "CATEGORY"
    SKU = "SKU"


# ============================================================
# 财务
# ============================================================


class PeriodStatus(LabeledEnum):
    """会计期间状态（TDD-05 §2.4）。

    CLOSED 期间禁止任何写入 —— 变更必须先 reopen（需 admin 权限 + 审计）。
    """

    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    REOPENED = "REOPENED"

    @property
    def accepts_write(self) -> bool:
        """是否接受数据写入。"""
        return self is not PeriodStatus.CLOSED

    @property
    def allows_recalc(self) -> bool:
        """是否允许重算。CLOSING 期间只允许录入，不允许重算。"""
        return self in (PeriodStatus.OPEN, PeriodStatus.REOPENED)


# ============================================================
# 平台
# ============================================================


class Platform(LabeledEnum):
    """支持的平台。值用于数据库存储与适配器注册键。"""

    AMAZON = "AMAZON"
    TIKTOK = "TIKTOK"
    TEMU = "TEMU"
    SHEIN = "SHEIN"
    SHOPEE = "SHOPEE"
    LAZADA = "LAZADA"
    ALIEXPRESS = "ALIEXPRESS"
    SHOPIFY = "SHOPIFY"
    MOCK = "MOCK"

    @property
    def is_real(self) -> bool:
        """是否为真实平台（MOCK 不是）。"""
        return self is not Platform.MOCK


class Region(LabeledEnum):
    """区域。Amazon 的 Base URL 按区域分三套，必须路由正确。"""

    NA = "NA"
    EU = "EU"
    FE = "FE"
    OTHER = "OTHER"


class CapabilityState(LabeledEnum):
    """平台能力状态（TDD-03 §2.2）。

    六态而非二元 —— "支持/不支持"会掩盖大量灰色地带。
    """

    NATIVE = "NATIVE"
    ASYNC = "ASYNC"
    WEBHOOK = "WEBHOOK"
    FILE_IMPORT = "FILE_IMPORT"
    MANUAL = "MANUAL"
    UNSUPPORTED = "UNSUPPORTED"

    @property
    def callable(self) -> bool:
        """是否可直接调用 API（不需人工或文件中转）。"""
        return self in (
            CapabilityState.NATIVE,
            CapabilityState.ASYNC,
            CapabilityState.WEBHOOK,
        )

    @property
    def needs_polling(self) -> bool:
        """是否需要轮询结果（异步提交）。"""
        return self is CapabilityState.ASYNC

    @property
    def needs_human(self) -> bool:
        """是否需要人工介入（应生成待办）。"""
        return self in (CapabilityState.MANUAL, CapabilityState.FILE_IMPORT)


# ============================================================
# 基础设施
# ============================================================


class EventStatus(LabeledEnum):
    """领域事件（Outbox）消费状态（TDD-04 §3.4）。"""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class ConfigValueType(LabeledEnum):
    """配置项的值类型。用于读取时做 schema 校验。"""

    INT = "INT"
    DECIMAL = "DECIMAL"
    BOOL = "BOOL"
    STRING = "STRING"
    DURATION = "DURATION"
    JSON = "JSON"


# ============================================================
# 中文标签字典（集中维护，见模块文档字符串的说明）
# ============================================================

_LABELS: dict[str, dict[str, str]] = {
    "TenantStatus": {
        "ACTIVE": "正常",
        "SUSPENDED": "已暂停",
        "DELETED": "已删除",
    },
    "UserStatus": {
        "ACTIVE": "正常",
        "DISABLED": "已停用",
        "LOCKED": "已锁定",
    },
    "ShopStatus": {
        "ACTIVE": "正常",
        "SUSPENDED": "已暂停",
        "DELETED": "已删除",
    },
    "CredentialStatus": {
        "ACTIVE": "已授权",
        "INACTIVE": "未启用",
        "REVOKED": "已撤销",
        "ERROR": "异常",
    },
    "CredentialHealth": {
        "VALID": "有效",
        "EXPIRING": "即将过期",
        "EXPIRED": "已过期",
        "REVOKED": "已撤销",
    },
    "ProductStatus": {
        "DRAFT": "草稿",
        "ACTIVE": "在售",
        "ARCHIVED": "已归档",
    },
    "ListingStatus": {
        "DRAFT": "草稿",
        "VALIDATING": "校验中",
        "PENDING_APPROVAL": "待审批",
        "REJECTED": "已驳回",
        "QUEUED": "已入队",
        "SUBMITTED": "已提交",
        "PROCESSING": "处理中",
        "ACTIVE": "已上线",
        "PARTIAL_ACTIVE": "部分上线",
        "INACTIVE": "已下架",
        "FAILED": "失败",
        "DELETED": "已删除",
    },
    "OrderStatus": {
        "PENDING": "待处理",
        "UNSHIPPED": "未发货",
        "PARTIALLY_SHIPPED": "部分发货",
        "SHIPPED": "已发货",
        "DELIVERED": "已签收",
        "CANCELED": "已取消",
        "RETURNED": "已退回",
    },
    "RefundStatus": {
        "REQUESTED": "买家已申请",
        "APPROVED": "已批准",
        "AUTO_APPROVED": "规则自动批准",
        "REJECTED": "已拒绝",
        "REFUNDED": "已退款",
        "CLOSED": "已关闭",
    },
    "RefundType": {
        "FULL": "全额退款",
        "PARTIAL": "部分退款",
        "GOODWILL": "善意补偿",
        "CHARGEBACK": "拒付",
    },
    "RiskLevel": {"LOW": "低", "MEDIUM": "中", "HIGH": "高"},
    "RestockStatus": {
        "SELLABLE": "可再售",
        "UNSELLABLE": "不可售",
        "PENDING": "待判定",
    },
    "ApprovalType": {
        "LISTING_PUBLISH": "商品上架",
        "LISTING_DELIST": "商品下架",
        "PRICE_UPDATE": "价格修改",
        "AD_BUDGET_CHANGE": "广告预算调整",
        "REFUND_EXECUTE": "退款执行",
        "CREDENTIAL_ACCESS": "凭据访问",
        "DATA_EXPORT": "数据导出",
        "BULK_OPERATION": "批量操作",
        "KILL_SWITCH_RESUME": "恢复自动化",
    },
    "ApprovalStatus": {
        "DRAFT": "草稿",
        "PENDING": "待审批",
        "APPROVED": "已通过",
        "REJECTED": "已驳回",
        "EXPIRED": "已超时",
        "CANCELED": "已撤销",
        "EXECUTED": "已执行",
        "FAILED": "执行失败",
    },
    "ApprovalRiskLevel": {
        "LOW": "低",
        "MEDIUM": "中",
        "HIGH": "高",
        "CRITICAL": "紧急",
    },
    "SuggestionSource": {
        "HUMAN": "人工",
        "AI": "AI 建议",
        "RULE": "规则触发",
        "ALERT": "预警联动",
    },
    "ApprovalAction": {
        "SUBMIT": "提交",
        "APPROVE": "通过",
        "REJECT": "驳回",
        "CANCEL": "撤销",
        "EXPIRE": "超时",
        "EXECUTE": "执行成功",
        "EXECUTE_FAIL": "执行失败",
        "ROLLBACK": "回滚",
        "COMMENT": "评论",
    },
    "RollbackStatus": {
        "NONE": "无需回滚",
        "PARTIAL": "部分回滚",
        "DONE": "已回滚",
        "FAILED": "回滚失败",
    },
    "ActorType": {"USER": "用户", "SYSTEM": "系统", "PLATFORM": "平台"},
    "SyncStatus": {
        "IDLE": "空闲",
        "RUNNING": "同步中",
        "ERROR": "失败",
        "PAUSED": "已暂停",
    },
    "IntegrityStatus": {
        "OK": "完整",
        "GAPS_DETECTED": "存在缺口",
        "STALE": "数据过期",
        "UNKNOWN": "未核对",
    },
    "TaskStatus": {
        "RUNNING": "执行中",
        "SUCCESS": "成功",
        "FAILED": "失败",
        "TIMEOUT": "超时",
        "CANCELED": "已取消",
    },
    "AlertLevel": {"P0": "紧急", "P1": "重要", "P2": "提示"},
    "AlertStatus": {
        "OPEN": "待处理",
        "NOTIFIED": "已通知",
        "ACKNOWLEDGED": "已确认",
        "RESOLVED": "已解决",
        "IGNORED": "已忽略",
        "SUPPRESSED": "已抑制",
    },
    "RuleScopeType": {
        "GLOBAL": "全局",
        "SHOP": "店铺",
        "CATEGORY": "类目",
        "SKU": "单品",
    },
    "PeriodStatus": {
        "OPEN": "开放",
        "CLOSING": "结账中",
        "CLOSED": "已关账",
        "REOPENED": "已重开",
    },
    "Platform": {
        "AMAZON": "亚马逊",
        "TIKTOK": "TikTok Shop",
        "TEMU": "Temu",
        "SHEIN": "SHEIN",
        "SHOPEE": "Shopee",
        "LAZADA": "Lazada",
        "ALIEXPRESS": "速卖通",
        "SHOPIFY": "Shopify",
        "MOCK": "Mock（测试）",
    },
    "Region": {"NA": "北美", "EU": "欧洲", "FE": "远东", "OTHER": "其他"},
    "CapabilityState": {
        "NATIVE": "原生支持",
        "ASYNC": "异步支持",
        "WEBHOOK": "推送支持",
        "FILE_IMPORT": "文件导入",
        "MANUAL": "人工处理",
        "UNSUPPORTED": "不支持",
    },
    "EventStatus": {
        "PENDING": "待消费",
        "PROCESSING": "消费中",
        "DONE": "已完成",
        "FAILED": "失败",
    },
    "ConfigValueType": {
        "INT": "整数",
        "DECIMAL": "小数",
        "BOOL": "布尔",
        "STRING": "字符串",
        "DURATION": "时长",
        "JSON": "JSON",
    },
}


def label_of(enum_cls: type[LabeledEnum], value: str) -> str:
    """按枚举类与值取中文标签（供报表与 API 层使用）。"""
    return _LABELS.get(enum_cls.__name__, {}).get(value, value)


# ============================================================
# 枚举字段注册表
# ============================================================

#: 所有需要 DDL 约束的枚举字段映射。
#: 键为 "表名.字段名"，值为枚举类。
#: ops/generate_constraints.py 读取此映射生成 CHECK 约束或触发器。
ENUM_COLUMNS: dict[str, type[LabeledEnum]] = {
    "tenants.status": TenantStatus,
    "users.status": UserStatus,
    "shops.status": ShopStatus,
    "shop_credentials.status": CredentialStatus,
    "products.status": ProductStatus,
    "product_listings.listing_status": ListingStatus,
    "orders.order_status": OrderStatus,
    "refunds.status": RefundStatus,
    "refunds.refund_type": RefundType,
    "refunds.risk_level": RiskLevel,
    "refunds.restock_status": RestockStatus,
    "approvals.status": ApprovalStatus,
    "approvals.approval_type": ApprovalType,
    "approvals.risk_level": ApprovalRiskLevel,
    "approvals.suggestion_source": SuggestionSource,
    "approvals.rollback_status": RollbackStatus,
    "approval_actions.action": ApprovalAction,
    "approval_actions.actor_type": ActorType,
    "sync_cursors.status": SyncStatus,
    "sync_watermarks.integrity_status": IntegrityStatus,
    "task_runs.status": TaskStatus,
    "alerts.status": AlertStatus,
    "alerts.alert_level": AlertLevel,
    "alert_rules.scope_type": RuleScopeType,
    "accounting_periods.status": PeriodStatus,
    "state_transitions.actor_type": ActorType,
    "domain_events.status": EventStatus,
    "config_items.value_type": ConfigValueType,
    "platform_capabilities.state": CapabilityState,
}


def enum_of(table: str, column: str) -> type[LabeledEnum] | None:
    """查表字段对应的枚举类。约束生成器与校验器共用。"""
    return ENUM_COLUMNS.get(f"{table}.{column}")
