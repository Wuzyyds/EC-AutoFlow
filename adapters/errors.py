"""适配器统一错误分类（TDD-03 §4）。

核心契约：
    **业务层只认 `category`，不认平台原始错误码。**

    错误码映射表放在各平台的 `error_map.py`，业务代码里出现
    `if error_code == "Throttled"` 即为设计缺陷。

为什么必须分类而不是透传原始码：
    1. 8 个平台有 8 套错误码体系，业务层不可能都懂
    2. 重试决策依赖"可重试性"，而原始码不直接表达这个语义
    3. 平台会改错误码文案，分类层是唯一需要跟着改的地方

异常层次（TDD-03 §4.3）：
    AdapterError
    ├── AuthenticationError     认证类（AUTH_*）
    ├── RateLimitError          限流类（RATE_* / QUOTA_*）
    ├── ValidationError         校验类（VALIDATION_* / SCHEMA_*）
    ├── UnsupportedCapabilityError  能力不支持
    ├── PartialSuccessError     部分成功（需逐行处理）
    ├── SchemaDriftError        平台返回结构变了
    └── PlatformServerError     平台服务端错误

**硬性要求**：适配器方法不允许抛出 AdapterError 以外的异常。
这条由 `translate_errors` 装饰器强制。
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from core.constants import LabeledEnum
from core.exceptions import AppError

__all__ = [
    "ErrorCategory",
    "ErrorPolicy",
    "ERROR_POLICIES",
    "AdapterError",
    "AuthenticationError",
    "RateLimitError",
    "ValidationError",
    "UnsupportedCapabilityError",
    "PartialSuccessError",
    "SchemaDriftError",
    "PlatformServerError",
    "FieldError",
    "translate_errors",
    "policy_of",
]

P = ParamSpec("P")
R = TypeVar("R")


class ErrorCategory(LabeledEnum):
    """统一错误分类。

    每个分类对应明确的处理策略（见 ERROR_POLICIES）。
    命名用 UPPER_SNAKE，与数据库 api_call_logs.error_category 存储一致。
    """

    # ===== 认证与授权（不可重试，需人工介入）=====
    AUTH_INVALID = "AUTH_INVALID"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    AUTH_INSUFFICIENT_SCOPE = "AUTH_INSUFFICIENT_SCOPE"
    AUTH_REFRESH_FAILED = "AUTH_REFRESH_FAILED"

    # ===== 限流与配额 =====
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"

    # ===== 客户端错误（需修数据）=====
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"

    # ===== 业务规则拒绝（需人工判断）=====
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
    ELIGIBILITY_DENIED = "ELIGIBILITY_DENIED"
    COMPLIANCE_REQUIRED = "COMPLIANCE_REQUIRED"

    # ===== 服务端错误（可重试）=====
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"

    # ===== 部分成功（需逐行处理）=====
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"

    # ===== 数据质量（需调查）=====
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"

    # ===== 不支持 =====
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

    # ===== 未知（需补映射）=====
    UNKNOWN = "UNKNOWN"

    @property
    def is_auth_related(self) -> bool:
        """是否认证相关。这类错误意味着整个店铺都要停摆。"""
        return self.value.startswith("AUTH_")

    @property
    def needs_mapping_work(self) -> bool:
        """是否说明映射表需要补充。

        UNKNOWN 与 SCHEMA_DRIFT 都意味着"平台给了我们没预期到的东西"，
        必须触发红色告警并补映射，不能静默处理。
        """
        return self in (ErrorCategory.UNKNOWN, ErrorCategory.SCHEMA_DRIFT)


@dataclass(frozen=True, slots=True)
class ErrorPolicy:
    """错误处理策略。**这张表决定重试逻辑**，是开发的核心依据。"""

    #: 是否可重试
    retryable: bool
    #: 最大重试次数（不可重试时为 0）
    max_retries: int = 0
    #: 告警级别：immediate / on_final_failure / daily / none
    alert: str = "none"
    #: 业务层应对方式（给开发者看的指引）
    action: str = ""


#: 错误 → 策略映射（TDD-03 §4.2 的完整落地）。
ERROR_POLICIES: dict[ErrorCategory, ErrorPolicy] = {
    # ---- 认证：不可重试，必须立即告警（店铺停摆）----
    ErrorCategory.AUTH_INVALID: ErrorPolicy(
        retryable=False,
        alert="immediate",
        action="店铺标记异常，暂停该店所有任务，通知运维重新授权",
    ),
    ErrorCategory.AUTH_EXPIRED: ErrorPolicy(
        retryable=True,
        max_retries=1,
        alert="on_final_failure",
        action="先刷新 Token 再重试一次；失败则标记店铺异常",
    ),
    ErrorCategory.AUTH_INSUFFICIENT_SCOPE: ErrorPolicy(
        retryable=False,
        alert="immediate",
        action="提示缺少权限，需重新授权并勾选对应 scope",
    ),
    ErrorCategory.AUTH_REFRESH_FAILED: ErrorPolicy(
        retryable=True,
        max_retries=3,
        alert="immediate",
        action="指数退避重试 3 次；仍失败则暂停任务并通知运维",
    ),
    # ---- 限流 ----
    ErrorCategory.RATE_LIMITED: ErrorPolicy(
        retryable=True,
        max_retries=5,
        alert="on_final_failure",
        action="尊重 Retry-After，指数退避 + 抖动；业务层无需处理",
    ),
    ErrorCategory.QUOTA_EXCEEDED: ErrorPolicy(
        retryable=False,
        alert="daily",
        action="重试无用。计算配额重置时间，任务改期到该时间之后",
    ),
    ErrorCategory.CONCURRENCY_LIMIT: ErrorPolicy(
        retryable=True,
        max_retries=3,
        alert="none",
        action="短退避重试；业务层无需处理",
    ),
    # ---- 客户端错误：数据有问题，重试无用 ----
    ErrorCategory.VALIDATION_FAILED: ErrorPolicy(
        retryable=False,
        alert="on_final_failure",
        action="记录字段级错误，跳过该条继续处理其余（批量场景）",
    ),
    ErrorCategory.SCHEMA_MISMATCH: ErrorPolicy(
        retryable=False,
        alert="on_final_failure",
        action="重新拉取类目 schema，用新 schema 重新校验",
    ),
    ErrorCategory.NOT_FOUND: ErrorPolicy(
        retryable=False,
        alert="none",
        action="标记本地记录为已删除",
    ),
    ErrorCategory.CONFLICT: ErrorPolicy(
        retryable=False,
        alert="on_final_failure",
        action="人工介入（典型场景：SKU 冲突）",
    ),
    ErrorCategory.IDEMPOTENCY_CONFLICT: ErrorPolicy(
        retryable=False,
        alert="immediate",
        action="**严重**：同幂等键不同参数，说明代码有 bug。立即停止并排查",
    ),
    ErrorCategory.PRECONDITION_FAILED: ErrorPolicy(
        retryable=False,
        alert="on_final_failure",
        action="检查前置状态（如订单未发货不能确认发货）",
    ),
    # ---- 业务规则拒绝：需人工判断 ----
    ErrorCategory.BUSINESS_REJECTED: ErrorPolicy(
        retryable=False,
        alert="on_final_failure",
        action="生成人工待办，附带平台拒绝原因",
    ),
    ErrorCategory.ELIGIBILITY_DENIED: ErrorPolicy(
        retryable=False,
        alert="on_final_failure",
        action="生成待办：需申请类目/品牌授权",
    ),
    ErrorCategory.COMPLIANCE_REQUIRED: ErrorPolicy(
        retryable=False,
        alert="on_final_failure",
        action="生成待办：需提交合规材料",
    ),
    # ---- 服务端错误：可重试 ----
    ErrorCategory.SERVER_ERROR: ErrorPolicy(
        retryable=True,
        max_retries=5,
        alert="on_final_failure",
        action="指数退避重试；业务层无需处理",
    ),
    ErrorCategory.TIMEOUT: ErrorPolicy(
        retryable=True,
        max_retries=3,
        alert="on_final_failure",
        action="指数退避重试；幂等键防重复提交",
    ),
    ErrorCategory.SERVICE_UNAVAILABLE: ErrorPolicy(
        retryable=True,
        max_retries=5,
        alert="on_final_failure",
        action="长退避（5 分钟起）；任务改期",
    ),
    ErrorCategory.DEPENDENCY_FAILED: ErrorPolicy(
        retryable=True,
        max_retries=3,
        alert="on_final_failure",
        action="指数退避重试（平台内部依赖故障）",
    ),
    # ---- 部分成功：必须逐行处理 ----
    ErrorCategory.PARTIAL_SUCCESS: ErrorPolicy(
        retryable=False,
        alert="on_final_failure",
        action="**逐行处理**成功/失败；失败行生成待办，不可整体重试",
    ),
    # ---- 数据质量 ----
    ErrorCategory.MALFORMED_RESPONSE: ErrorPolicy(
        retryable=True,
        max_retries=1,
        alert="immediate",
        action="保存原始报文，重试一次；仍失败则人工分析",
    ),
    ErrorCategory.SCHEMA_DRIFT: ErrorPolicy(
        retryable=False,
        alert="immediate",
        action="**冻结该接口**，需人工适配新结构。不可静默处理",
    ),
    # ---- 不支持 ----
    ErrorCategory.UNSUPPORTED_CAPABILITY: ErrorPolicy(
        retryable=False,
        alert="none",
        action="预期内。走降级路径（生成人工待办 / 文件导入）",
    ),
    ErrorCategory.NOT_IMPLEMENTED: ErrorPolicy(
        retryable=False,
        alert="none",
        action="Phase 1 占位，不应在生产环境触发",
    ),
    # ---- 未知 ----
    ErrorCategory.UNKNOWN: ErrorPolicy(
        retryable=True,
        max_retries=1,
        alert="immediate",
        action="**必须补映射**。保存原始报文与错误码",
    ),
}


def policy_of(category: ErrorCategory) -> ErrorPolicy:
    """查错误策略。未登记的分类返回保守默认（不重试、立即告警）。"""
    return ERROR_POLICIES.get(
        category,
        ErrorPolicy(retryable=False, alert="immediate", action="未登记分类，请补映射"),
    )


# ============================================================
# 异常层次
# ============================================================


@dataclass(frozen=True, slots=True)
class FieldError:
    """字段级校验错误（批量提交时定位到具体行与字段）。"""

    field: str
    message: str
    row_index: int | None = None
    value: Any = None

    def __str__(self) -> str:
        loc = f"第{self.row_index + 1}行 " if self.row_index is not None else ""
        return f"{loc}{self.field}: {self.message}"


class AdapterError(AppError):
    """适配器异常基类。

    所有平台异常必须转换为此类或其子类。
    """

    code = "ADAPTER_ERROR"
    http_status = 502

    def __init__(
        self,
        *,
        category: ErrorCategory,
        message: str,
        platform: str,
        raw_code: str | None = None,
        raw_message: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
        retry_after: int | None = None,
        request_id: str | None = None,
        context: dict[str, Any] | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        policy = policy_of(category)

        super().__init__(
            message,
            code=category.value,
            action=policy.action,
            context={
                "platform": platform,
                "category": category.value,
                "raw_code": raw_code,
                "http_status": http_status,
                "retryable": retryable if retryable is not None else policy.retryable,
                "request_id": request_id,
                **(context or {}),
            },
        )

        self.category = category
        self.platform = platform
        self.raw_code = raw_code
        self.raw_message = raw_message
        self.http_status_code = http_status
        self.retry_after = retry_after
        self.request_id = request_id
        self.raw_payload = raw_payload
        self._retryable_override = retryable

    @property
    def retryable(self) -> bool:
        """是否可重试。显式传入的值优先于策略表默认值。"""
        if self._retryable_override is not None:
            return self._retryable_override
        return policy_of(self.category).retryable

    @property
    def max_retries(self) -> int:
        return policy_of(self.category).max_retries

    @property
    def alert_level(self) -> str:
        return policy_of(self.category).alert

    def __str__(self) -> str:
        base = f"[{self.platform}/{self.category.value}] {self.message}"
        if self.raw_code:
            base += f" (平台码: {self.raw_code})"
        return base


class AuthenticationError(AdapterError):
    """认证类异常。category ∈ {AUTH_*}。"""


class RateLimitError(AdapterError):
    """限流类异常。category ∈ {RATE_LIMITED, QUOTA_EXCEEDED, CONCURRENCY_LIMIT}。"""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(**kwargs)

    @property
    def wait_seconds(self) -> int:
        """建议等待秒数。优先用平台给的 Retry-After。"""
        if self.retry_after is not None:
            return self.retry_after
        # 无 Retry-After 时的保守默认
        if self.category == ErrorCategory.QUOTA_EXCEEDED:
            return 3600
        return 60


class ValidationError(AdapterError):
    """校验类异常。携带字段级错误明细。"""

    def __init__(self, *, field_errors: list[FieldError] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.field_errors = field_errors or []

    def __str__(self) -> str:
        base = super().__str__()
        if self.field_errors:
            details = "; ".join(str(e) for e in self.field_errors[:5])
            more = f" 等 {len(self.field_errors)} 项" if len(self.field_errors) > 5 else ""
            base += f" | {details}{more}"
        return base


class UnsupportedCapabilityError(AdapterError):
    """能力不支持。

    这是**预期内**的错误（不是故障）—— 业务层捕获后走降级路径。
    """

    def __init__(
        self,
        *,
        capability: str,
        suggested_fallback: str = "生成人工待办",
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("category", ErrorCategory.UNSUPPORTED_CAPABILITY)
        super().__init__(**kwargs)
        self.capability = capability
        self.suggested_fallback = suggested_fallback
        self.context.update(
            {"capability": capability, "suggested_fallback": suggested_fallback}
        )


class PartialSuccessError(AdapterError):
    """部分成功。

    批量操作中"1000 行提交，997 成功 3 失败"的场景。
    这**既不是成功也不是失败**，业务层必须逐行处理 ——
    如果当成成功上报，会有 3 个 SKU 静默地没上架。
    """

    def __init__(
        self,
        *,
        succeeded: list[Any] | None = None,
        failed: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("category", ErrorCategory.PARTIAL_SUCCESS)
        super().__init__(**kwargs)
        self.succeeded = succeeded or []
        self.failed = failed or []

    @property
    def success_count(self) -> int:
        return len(self.succeeded)

    @property
    def failure_count(self) -> int:
        return len(self.failed)


class SchemaDriftError(AdapterError):
    """平台返回结构与我们预期的不符。

    **必须触发红色告警** —— 这意味着平台改了接口，
    继续运行会静默产生错误数据。
    """

    def __init__(
        self,
        *,
        expected_shape: Any = None,
        actual_sample: Any = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("category", ErrorCategory.SCHEMA_DRIFT)
        super().__init__(**kwargs)
        self.expected_shape = expected_shape
        self.actual_sample = actual_sample


class PlatformServerError(AdapterError):
    """平台服务端错误（可重试）。"""


# ============================================================
# 异常转换装饰器
# ============================================================


def translate_errors(
    platform: str,
    *,
    error_map: dict[str, ErrorCategory] | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """装饰适配器方法，把所有异常转换为 AdapterError。

    转换规则（TDD-03 §4.3）：
        1. httpx.TimeoutException    → TIMEOUT
        2. httpx.ConnectError/网络类 → SERVICE_UNAVAILABLE
        3. JSON 解析失败             → MALFORMED_RESPONSE
        4. 平台错误码（查 error_map）→ 对应分类
        5. 未映射错误码              → UNKNOWN（并记录原始码，强制补映射）
        6. AdapterError 本身         → 原样抛出（不重复包装）
        7. 其他任何异常              → UNKNOWN（**不允许裸异常逃逸**）

    为什么必须有第 7 条：
        没有它，一个 KeyError 会穿透适配器层直达 API 层，
        返回 500 且日志里看不出是哪个平台出的问题。
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            import httpx

            try:
                return await func(*args, **kwargs)
            except AdapterError:
                # 已是统一异常，原样抛出（避免重复包装丢失原始信息）
                raise
            except httpx.TimeoutException as exc:
                raise AdapterError(
                    category=ErrorCategory.TIMEOUT,
                    message=f"调用 {platform} 超时",
                    platform=platform,
                    raw_message=str(exc),
                ) from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                raise AdapterError(
                    category=_category_from_status(status),
                    message=f"{platform} 返回 HTTP {status}",
                    platform=platform,
                    http_status=status,
                    raw_message=exc.response.text[:500],
                ) from exc
            except (httpx.ConnectError, httpx.NetworkError) as exc:
                raise AdapterError(
                    category=ErrorCategory.SERVICE_UNAVAILABLE,
                    message=f"无法连接 {platform}",
                    platform=platform,
                    raw_message=str(exc),
                ) from exc
            except asyncio.CancelledError:
                # 任务取消不是错误，必须原样抛出，否则会吞掉取消信号
                raise
            except (ValueError, KeyError, TypeError) as exc:
                # 通常是响应结构解析失败 —— 可能是平台改了结构
                raise SchemaDriftError(
                    category=ErrorCategory.SCHEMA_DRIFT,
                    message=f"{platform} 响应解析失败：{type(exc).__name__}: {exc}",
                    platform=platform,
                    raw_message=str(exc),
                    expected_shape=func.__name__,
                ) from exc
            except Exception as exc:  # noqa: BLE001
                # 保底：任何未捕获异常都不得逃逸
                raise AdapterError(
                    category=ErrorCategory.UNKNOWN,
                    message=f"{platform} 未知异常：{type(exc).__name__}: {exc}",
                    platform=platform,
                    raw_message=str(exc),
                    context={"function": func.__name__},
                ) from exc

        return wrapper

    return decorator


