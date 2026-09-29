"""测试全局配置。

**Windows 上的 asyncio 事件循环陷阱**：

    默认的 `ProactorEventLoop` 不支持 aiomysql 依赖的 socket 操作，
    会抛 `NotImplementedError`。必须切到 `SelectorEventLoop`。

    这个坑不会在 Linux CI 上暴露 —— 只会在 Windows 本地开发时出现，
    而且报错信息与"连不上数据库"很像，容易误判为配置问题。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_async_engine_between_tests():
    """每个测试前后重置全局异步引擎。

    `get_async_engine()` 是**全局单例**，绑定在创建它时的事件循环上。
    多个测试各自 `asyncio.run()` 时，后一个测试会拿到属于已关闭循环的
    连接池，报 `RuntimeError: Event loop is closed` ——
    这个错误与"连不上数据库"极像，很容易误判为配置问题。

    直接置空全局引用即可：下次 `get_async_engine()` 会用当前循环重建。
    这是测试专用处理，生产代码不需要（一个进程只有一个事件循环）。
    """
    import core.db as db_module

    db_module._async_engine = None
    db_module._async_session_factory = None
    yield
    db_module._async_engine = None
    db_module._async_session_factory = None
