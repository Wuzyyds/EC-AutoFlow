"""时间处理工具。

核心约定（ADR-007 §3.3）：
    1. 数据库时间列一律 `DATETIME(6)`，**存 naive UTC**
    2. 应用层内部传递一律 **aware UTC**（带 tzinfo）
    3. 展示层按店铺时区转换

为什么不用 MySQL 的 TIMESTAMP：
    它会随 session time_zone 自动转换，导致同一行在不同连接下读出不同时间，
    且上限是 2038 年。DATETIME 原样存储，语义确定。

为什么禁止 datetime.now()：
    它返回 naive 本地时间。一旦有人用它写库，库里就混入了非 UTC 的时间，
    而且**没有任何报错**——三个月后报表对不上时才会发现。
    因此本模块提供唯一入口 utc_now()，并用 `to_db()` 在写库前做防御性校验。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "utc_now",
    "to_db",
    "from_db",
    "to_shop_timezone",
    "shop_today",
    "parse_iso",
    "iso_z",
    "day_bounds_utc",
    "humanize_delta",
    "TimeoutClock",
]

#: 兜底时区。店铺未配置时区时使用，避免抛错导致同步中断。
DEFAULT_TZ = "UTC"


def utc_now() -> datetime:
    """获取当前 UTC 时间（aware）。

    这是**全系统唯一**允许获取"当前时间"的入口。
    禁止直接调用 `datetime.now()` 或 `datetime.utcnow()`：
    - `datetime.now()` 返回本地 naive 时间
    - `datetime.utcnow()` 返回 UTC 但 naive，且已废弃
    """
    return datetime.now(UTC)


def to_db(dt: datetime) -> datetime:
    """写库前转换：aware UTC → naive UTC。

    防御性校验：传入 naive datetime 直接报错。
    这是刻意的 —— 宁可写不进去，也不要静默写入含义不明的时间。

    Raises:
        ValueError: 传入 naive datetime。
    """
    if dt.tzinfo is None:
        raise ValueError(
            "禁止写入 naive datetime。请使用 core.timeutil.utc_now() "
            "或显式指定时区（如 datetime.now(ZoneInfo('Asia/Shanghai'))）。"
        )
    return dt.astimezone(UTC).replace(tzinfo=None)


def from_db(dt: datetime | None) -> datetime | None:
    """读库后转换：naive UTC → aware UTC。

    若驱动已返回 aware 值（部分驱动会），则原样规整到 UTC。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _resolve_tz(tz_name: str | None) -> ZoneInfo:
    """解析时区名，失败时回退到 UTC 并保持沉默（不阻断业务）。"""
    if not tz_name:
        return ZoneInfo(DEFAULT_TZ)
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def to_shop_timezone(dt: datetime, tz_name: str | None) -> datetime:
    """把 UTC 时间转成店铺本地时间，用于展示。

    Args:
        dt: aware 或 naive（按 UTC 解释）时间。
        tz_name: IANA 时区名，如 "America/Los_Angeles"、"Asia/Shanghai"。
    """
    aware = from_db(dt) if dt.tzinfo is None else dt.astimezone(UTC)
    assert aware is not None
    return aware.astimezone(_resolve_tz(tz_name))


def shop_today(tz_name: str | None) -> date:
    """店铺本地时区的"今天"。

    报表必须用这个而不是 UTC 的今天 ——
    否则美国店铺的"昨日报表"会把当天下午的数据算进去。
    """
    return to_shop_timezone(utc_now(), tz_name).date()


def day_bounds_utc(day: date, tz_name: str | None) -> tuple[datetime, datetime]:
    """把"店铺本地某一天"转成 UTC 的 [start, end) 区间。

    报表按店铺自然日统计时使用。返回 aware UTC，可直接写库比较。
    """
    tz = _resolve_tz(tz_name)
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def parse_iso(value: str) -> datetime:
    """解析 ISO 8601 字符串。

    - 带时区偏移的（`2026-09-29T10:00:00+08:00`）→ 转 UTC
    - 不带时区的（`2026-09-29T10:00:00`）→ **按 UTC 解释**（不是本地时间）

    第二条是关键：平台 API 返回的时间常不带时区，
    若按本地时间解释，会引入固定偏移的错误。
    """
    text = value.strip()
    # 兼容结尾的 Z
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_z(dt: datetime) -> str:
    """序列化为带 Z 的 ISO 8601（API 响应用）。"""
    aware = from_db(dt) if dt.tzinfo is None else dt.astimezone(UTC)
    assert aware is not None
    return aware.isoformat().replace("+00:00", "Z")


def humanize_delta(delta: timedelta) -> str:
    """把时长转成人类可读形式（日志与告警用）。"""
    total = int(delta.total_seconds())
    if total < 0:
        return f"-{humanize_delta(-delta)}"
    if total < 60:
        return f"{total}秒"
    if total < 3600:
        return f"{total // 60}分{total % 60}秒"
    if total < 86400:
        return f"{total // 3600}小时{(total % 3600) // 60}分"
    return f"{total // 86400}天{(total % 86400) // 3600}小时"


class TimeoutClock:
    """超时判定器（轮询场景）。

    为什么不用 `while time.time() < deadline`：
        直接读系统时间在测试中不可注入，无法验证"轮询超时"分支。
        这个类允许传入自定义的 now 函数，测试可完全掌控时间推进。

    用法：
        clock = TimeoutClock(timeout=timedelta(minutes=30))
        while not clock.expired():
            status = await adapter.poll_submission(...)
            if status.done:
                break
            await clock.sleep_interval()
    """

    __slots__ = ("_now", "_start", "_timeout", "_interval")

    def __init__(
        self,
        timeout: timedelta,
        interval: timedelta = timedelta(seconds=30),
        *,
        now_fn: object = None,
    ) -> None:
        self._now = now_fn or utc_now
        self._start = self._now()  # type: ignore[operator]
        self._timeout = timeout
        self._interval = interval

    def elapsed(self) -> timedelta:
        return self._now() - self._start  # type: ignore[operator]

    def remaining(self) -> timedelta:
        return max(self._timeout - self.elapsed(), timedelta(0))

    def expired(self) -> bool:
        return self.elapsed() >= self._timeout

    @property
    def interval(self) -> timedelta:
        return self._interval

    def next_interval(self) -> timedelta:
        """下一次轮询间隔，不超过剩余时间。

        避免"还剩 5 秒却 sleep 30 秒"导致超时判定滞后。
        """
        return min(self._interval, self.remaining())
