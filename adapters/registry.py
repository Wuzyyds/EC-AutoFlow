"""适配器注册表与工厂。

核心职责：
    1. 平台名 → 适配器类的注册与查找
    2. **环境强制**：local / test 环境必须用 Mock 适配器

第 2 条是硬约束（TDD-01 §4.1）：
    CI 必须禁止出网，否则会因网络抖动随机失败，
    最终团队开始忽略失败 —— 这是测试体系崩溃的开始。
    强制点在工厂里，业务层无法绕过。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.base import PlatformAdapter, PlatformClient, ShopContext
from core.constants import Platform
from core.exceptions import ConfigurationError, NotFoundError

if TYPE_CHECKING:
    pass

__all__ = [
    "register",
    "get_adapter_class",
    "registered_platforms",
    "build_adapter",
    "is_registered",
]

#: 平台名 → 适配器类
_ADAPTERS: dict[str, type[PlatformAdapter]] = {}

#: 内置适配器是否已加载。
#:
#: 必须用独立标志，**不能**用 `if _ADAPTERS: return` 判断 ——
#: 因为 mock 模块可能在 registry 之前被 import，此时 _ADAPTERS 已非空，
#: 会导致 Amazon 等内置适配器永远不被注册（这个 bug 已踩过一次）。
_builtins_loaded = False

#: 强制使用 Mock 的环境（不允许出网）
_MOCK_FORCED_ENVS: frozenset[str] = frozenset({"local", "test"})


def register(platform: str | Platform):
    """注册适配器类的装饰器。

    用法：
        @register(Platform.AMAZON)
        class AmazonAdapter(PlatformAdapter):
            ...
    """

    def wrapper(cls: type[PlatformAdapter]) -> type[PlatformAdapter]:
        key = platform.value if isinstance(platform, Platform) else platform
        if key in _ADAPTERS and _ADAPTERS[key] is not cls:
            raise ConfigurationError(
                f"平台 {key} 已被 {_ADAPTERS[key].__name__} 注册，"
                f"不能重复注册为 {cls.__name__}",
                code="ADAPTER_DUPLICATE_REGISTRATION",
            )
        cls.platform = key
        _ADAPTERS[key] = cls
        return cls

    return wrapper


def get_adapter_class(platform: str) -> type[PlatformAdapter]:
    """按平台名取适配器类。

    Raises:
        NotFoundError: 平台未注册。
    """
    _ensure_builtin_registered()
    key = platform.upper()
    cls = _ADAPTERS.get(key)
    if cls is None:
        raise NotFoundError(
            f"平台 {platform} 未注册适配器",
            resource="adapter",
            resource_id=platform,
        )
    return cls


def registered_platforms() -> list[str]:
    """已注册的平台列表。"""
    _ensure_builtin_registered()
    return sorted(_ADAPTERS)


def is_registered(platform: str) -> bool:
    _ensure_builtin_registered()
    return platform.upper() in _ADAPTERS


async def build_adapter(
    *,
    shop: ShopContext,
    env: str,
    client: PlatformClient | None = None,
) -> PlatformAdapter:
    """构建适配器实例。

    环境规则：
        local / test  → **强制 MockAdapter**（无论 shop.platform 是什么）
        sandbox       → 真实适配器 + 沙箱 Base URL
        staging / prod → 真实适配器

    Args:
        shop: 店铺上下文。
        env: 运行环境名。
        client: HTTP 客户端。local/test 环境可为 None（Mock 不需要）。

    Raises:
        ConfigurationError: 生产环境缺少 client。
    """
    if env in _MOCK_FORCED_ENVS:
        from adapters.mock.adapter import MockAdapter

        return MockAdapter(shop=shop, client=client or _NullClient())

    if client is None:
        raise ConfigurationError(
            f"{env} 环境必须提供真实的 HTTP 客户端",
            code="CLIENT_REQUIRED",
            action="检查凭据是否已加载，以及适配器工厂的调用方式",
        )

    adapter_cls = get_adapter_class(shop.platform)
    return adapter_cls(shop=shop, client=client)


class _NullClient:
    """空客户端。Mock 适配器不需要真实网络，但仍需满足构造签名。

    所有方法都抛错 —— 如果 Mock 意外发起了网络调用，会立即暴露，
    而不是静默地走真实网络（那会让 CI 变得不可靠）。
    """

    async def request(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise ConfigurationError(
            "Mock 适配器不应发起网络请求 —— 说明有代码绕过了 Mock 分支",
            code="MOCK_NETWORK_ATTEMPT",
        )

    async def download(self, url: str) -> bytes:
        raise ConfigurationError(
            f"Mock 适配器不应下载文件：{url}",
            code="MOCK_NETWORK_ATTEMPT",
        )

    async def upload(self, url: str, content: bytes) -> None:
        raise ConfigurationError(
            f"Mock 适配器不应上传文件：{url}",
            code="MOCK_NETWORK_ATTEMPT",
        )


def _ensure_builtin_registered() -> None:
    """延迟导入内置适配器。

    为什么延迟：mock/adapter.py 需要 import registry.register，
    如果在模块顶层互相导入会形成循环。
    """
    global _builtins_loaded  # noqa: PLW0603
    if _builtins_loaded:
        return
    _builtins_loaded = True

    # noqa: F401 —— 导入即触发 @register 装饰器
    from adapters.mock import adapter as _mock_adapter  # noqa: F401

    try:
        from adapters.amazon import adapter as _amazon_adapter  # noqa: F401
    except ImportError:
        # Amazon 适配器是 Phase 1 骨架，允许缺失（不影响 Mock 链路）
        pass
