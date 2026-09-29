"""Amazon SP-API HTTP 客户端（带限流与重试）。

设计约束（TDD-01 §3.3）：
    **所有调用必须经过本客户端，禁止裸用 httpx** ——
    否则会绕过限流控制。

三个必须处理好的点：

1. **区域路由**
   NA / EU / FE 三套 Base URL，硬编码即 bug。

2. **尊重 Retry-After**
   429 响应会带 `Retry-After`（秒）。必须用它，
   不要用自己的退避策略硬顶 —— 平台知道什么时候能恢复。

3. **令牌自动刷新**
   access_token 有效期 1 小时。在过期前自动刷新，
   而不是等收到 401 再补救（那样每次过期都会浪费一个请求）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from adapters.amazon.auth import LWAAuth, TokenSet
from adapters.amazon.error_map import AMAZON_REGION_ENDPOINTS, map_amazon_error
from adapters.errors import AdapterError, ErrorCategory
from core.timeutil import utc_now

__all__ = ["AmazonCredentials", "AmazonClient"]

#: 默认重试次数
DEFAULT_MAX_RETRIES = 5

#: 退避基数（秒）
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0


@dataclass
class AmazonCredentials:
    """Amazon 店铺凭据。

    **注意**：明文凭据只在内存中存在最小时间（TDD-06 §2.1）。
    本对象由 service 层在用完后立即丢弃，不长期持有。
    """

    refresh_token: str
    access_token: str | None = None
    access_token_expires_at: object | None = None  # datetime

    def to_token_set(self) -> TokenSet | None:
        if not self.access_token or not self.access_token_expires_at:
            return None
        return TokenSet(
            access_token=self.access_token,
            expires_at=self.access_token_expires_at,
        )


class AmazonClient:
    """SP-API 客户端。

    无状态（除令牌缓存外）—— 可安全地在协程间共享。
    """

    def __init__(
        self,
        *,
        region: str,
        auth: LWAAuth,
        credentials: AmazonCredentials,
        marketplace_id: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = 30.0,
    ) -> None:
        self._region = region.upper()
        self._auth = auth
        self._credentials = credentials
        self._marketplace_id = marketplace_id
        self._max_retries = max_retries
        self._timeout = timeout
        self._token: TokenSet | None = credentials.to_token_set()
        self._lock = asyncio.Lock()

    # ========================================================
    # 区域路由
    # ========================================================

    @property
    def base_url(self) -> str:
        """按区域返回 Base URL。

        Raises:
            AdapterError: 未知区域。宁可报错也不要猜 ——
                用错区域会导致 401 或查不到数据，很难排查。
        """
        url = AMAZON_REGION_ENDPOINTS.get(self._region)
        if url is None:
            raise AdapterError(
                category=ErrorCategory.PRECONDITION_FAILED,
                message=f"未知的 Amazon 区域：{self._region}",
                platform="AMAZON",
                context={
                    "region": self._region,
                    "supported": sorted(AMAZON_REGION_ENDPOINTS),
                },
                action="检查店铺配置的 region 是否为 NA / EU / FE",
            )
        return url

    # ========================================================
    # 令牌管理
    # ========================================================

    async def _ensure_token(self) -> str:
        """确保有可用的 access_token，必要时刷新。

        用锁保护：并发请求同时发现令牌过期时，
        只应该刷新一次，而不是每个请求都去刷。
        """
        async with self._lock:
            if not self._auth.needs_refresh(self._token):
                assert self._token is not None
                return self._token.access_token

            token = await self._auth.exchange_refresh_token(self._credentials.refresh_token)
            self._token = token
            # 同步回凭据对象（上层可能持久化）
            self._credentials.access_token = token.access_token
            self._credentials.access_token_expires_at = token.expires_at
            return token.access_token

    def _invalidate_token(self) -> None:
        """令牌失效时清空缓存，下次请求会重新获取。"""
        self._token = None
        self._credentials.access_token = None

    # ========================================================
    # 请求
    # ========================================================

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """发起 SP-API 请求，返回解析后的 JSON。

        自动处理：
            - 令牌获取与刷新（含 401 后重试一次）
            - 429 尊重 Retry-After
            - 5xx 指数退避重试
            - 所有错误转换为 AdapterError

        Raises:
            AdapterError: 所有错误已分类。
        """
        last_error: AdapterError | None = None

        for attempt in range(self._max_retries + 1):
            token = await self._ensure_token()
            req_headers = {
                "x-amz-access-token": token,
                "Accept": "application/json",
                **(headers or {}),
            }
            if self._marketplace_id and method.upper() == "POST":
                req_headers.setdefault("Content-Type", "application/json")

            try:
                async with httpx.AsyncClient(timeout=timeout or self._timeout) as client:
                    resp = await client.request(
                        method.upper(),
                        f"{self.base_url}{path}",
                        params=params,
                        json=json_body,
                        headers=req_headers,
                    )
            except httpx.TimeoutException as exc:
                last_error = AdapterError(
                    category=ErrorCategory.TIMEOUT,
                    message=f"请求 {path} 超时",
                    platform="AMAZON",
                    retryable=True,
                    raw_message=str(exc),
                    context={"path": path, "attempt": attempt},
                )
                await self._sleep_backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                last_error = AdapterError(
                    category=ErrorCategory.SERVICE_UNAVAILABLE,
                    message=f"请求 {path} 网络失败",
                    platform="AMAZON",
                    retryable=True,
                    raw_message=str(exc),
                    context={"path": path, "attempt": attempt},
                )
                await self._sleep_backoff(attempt)
                continue

            # ---- 成功 ----
            if 200 <= resp.status_code < 300:  # noqa: PLR2004
                return _parse_json(resp, path)

            # ---- 限流：尊重 Retry-After ----
            if resp.status_code == 429:  # noqa: PLR2004
                wait = _retry_after_seconds(resp) or int(BACKOFF_BASE * (2**attempt))
                last_error = AdapterError(
                    category=ErrorCategory.RATE_LIMITED,
                    message=f"Amazon 限流（{path}）",
                    platform="AMAZON",
                    http_status=429,
                    retryable=True,
                    retry_after=wait,
                    context={"path": path, "attempt": attempt},
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(min(wait, BACKOFF_MAX))
                    continue
                break

            # ---- 401：令牌可能失效，清缓存后重试一次 ----
            if resp.status_code == 401 and attempt == 0:  # noqa: PLR2004
                self._invalidate_token()
                last_error = AdapterError(
                    category=ErrorCategory.AUTH_EXPIRED,
                    message=f"Amazon 返回 401（{path}），已刷新令牌重试",
                    platform="AMAZON",
                    http_status=401,
                    retryable=True,
                    context={"path": path},
                )
                continue

            # ---- 其他错误 ----
            body = _parse_json_safe(resp)
            raw_code = _extract_error_code(body)
            category = map_amazon_error(raw_code, http_status=resp.status_code)
            err = AdapterError(
                category=category,
                message=f"Amazon 返回 HTTP {resp.status_code}（{path}）",
                platform="AMAZON",
                raw_code=raw_code,
                raw_message=_extract_error_message(body) or resp.text[:300],
                http_status=resp.status_code,
                context={"path": path, "attempt": attempt},
                raw_payload=body if isinstance(body, dict) else None,
            )

            # 认证类错误不重试（重试也不会好）
            if category.is_auth_related:
                raise err

            last_error = err
            if err.retryable and attempt < self._max_retries:
                await self._sleep_backoff(attempt)
                continue
            break

        raise last_error or AdapterError(
            category=ErrorCategory.UNKNOWN,
            message=f"请求 {path} 失败且无具体错误",
            platform="AMAZON",
            context={"path": path},
        )

    # ========================================================
    # 文件传输（Feed / Reports 用）
    # ========================================================

    async def download(self, url: str) -> bytes:
        """下载文件（Feed 文档、报表）。

        注意：Amazon 的文件 URL 是**预签名 URL**，
        不需要（也不应该）带认证 Header。
        """
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPError as exc:
            raise AdapterError(
                category=ErrorCategory.SERVICE_UNAVAILABLE,
                message="下载 Amazon 文件失败",
                platform="AMAZON",
                retryable=True,
                raw_message=str(exc),
            ) from exc

    async def upload(self, url: str, content: bytes) -> None:
        """上传文件到预签名 URL。

        Amazon 要求 `Content-Type: application/octet-stream`，
        且预签名 URL 里已含认证信息，不要额外加 Header。
        """
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.put(
                    url,
                    content=content,
                    headers={"Content-Type": "application/octet-stream"},
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise AdapterError(
                category=ErrorCategory.SERVICE_UNAVAILABLE,
                message="上传文件到 Amazon 失败",
                platform="AMAZON",
                retryable=True,
                raw_message=str(exc),
            ) from exc

    # ========================================================
    # 内部
    # ========================================================

    async def _sleep_backoff(self, attempt: int) -> None:
        """指数退避（带抖动）。

        抖动是必要的 —— 多个 worker 同时重试会形成"重试风暴"，
        把平台刚恢复的容量再打挂。
        """
        import random

        delay = min(BACKOFF_BASE * (2**attempt), BACKOFF_MAX)
        jitter = random.uniform(0, delay * 0.3)
        await asyncio.sleep(delay + jitter)


# ============================================================
# 辅助函数
# ============================================================


def _retry_after_seconds(resp: httpx.Response) -> int | None:
    """从响应头提取 Retry-After（秒）。"""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(int(float(raw)), 1)
    except (TypeError, ValueError):
        return None


def _parse_json(resp: httpx.Response, path: str) -> dict[str, Any]:
    """解析成功响应。

    204 / 空响应返回空字典 —— 部分接口（如 delete）无响应体。
    """
    if resp.status_code == 204 or not resp.content:  # noqa: PLR2004
        return {}
    try:
        data = resp.json()
    except ValueError as exc:
        # 成功状态码但返回非 JSON —— 这是结构漂移，必须告警
        raise AdapterError(
            category=ErrorCategory.SCHEMA_DRIFT,
            message=f"Amazon 返回 200 但响应不是合法 JSON（{path}）",
            platform="AMAZON",
            raw_message=resp.text[:300],
            context={"path": path},
        ) from exc
    if isinstance(data, dict):
        return data
    # 部分接口直接返回数组
    return {"payload": data}


def _parse_json_safe(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"payload": data}
    except Exception:  # noqa: BLE001
        return {}


def _extract_error_code(body: dict[str, Any]) -> str | None:
    """从 Amazon 错误响应中提取错误码。

    Amazon 的错误响应结构不统一，有三种形态：
        {"errors": [{"code": "...", "message": "..."}]}
        {"code": "...", "message": "..."}
        {"error": "..."}
    """
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            code = first.get("code")
            return str(code) if code else None
    code = body.get("code")
    if code:
        return str(code)
    err = body.get("error")
    if err:
        return str(err)
    return None


def _extract_error_message(body: dict[str, Any]) -> str | None:
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            msg = first.get("message")
            return str(msg) if msg else None
    msg = body.get("message") or body.get("error_description")
    return str(msg) if msg else None