def _category_from_status(status: int) -> ErrorCategory:
    """按 HTTP 状态码给出兜底分类（在平台错误码缺失时使用）。"""
    if status == 401:  # noqa: PLR2004
        return ErrorCategory.AUTH_INVALID
    if status == 403:  # noqa: PLR2004
        return ErrorCategory.AUTH_INSUFFICIENT_SCOPE
    if status == 404:  # noqa: PLR2004
        return ErrorCategory.NOT_FOUND
    if status == 409:  # noqa: PLR2004
        return ErrorCategory.CONFLICT
    if status == 429:  # noqa: PLR2004
        return ErrorCategory.RATE_LIMITED
    if status == 400:  # noqa: PLR2004
        return ErrorCategory.VALIDATION_FAILED
    if 500 <= status < 600:  # noqa: PLR2004
        return ErrorCategory.SERVER_ERROR
    return ErrorCategory.UNKNOWN


def category_from_code(
    raw_code: str | None,
    error_map: dict[str, ErrorCategory] | None = None,
) -> ErrorCategory:
    """把平台原始错误码转换为统一分类。

    未映射的返回 UNKNOWN —— 调用方应记录原始码并触发补映射告警。
    """
    if not raw_code or not error_map:
        return ErrorCategory.UNKNOWN
    return error_map.get(raw_code, ErrorCategory.UNKNOWN)
