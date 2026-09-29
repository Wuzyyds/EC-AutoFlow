"""PII 脱敏与日志字段白名单。

两条核心规则（TDD-06 §2.2）：

1. **日志只允许白名单字段**
   不用黑名单（"禁止这些字段"），因为新增字段容易漏。
   白名单是安全默认：没登记 = 不输出。

2. **脱敏必须在写入日志前完成**
   不是"记录后再清洗"，而是"清洗后再记录"。
   记录后清洗意味着明文已经落盘，可能已被采集到日志系统。

PII 范围（PRD v1.1 §15.2 数据分级 L3/L4）：
    买家姓名、邮箱、电话、收货地址、支付信息、消息正文、平台 Token
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "REDACTED",
    "mask_email",
    "mask_phone",
    "mask_name",
    "mask_address",
    "mask_card",
    "mask_token",
    "mask_value",
    "scrub_mapping",
    "pseudonymize",
    "LOG_WHITELIST",
    "filter_log_fields",
    "PII_FIELD_PATTERNS",
    "is_pii_field",
]

REDACTED: Final[str] = "***REDACTED***"

#: PII 字段名模式（小写匹配，支持子串）。用于兜底识别。
PII_FIELD_PATTERNS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "buyer_name",
    "buyer_email",
    "buyer_phone",
    "buyer_address",
    "buyer_encrypted",
    "recipient",
    "receiver_name",
    "receiver_phone",
    "receiver_address",
    "shipping_address",
    "phone",
    "mobile",
    "email",
    "id_card",
    "card_number",
    "cvv",
    "message_content",
    "content",
    "session_id",
    "authorization",
    "cookie",
)


# ============================================================
# 基础脱敏函数
# ============================================================


def mask_email(value: str) -> str:
    """邮箱脱敏：ab***@gmail.com"""
    if not value or "@" not in value:
        return REDACTED
    local, _, domain = value.partition("@")
    if len(local) <= 2:  # noqa: PLR2004
        head = local[:1]
    else:
        head = local[:2]
    return f"{head}***@{domain}"


def mask_phone(value: str) -> str:
    """手机号脱敏：138****12

    保留前 3 位与后 2 位 —— 这是客服核对身份所需的最少信息。
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) < 7:  # noqa: PLR2004
        return REDACTED
    return f"{digits[:3]}****{digits[-2:]}"


def mask_name(value: str) -> str:
    """姓名脱敏：张**"""
    if not value:
        return REDACTED
    return value[0] + "*" * max(len(value) - 1, 1)


def mask_address(value: str) -> str:
    """地址脱敏：保留前 6 个字符。

    地址对排障（如"这批货都发到哪个州"）有价值，因此保留前缀而非全抹。
    """
    if not value:
        return REDACTED
    if len(value) <= 6:  # noqa: PLR2004
        return value[0] + "***"
    return value[:6] + "***"


def mask_card(value: str) -> str:
    """银行卡脱敏：6222 **** **** 1234"""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 10:  # noqa: PLR2004
        return REDACTED
    return f"{digits[:4]}****{digits[-4:]}"


def mask_token(value: str) -> str:
    """凭据类一律全抹，不保留任何字符。

    与邮箱/手机不同，Token 的任何一个字符都不该出现在日志里。
    """
    return REDACTED


# ============================================================
# 自动识别脱敏
# ============================================================

_EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE: Final[re.Pattern[str]] = re.compile(r"^\+?\d[\d\s\-()]{6,}$")
_TOKENISH_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\-.]{40,}$")


def mask_value(value: Any, *, field_name: str | None = None) -> Any:
    """按内容与字段名智能脱敏。

    优先按字段名判断（更可靠），其次按内容特征。
    """
    if value is None:
        return None

    # 1. 按字段名判断
    if field_name:
        lowered = field_name.lower()
        if any(p in lowered for p in ("token", "secret", "password", "credential", "key")):
            return mask_token(str(value))
        if "email" in lowered:
            return mask_email(str(value))
        if any(p in lowered for p in ("phone", "mobile")):
            return mask_phone(str(value))
        if "name" in lowered and "file" not in lowered and "table" not in lowered:
            return mask_name(str(value))
        if "address" in lowered:
            return mask_address(str(value))

    if not isinstance(value, str):
        return value

    # 2. 按内容特征
    if _EMAIL_RE.match(value):
        return mask_email(value)
    if _PHONE_RE.match(value) and len(re.sub(r"\D", "", value)) >= 7:  # noqa: PLR2004
        return mask_phone(value)
    if _TOKENISH_RE.match(value):
        return mask_token(value)

    return value


