"""统一异常体系。

设计原则：
    1. 所有异常继承 `AppError`，携带 `code`（机器可读）与 `message`（人可读）
    2. 业务错误必须携带 `action` —— 告诉用户**该怎么做**，而不只是"出错了"
    3. 异常可安全序列化（`to_dict()`），供 API 返回与日志记录
    4. 异常不得携带明文凭据或 PII（`context` 需调用方保证已脱敏）

分层：
    AppError
    ├── BusinessError          业务规则拒绝（可预期，需给用户明确指引）
    │   ├── PeriodClosedError  会计期间已关账
    │   ├── ApprovalRequiredError  需要审批才能继续
    │   └── QuotaExceededError 配额用尽
    ├── NotFoundError          资源不存在
    ├── ConflictError          状态冲突（乐观锁失败、重复创建）
    ├── PermissionDeniedError  权限不足
    ├── ConfigurationError     配置错误（启动期校验失败）
    └── ExternalServiceError   外部依赖故障

注意：适配器层的异常是独立体系（adapters/errors.py 的 AdapterError），
      因为它需要携带平台原始错误码、重试语义等额外信息。
      适配器异常在 service 层会被转换为 AppError 的子类。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AppError",
    "BusinessError",
    "NotFoundError",
    "ConflictError",
    "PermissionDeniedError",
    "ConfigurationError",
    "ExternalServiceError",
    "PeriodClosedError",
    "ApprovalRequiredError",
    "QuotaExceededError",
    "IdempotencyConflictError",
    "RateLimitedError",
]


class AppError(Exception):
    """应用异常基类。

    Attributes:
        code: 机器可读的错误码（UPPER_SNAKE），前端据此做分支处理。
        message: 人可读的错误描述。
        action: 建议的下一步操作（可选，但业务错误强烈建议提供）。
        context: 附加上下文（**必须已脱敏**）。
        http_status: 对应的 HTTP 状态码。
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        action: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.action = action
        self.context = context or {}

    def to_dict(self, *, include_context: bool = True) -> dict[str, Any]:
        """序列化。API 响应与结构化日志共用。"""
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.action:
            payload["action"] = self.action
        if include_context and self.context:
            payload["context"] = self.context
        return payload

    def __str__(self) -> str:
        base = f"[{self.code}] {self.message}"
        if self.action:
            base += f"（建议：{self.action}）"
        return base


class BusinessError(AppError):
    """业务规则错误。

    与"程序 bug"的区别：这是**预期内**的拒绝，
    用户按 `action` 操作后通常可以继续。

    示例：
        raise BusinessError(
            code="LISTING_SCHEMA_MISMATCH",
            message="商品缺少类目必填属性 brand",
            action="请在商品编辑页补充品牌信息后重新提交",
            context={"category_id": "beauty", "missing": ["brand"]},
        )
    """

    http_status = 400

    def __init__(
        self,
        message: str,
        *,
        code: str = "BUSINESS_RULE_VIOLATION",
        action: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, action=action, context=context)


class NotFoundError(AppError):
    """资源不存在。"""

    code = "NOT_FOUND"
    http_status = 404

    def __init__(
        self,
        message: str = "资源不存在",
        *,
        resource: str | None = None,
        resource_id: str | int | None = None,
    ) -> None:
        context: dict[str, Any] = {}
        if resource:
            context["resource"] = resource
        if resource_id is not None:
            context["resource_id"] = str(resource_id)
        super().__init__(message, context=context)


class ConflictError(AppError):
    """状态冲突。

    典型场景：
    - 乐观锁失败（两个用户同时审批同一单据）
    - 幂等键冲突（同键不同参数 —— 这通常意味着代码 bug）
    """

    code = "CONFLICT"
    http_status = 409


class PermissionDeniedError(AppError):
    """权限不足。

    与"未登录"不同：这是已认证但无权操作。
    """

    code = "PERMISSION_DENIED"
    http_status = 403

    def __init__(
        self,
        message: str = "权限不足",
        *,
        required_permission: str | None = None,
        required_roles: tuple[str, ...] = (),
    ) -> None:
        context: dict[str, Any] = {}
        if required_permission:
            context["required_permission"] = required_permission
        if required_roles:
            context["required_roles"] = list(required_roles)
        super().__init__(message, context=context)


