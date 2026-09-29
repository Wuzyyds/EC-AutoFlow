"""Amazon LWA（Login with Amazon）OAuth 2.0 认证。

SP-API 的认证模型：
    1. 卖家在 Seller Central 授权你的应用，得到一个 **refresh_token**
    2. 用 refresh_token 换 **access_token**（有效期 1 小时）
    3. 每次请求在 Header 带 `x-amz-access-token: <access_token>`

关键约束：
    - refresh_token 是**长期凭据**，必须加密存储（core.security.crypto）
    - access_token 是**短期凭据**，可缓存，但必须在过期前刷新
    - 刷新失败要区分"网络问题"（可重试）与"refresh_token 失效"（需重新授权）

本模块只负责**令牌交换**，不负责存储 ——
存储由 repositories 层处理，且必须加密。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import httpx

from adapters.amazon.error_map import AMAZON_LWA_TOKEN_URL
from adapters.errors import AdapterError, ErrorCategory
from core.timeutil import utc_now

__all__ = ["TokenSet", "LWAAuth", "AmazonAuthError"]


class AmazonAuthError(AdapterError):
    """Amazon 认证异常。"""

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("platform", "AMAZON")
        kwargs.setdefault("category", ErrorCategory.AUTH_INVALID)
        super().__init__(**kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TokenSet:
    """令牌集合。"""

    access_token: str
    expires_at: object  # datetime（用 object 避免与 slots 的类型标注冲突）

    token_type: str = "bearer"

    @property
    def is_expired(self) -> bool:
        return utc_now() >= self.expires_at  # type: ignore[operator]

    def expires_within(self, seconds: int) -> bool:
        """是否在 N 秒内过期（用于提前刷新）。"""
        return utc_now() >= (self.expires_at - timedelta(seconds=seconds))  # type: ignore[operator]


#: 提前刷新窗口（秒）。Amazon 的 access_token 有效期 3600 秒，
#: 提前 5 分钟刷新，避免"刚好在用的时候过期"。
REFRESH_AHEAD_SECONDS = 300


class LWAAuth:
    """LWA 令牌交换客户端。

    无状态 —— access_token 的缓存由上层（凭据仓库）负责，
    因为缓存需要跨请求共享且需要持久化。
    """

    def __init__(self, client_id: str, client_secret: str, *, timeout: float = 15.0) -> None:
        if not client_id or not client_secret:
            raise AmazonAuthError(
                message="缺少 Amazon LWA 的 client_id 或 client_secret",
                action="在 .env 中配置 AMAZON_LWA_CLIENT_ID 与 AMAZON_LWA_CLIENT_SECRET",
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout

    async def exchange_refresh_token(self, refresh_token: str) -> TokenSet:
        """用 refresh_token 换取 access_token。

        Raises:
            AmazonAuthError: refresh_token 失效（需卖家重新授权）。
        """
        if not refresh_token:
            raise AmazonAuthError(
                message="refresh_token 为空",
                action="需卖家在 Seller Central 重新完成授权",
            )

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    AMAZON_LWA_TOKEN_URL,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.TimeoutException as exc:
            raise AmazonAuthError(
                message="LWA 令牌交换超时",
                category=ErrorCategory.TIMEOUT,
                retryable=True,
                raw_message=str(exc),
            ) from exc
        except httpx.HTTPError as exc:
            raise AmazonAuthError(
                message="LWA 令牌交换网络失败",
                category=ErrorCategory.SERVICE_UNAVAILABLE,
                retryable=True,
                raw_message=str(exc),
            ) from exc

        if resp.status_code != 200:  # noqa: PLR2004
            body = _safe_json(resp)
            error_code = body.get("error", "")
            # invalid_grant 意味着 refresh_token 已失效，必须重新授权
            if error_code == "invalid_grant":
                raise AmazonAuthError(
                    message="refresh_token 已失效",
                    category=ErrorCategory.AUTH_INVALID,
                    retryable=False,
                    raw_code=error_code,
                    raw_message=body.get("error_description", ""),
                    action="需卖家在 Seller Central 重新完成授权",
                )
            raise AmazonAuthError(
                message=f"LWA 令牌交换失败：HTTP {resp.status_code}",
                category=ErrorCategory.AUTH_REFRESH_FAILED,
                retryable=True,
                http_status=resp.status_code,
                raw_code=error_code or None,
                raw_message=body.get("error_description", resp.text[:300]),
            )

        body = resp.json()
        expires_in = int(body.get("expires_in", 3600))
        return TokenSet(
            access_token=body["access_token"],
            expires_at=utc_now() + timedelta(seconds=expires_in),
            token_type=body.get("token_type", "bearer"),
        )

    def needs_refresh(self, token: TokenSet | None) -> bool:
        """判断是否需要刷新。"""
        if token is None:
            return True
        return token.expires_within(REFRESH_AHEAD_SECONDS)

    @staticmethod
    def auth_headers(access_token: str) -> dict[str, str]:
        """构造 SP-API 的认证 Header。

        **注意**：这是 SP-API 的写法。Ads API 用的是另外两个 Header
        （`Amazon-Advertising-API-ClientId` + `Amazon-Advertising-API-Scope`），
        混用会 401/403。
        """
        return {"x-amz-access-token": access_token}


def _safe_json(resp: httpx.Response) -> dict[str, object]:
    """安全解析 JSON（失败返回空字典，不抛异常）。

    认证失败时平台可能返回 HTML 错误页，直接 .json() 会抛异常
    并掩盖真正的错误信息。
    """
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
