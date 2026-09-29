"""环境健康检查。

用途：
    1. 部署后自检
    2. 排障第一步（"连不上"还是"配置错"还是"版本不够"）
    3. CI 门禁（MySQL 版本必须 ≥ 8.0.16，否则 CHECK 约束静默失效）

用法：
    python ops/healthcheck.py
    python ops/healthcheck.py --quiet    # 只输出结论，供 CI 判断退出码

退出码：
    0 = 全部通过（可能含警告）
    1 = 存在致命问题
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许直接以脚本方式运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from core.config import get_settings  # noqa: E402
from core.db import (  # noqa: E402
    check_database_capabilities,
    dispose_engines,
    get_async_engine,
)

OK = "[ OK ]"
WARN = "[WARN]"
FAIL = "[FAIL]"


def _fix_windows_event_loop() -> None:
    """Windows 上 aiomysql 需要 SelectorEventLoop。

    ProactorEventLoop（Windows 默认）不支持 aiomysql 依赖的
    socket 操作，会抛 NotImplementedError。
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]


async def check_database() -> tuple[bool, list[str]]:
    """检查数据库连通性与能力。"""
    settings = get_settings()
    problems: list[str] = []

    print(f"\n{'=' * 62}")
    print("数据库")
    print("=" * 62)
    print(f"  目标      : {settings.database_url_safe}")

    try:
        engine = get_async_engine()
        async with AsyncSession(engine) as session:
            info = await check_database_capabilities(session)
    except Exception as exc:  # noqa: BLE001
        print(f"  {FAIL} 连接失败：{type(exc).__name__}: {exc}")
        return False, [f"数据库连接失败：{exc}"]

    print(f"  版本      : {info['version']}")
    print(f"  字符集    : {info['charset']} / {info['collation']}")
    print(f"  连接时区  : {info['time_zone']}")
    print(f"  隔离级别  : {info['isolation']}")
    print(f"  sql_mode  : {info['sql_mode']}")

    if info["supports_check"]:
        check_line = "是（原生 CHECK 约束生效）"
    else:
        min_ver = info["min_version_for_check"]
        check_line = f"否（需 >= {min_ver}，已降级为触发器实现）"
    print(f"  CHECK 支持: {check_line}")

    for w in info["warnings"]:
        print(f"  {WARN} {w}")
        problems.append(w)

    if not problems:
        print(f"  {OK} 数据库检查通过")

    # 致命项：连不上、字符集错误
    fatal = info["charset"] != "utf8mb4"
    return not fatal, problems


async def check_tables() -> tuple[bool, list[str]]:
    """检查核心表是否已创建。"""
    print(f"\n{'=' * 62}")
    print("表结构")
    print("=" * 62)

    engine = get_async_engine()
    try:
        async with AsyncSession(engine) as session:
            result = await session.execute(
                text(
                    "SELECT TABLE_NAME AS t FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' "
                    "ORDER BY TABLE_NAME"
                )
            )
            tables = [r[0] for r in result]
    except Exception as exc:  # noqa: BLE001
        print(f"  {FAIL} 查询失败：{exc}")
        return False, [f"表结构查询失败：{exc}"]

    if not tables:
        print(f"  {WARN} 尚无任何表 —— 请执行：python -m alembic upgrade head")
        return True, ["数据库为空，待执行迁移"]

    print(f"  已创建 {len(tables)} 张表")
    preview = ", ".join(tables[:8])
    print(f"  {preview}{' ...' if len(tables) > 8 else ''}")
    print(f"  {OK} 表结构检查通过")
    return True, []


async def check_redis() -> tuple[bool, list[str]]:
    """检查 Redis 连通性。"""
    print(f"\n{'=' * 62}")
    print("Redis")
    print("=" * 62)

    settings = get_settings()
    print(f"  目标      : {settings.redis_host}:{settings.redis_port}/{settings.redis_db}")

    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        try:
            pong = await client.ping()
            info = await client.info("server")
            print(f"  版本      : {info.get('redis_version', 'unknown')}")
            print(f"  PING      : {'PONG' if pong else 'NO RESPONSE'}")
            print(f"  {OK} Redis 检查通过")
            return True, []
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        print(f"  {WARN} 不可用：{type(exc).__name__}: {exc}")
        print("        （Phase 1 骨架阶段可暂时忽略；任务队列需要它）")
        return True, [f"Redis 不可用：{exc}"]


async def main() -> int:
    parser = argparse.ArgumentParser(description="EC-AutoFlow 环境健康检查")
    parser.add_argument("--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args()

    _fix_windows_event_loop()

    if args.quiet:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            results = [
                await check_database(),
                await check_tables(),
                await check_redis(),
            ]
    else:
        print("\nEC-AutoFlow 环境健康检查")
        results = [
            await check_database(),
            await check_tables(),
            await check_redis(),
        ]

    await dispose_engines()

    all_ok = all(ok for ok, _ in results)
    issues = [msg for _, msgs in results for msg in msgs]

    if not args.quiet:
        print(f"\n{'=' * 62}")
        print("结论")
        print("=" * 62)
        if all_ok and not issues:
            print(f"  {OK} 环境就绪")
        elif all_ok:
            print(f"  {WARN} 可用，但有 {len(issues)} 条提示：")
            for msg in issues:
                print(f"        - {msg}")
        else:
            print(f"  {FAIL} 存在致命问题，请先修复")
        print()

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