class ConfigurationError(AppError):
    """配置错误。

    用于启动期校验（如 prod 环境缺少加密密钥）。
    这类错误应当**阻止服务启动** —— 带着错误配置运行比不运行更危险。
    """

    code = "CONFIGURATION_ERROR"
    http_status = 500


class ExternalServiceError(AppError):
    """外部依赖故障（平台 API、LLM、通知服务）。

    service 层捕获 AdapterError 后转换为本异常，
    把"平台错误码"翻译成"业务语言"。
    """

    code = "EXTERNAL_SERVICE_ERROR"
    http_status = 502

    def __init__(
        self,
        message: str,
        *,
        service: str | None = None,
        retryable: bool = False,
        code: str | None = None,
    ) -> None:
        self.retryable = retryable
        context: dict[str, Any] = {"retryable": retryable}
        if service:
            context["service"] = service
        super().__init__(message, code=code, context=context)


# ============================================================
# 具体业务异常
# ============================================================


class PeriodClosedError(BusinessError):
    """会计期间已关账，禁止写入。

    对应 TDD-05 §2.4。关账后任何写入都必须先 reopen（需 admin 权限 + 审计）。
    """

    def __init__(self, period: str, *, shop_id: int | None = None) -> None:
        super().__init__(
            f"会计期间 {period} 已关账，禁止写入",
            code="PERIOD_CLOSED",
            action="如需修改，请先申请重开该期间（需管理员权限并留审计记录）",
            context={"period": period, "shop_id": shop_id},
        )


class ApprovalRequiredError(BusinessError):
    """操作超出免审批额度，需要审批。

    携带已创建的审批单号，方便前端直接跳转。
    """

    def __init__(
        self,
        *,
        approval_no: str | None = None,
        approval_type: str | None = None,
        reason: str = "该操作超过免审批额度",
    ) -> None:
        super().__init__(
            reason,
            code="APPROVAL_REQUIRED",
            action="已提交审批，请在审批中心查看进度"
            if approval_no
            else "请提交审批申请",
            context={"approval_no": approval_no, "approval_type": approval_type},
        )


class QuotaExceededError(BusinessError):
    """平台配额用尽。

    与 RateLimitedError 的区别：
    - 限流是"太快了"，短退避后可重试
    - 配额是"用完了"，重试无用，必须等到重置时间
    """

    def __init__(
        self,
        *,
        platform: str,
        scope: str = "daily",
        reset_at: str | None = None,
    ) -> None:
        super().__init__(
            f"{platform} 的{scope}配额已用尽",
            code="QUOTA_EXCEEDED",
            action=f"任务已改期至 {reset_at} 之后执行" if reset_at else "任务已改期",
            context={"platform": platform, "scope": scope, "reset_at": reset_at},
        )


class IdempotencyConflictError(ConflictError):
    """幂等键冲突：同一幂等键提交了不同参数。

    **这是严重错误，会触发红色告警。**
    正常情况下不可能发生 —— 出现即意味着代码有 bug
    （如幂等键生成逻辑依赖了随机值或未包含全部参数）。
    """

    def __init__(self, idempotency_key: str, *, existing_id: int | None = None) -> None:
        super().__init__(
            f"幂等键 {idempotency_key} 已存在且参数不同",
            code="IDEMPOTENCY_CONFLICT",
        )
        self.context = {
            "idempotency_key": idempotency_key,
            "existing_id": existing_id,
            "severity": "critical",
        }


class RateLimitedError(ExternalServiceError):
    """被平台限流（可重试）。"""

    def __init__(
        self,
        *,
        platform: str,
        retry_after_seconds: int | None = None,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(
            f"{platform} 触发限流",
            service=platform,
            retryable=True,
            code="RATE_LIMITED",
        )
        self.retry_after_seconds = retry_after_seconds
        self.context.update(
            {"platform": platform, "endpoint": endpoint, "retry_after": retry_after_seconds}
        )