def scrub_mapping(
    data: Mapping[str, Any],
    *,
    depth: int = 0,
    max_depth: int = 6,
) -> dict[str, Any]:
    """递归清理映射中的敏感值。

    用于日志与 API 错误响应。**必须**在输出前调用。

    Args:
        depth: 当前递归深度（内部使用）。
        max_depth: 最大递归深度，防止环形结构与超深嵌套。
    """
    if depth > max_depth:
        return {"_truncated": "达到最大嵌套深度"}

    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, Mapping):
            result[key] = scrub_mapping(value, depth=depth + 1, max_depth=max_depth)
        elif isinstance(value, str):
            result[key] = mask_value(value, field_name=key)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            result[key] = [
                scrub_mapping(item, depth=depth + 1, max_depth=max_depth)
                if isinstance(item, Mapping)
                else mask_value(item, field_name=key)
                for item in value
            ]
        elif isinstance(value, bytes):
            result[key] = f"<bytes:{len(value)}>"
        else:
            result[key] = value
    return result


def pseudonymize(value: str, *, salt: str = "") -> str:
    """不可逆哈希（用于 buyer_hash 这类"能关联但不还原"的场景）。

    用途（TDD-02 §8）：
        orders 表不存买家明文，存 buyer_hash。
        这样能统计"同一买家的复购率"，但无法还原是谁。

    注意：
        不加盐时，相同的输入产生相同输出 —— 攻击者可用彩虹表反查常见值。
        生产环境应通过 salt 参数传入租户级盐值。
    """
    payload = f"{salt}:{value}".encode()
    return hashlib.sha256(payload).hexdigest()


def is_pii_field(field_name: str) -> bool:
    """按字段名判断是否可能含 PII。用于兜底拦截。"""
    lowered = field_name.lower()
    return any(p in lowered for p in PII_FIELD_PATTERNS)


# ============================================================
# 日志字段白名单
# ============================================================

#: 各类日志事件允许输出的字段。
#:
#: 规则：**没登记 = 不输出**。
#: 新增字段时必须显式加进来 —— 这个"麻烦"是刻意的，
#: 它强制开发者在写日志时想一下"这个字段能进日志吗"。
LOG_WHITELIST: Final[dict[str, frozenset[str]]] = {
    # 通用字段：所有事件都允许
    "_common": frozenset(
        {
            "timestamp",
            "level",
            "event",
            "trace_id",
            "tenant_id",
            "message",
            "duration_ms",
            "error_code",
            "error_category",
            "retryable",
            "attempt",
        }
    ),
    # 订单相关
    "order": frozenset(
        {
            "order_id",
            "shop_id",
            "platform_order_id",
            "order_status",
            "grand_total",
            "currency",
            "order_time",
            "buyer_region",  # 注意：无 buyer_encrypted、无 buyer_name
            "item_count",
        }
    ),
    # Listing 相关
    "listing": frozenset(
        {
            "listing_id",
            "product_id",
            "shop_id",
            "platform_sku",
            "platform_item_id",
            "listing_status",
            "price",
            "currency",
            "submission_id",
            "category_id",
        }
    ),
    # 平台 API 调用
    "api_call": frozenset(
        {
            "platform",
            "shop_id",  # 排查必需：哪个店铺的调用出问题
            "endpoint",
            "method",
            "status_code",
            "duration_ms",
            "retry_count",
            "rate_limited",
            "request_id",
            "error_code",
            "error_category",
        }
    ),
    # 同步任务
    "sync": frozenset(
        {
            "shop_id",
            "resource",
            "records_synced",
            "cursor_value",
            "consecutive_failures",
            "sync_status",
            "lag_minutes",
        }
    ),
    # 审批
    "approval": frozenset(
        {
            "approval_no",
            "approval_type",
            "approval_status",
            "risk_level",
            "requested_by",
            "approved_by",
            "amount",
            "quantity",
        }
    ),
    # 财务
    "finance": frozenset(
        {
            "shop_id",
            "period",
            "sku",
            "net_sales",
            "cogs",
            "ad_spend",
            "contribution_margin",
            "currency",
            "rate_used",
        }
    ),
    # 加密与凭据（**不含任何明文**）
    "security": frozenset(
        {
            "shop_id",
            "credential_id",
            "key_version",
            "operation",
            "actor_id",
            "result",
        }
    ),
    # 状态机转换
    "state_transition": frozenset(
        {
            "machine",
            "entity_type",
            "entity_id",
            "from_state",
            "to_state",
            "event",
            "actor_type",
            "actor_id",
            "reason",
        }
    ),
}


def filter_log_fields(
    payload: Mapping[str, Any],
    *,
    category: str = "api_call",
    strict: bool = True,
) -> dict[str, Any]:
    """按白名单过滤日志字段。

    Args:
        payload: 原始日志字段。
        category: 日志类别（对应 LOG_WHITELIST 的键）。
        strict: True 时丢弃未登记字段；False 时保留但脱敏。
            生产环境必须用 True。False 仅用于本地调试。

    Returns:
        过滤后的字段字典，值已脱敏。
    """
    allowed = LOG_WHITELIST.get(category, frozenset()) | LOG_WHITELIST["_common"]

    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in allowed:
            result[key] = mask_value(value, field_name=key)
        elif not strict:
            result[key] = mask_value(value, field_name=key)
        # strict 模式下未登记字段直接丢弃

    return result
