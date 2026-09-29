"""Amazon SP-API 错误码映射（TDD-03 §4.4）。

数据来源：官方错误响应文档 + 实测。

**未列出的错误码会映射为 UNKNOWN 并触发红色告警** ——
这是刻意的：UNKNOWN 意味着"平台给了我们没预期到的东西"，
必须补映射，不能静默处理。

Amazon 特有的四个坑（写进 adapters/amazon/AGENTS.md）：

1. **两套 Header 不能混用**
   SP-API 用 `x-amz-access-token`；
   Ads API 用 `Amazon-Advertising-API-ClientId` + `Amazon-Advertising-API-Scope`。
   混用直接 401/403。

2. **Feeds 是异步的，四步缺一不可**
   createFeedDocument → createFeed → 轮询 getFeed 到 DONE → getFeedDocument
   拿 processingReport。**"提交成功" ≠ "上架成功"**。

3. **区域 Base URL 三套**（NA / EU / FE），必须按 shop.region 路由。

4. **429 会带 Retry-After**，必须尊重该值，不能用自己的退避策略硬顶。
"""

from __future__ import annotations

from adapters.errors import ErrorCategory

__all__ = [
    "AMAZON_ERROR_MAP",
    "AMAZON_FEED_ROW_STATUS_MAP",
    "AMAZON_REGION_ENDPOINTS",
    "map_amazon_error",
]

#: 平台错误码 → 统一分类
AMAZON_ERROR_MAP: dict[str, ErrorCategory] = {
    # ---------- 认证 ----------
    "Unauthorized": ErrorCategory.AUTH_INVALID,
    "InvalidAccessToken": ErrorCategory.AUTH_EXPIRED,
    "AccessDenied": ErrorCategory.AUTH_INSUFFICIENT_SCOPE,
    "InvalidSignature": ErrorCategory.AUTH_INVALID,
    "MissingAuthenticationToken": ErrorCategory.AUTH_INVALID,
    # ---------- 限流 ----------
    "Throttled": ErrorCategory.RATE_LIMITED,
    "RequestThrottled": ErrorCategory.RATE_LIMITED,
    "QuotaExceeded": ErrorCategory.QUOTA_EXCEEDED,
    "TooManyRequests": ErrorCategory.RATE_LIMITED,
    # ---------- 校验 ----------
    "InvalidInput": ErrorCategory.VALIDATION_FAILED,
    "InvalidParameterValue": ErrorCategory.VALIDATION_FAILED,
    "MissingParameter": ErrorCategory.VALIDATION_FAILED,
    "InvalidMarketplaceId": ErrorCategory.VALIDATION_FAILED,
    "InvalidRequest": ErrorCategory.VALIDATION_FAILED,
    "InvalidFormat": ErrorCategory.SCHEMA_MISMATCH,
    # ---------- 资源 ----------
    "NotFound": ErrorCategory.NOT_FOUND,
    "ResourceNotFound": ErrorCategory.NOT_FOUND,
    "Conflict": ErrorCategory.CONFLICT,
    "DuplicateResource": ErrorCategory.CONFLICT,
    # ---------- 业务 ----------
    "InvalidListing": ErrorCategory.BUSINESS_REJECTED,
    "RestrictedProduct": ErrorCategory.ELIGIBILITY_DENIED,
    "BrandNotAuthorized": ErrorCategory.ELIGIBILITY_DENIED,
    "CategoryNotOpen": ErrorCategory.ELIGIBILITY_DENIED,
    "ApprovalRequired": ErrorCategory.COMPLIANCE_REQUIRED,
    "UnsupportedMarketplace": ErrorCategory.PRECONDITION_FAILED,
    # ---------- 服务端 ----------
    "InternalFailure": ErrorCategory.SERVER_ERROR,
    "InternalServerError": ErrorCategory.SERVER_ERROR,
    "ServiceUnavailable": ErrorCategory.SERVICE_UNAVAILABLE,
    "GatewayTimeout": ErrorCategory.TIMEOUT,
}

#: Feed 处理报告中的逐行状态（与 HTTP 错误码是不同体系）。
#:
#: None 表示成功或无错误。
AMAZON_FEED_ROW_STATUS_MAP: dict[str, ErrorCategory | None] = {
    "ACCEPTED": None,
    "INVALID": ErrorCategory.VALIDATION_FAILED,
    "FATAL": ErrorCategory.BUSINESS_REJECTED,
    "WARNING": None,  # 有警告但成功
    "SUCCESS": None,
}

#: 区域端点（坑 3：必须按 region 路由，硬编码即 bug）
AMAZON_REGION_ENDPOINTS: dict[str, str] = {
    "NA": "https://sellingpartnerapi-na.amazon.com",
    "EU": "https://sellingpartnerapi-eu.amazon.com",
    "FE": "https://sellingpartnerapi-fe.amazon.com",
    # 沙箱（覆盖不全，见 TDD-03 §2.3 脚注 3）
    "NA_SANDBOX": "https://sandbox.sellingpartnerapi-na.amazon.com",
    "EU_SANDBOX": "https://sandbox.sellingpartnerapi-eu.amazon.com",
}

#: LWA 令牌端点（全球统一，不分区域）
AMAZON_LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


def map_amazon_error(
    raw_code: str | None,
    *,
    http_status: int | None = None,
) -> ErrorCategory:
    """把 Amazon 错误码映射为统一分类。

    先查映射表，查不到再按 HTTP 状态码兜底，
    最后落到 UNKNOWN（触发补映射告警）。
    """
    from adapters.errors import _category_from_status

    if raw_code and raw_code in AMAZON_ERROR_MAP:
        return AMAZON_ERROR_MAP[raw_code]
    if http_status is not None:
        return _category_from_status(http_status)
    return ErrorCategory.UNKNOWN
