"""基础设施仓储：配置、同步游标、任务运行、原始报文、告警。

**同步游标的前移规则**（TDD-04 §4）：

    游标**只在整批全部成功时前移**。
    每页都写游标会导致中途失败时丢数据 ——
    而且丢的是"中间一段"，事后完全无法察觉，只能靠水位检查发现。

    连续失败达阈值自动转 `PAUSED`：
    Token 失效这类问题重试 100 次也没用，只会刷爆日志和告警，
    最终让团队关掉通知 —— 那时真正的 P0 也收不到了。
"""

from __future__ import annotations

from datetime import datetime

from core.constants import SyncStatus, TaskStatus
from core.models import (
    Alert,
    AlertRule,
    ConfigItem,
    RawPayload,
    SyncCursor,
    SyncWatermark,
    TaskRun,
)
from core.timeutil import utc_now
from repositories.base import BaseRepository, SyncBaseRepository

__all__ = [
    "AlertRepository",
    "AlertRuleRepository",
    "ConfigItemRepository",
    "RawPayloadRepository",
    "SyncCursorRepository",
    "SyncWatermarkRepository",
    "TaskRunRepository",
]

#: 同步游标连续失败达此值自动暂停。
SYNC_FAILURE_PAUSE_THRESHOLD = 5


class ConfigItemRepository(BaseRepository[ConfigItem]):
    """业务配置仓储（L3 层，改完实时生效，不需要重启）。"""

    model = ConfigItem

    async def get_by_key(self, namespace: str, config_key: str) -> ConfigItem | None:
        """按命名空间 + 键取值。

        命名空间是隔离手段：不同模块可以有自己的 `timeout`，
        不会互相覆盖。
        """
        stmt = (
            self._stmt()
            .where(ConfigItem.namespace == namespace)
            .where(ConfigItem.config_key == config_key)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_by_namespace(self, namespace: str) -> list[ConfigItem]:
        stmt = self._stmt().where(ConfigItem.namespace == namespace)
        return list((await self.session.execute(stmt)).scalars().all())


class AlertRuleRepository(BaseRepository[AlertRule]):
    """预警规则仓储。"""

    model = AlertRule

    async def get_by_code(self, rule_code: str) -> AlertRule | None:
        stmt = self._stmt().where(AlertRule.rule_code == rule_code)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_active(self) -> list[AlertRule]:
        stmt = self._stmt().where(AlertRule.is_active.is_(True))
        return list((await self.session.execute(stmt)).scalars().all())


class AlertRepository(BaseRepository[Alert]):
    """预警实例仓储。"""

    model = Alert

    async def list_open(self, *, limit: int | None = None) -> list[Alert]:
        """未处理的预警（按级别与时间排序，P0 优先）。"""
        from core.constants import AlertStatus

        stmt = self._stmt().where(Alert.status == AlertStatus.OPEN.value)
        stmt = self._apply_ordering(stmt, ["level", "-created_at"])
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class RawPayloadRepository(BaseRepository[RawPayload]):
    """平台原始报文仓储。

    报文里可能含 PII，读取与展示必须经脱敏；
    本模块只负责存取，不负责判断哪些字段敏感。
    """

    model = RawPayload

    async def list_by_shop(self, shop_id: int, *, limit: int | None = None) -> list[RawPayload]:
        stmt = self._stmt().where(RawPayload.shop_id == shop_id)
        stmt = self._apply_ordering(stmt, "-created_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class TaskRunRepository(BaseRepository[TaskRun]):
    """任务运行记录仓储。"""

    model = TaskRun

    async def list_recent(self, task_name: str, *, limit: int | None = None) -> list[TaskRun]:
        stmt = self._stmt().where(TaskRun.task_name == task_name)
        stmt = self._apply_ordering(stmt, "-created_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_failed(self, *, limit: int | None = None) -> list[TaskRun]:
        stmt = self._stmt().where(TaskRun.status == TaskStatus.FAILED.value)
        stmt = self._apply_ordering(stmt, "-created_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class SyncCursorRepository(BaseRepository[SyncCursor]):
    """同步游标仓储。"""

    model = SyncCursor

    async def get_cursor(self, shop_id: int, resource: str) -> SyncCursor | None:
        stmt = (
            self._stmt().where(SyncCursor.shop_id == shop_id).where(SyncCursor.resource == resource)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def advance(
        self, shop_id: int, resource: str, cursor_value: str, *, cursor_type: str = "TIMESTAMP"
    ) -> SyncCursor:
        """整批成功后前移游标。

        **只在整批成功时调用** —— 每页都前移会在中途失败时丢数据。
        """
        cursor = await self.get_cursor(shop_id, resource)
        if cursor is None:
            cursor = SyncCursor(
                shop_id=shop_id,
                resource=resource,
                cursor_type=cursor_type,
                cursor_value=cursor_value,
                status=SyncStatus.IDLE.value,
            )
            await self.add(cursor)
        else:
            cursor.cursor_value = cursor_value
            cursor.last_success_at = utc_now()
            cursor.consecutive_failures = 0
            cursor.status = SyncStatus.IDLE.value
            cursor.error_msg = None
            await self.session.flush()
        return cursor

    async def record_failure(self, shop_id: int, resource: str, error: str) -> SyncCursor:
        """记录一次失败；连续失败达阈值自动暂停。

        自动暂停的理由：Token 失效、权限被撤销这类问题重试不会成功，
        无限重试只会掩盖真正需要人工处理的问题。
        """
        cursor = await self.get_cursor(shop_id, resource)
        if cursor is None:
            cursor = SyncCursor(
                shop_id=shop_id,
                resource=resource,
                status=SyncStatus.ERROR.value,
                consecutive_failures=1,
                error_msg=(error or "")[:1000],
            )
            await self.add(cursor)
            return cursor

        cursor.consecutive_failures += 1
        cursor.last_attempt_at = utc_now()
        cursor.error_msg = (error or "")[:1000]
        cursor.status = (
            SyncStatus.PAUSED.value
            if cursor.consecutive_failures >= SYNC_FAILURE_PAUSE_THRESHOLD
            else SyncStatus.ERROR.value
        )
        await self.session.flush()
        return cursor

    async def list_paused(self) -> list[SyncCursor]:
        """已暂停的同步（需要人工介入）。"""
        stmt = self._stmt().where(SyncCursor.status == SyncStatus.PAUSED.value)
        return list((await self.session.execute(stmt)).scalars().all())


class SyncWatermarkRepository(BaseRepository[SyncWatermark]):
    """同步水位仓储（数据新鲜度与完整性）。"""

    model = SyncWatermark

    async def get_watermark(self, shop_id: int, resource: str) -> SyncWatermark | None:
        stmt = (
            self._stmt()
            .where(SyncWatermark.shop_id == shop_id)
            .where(SyncWatermark.resource == resource)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def update_watermark(
        self,
        shop_id: int,
        resource: str,
        *,
        data_max_time: datetime | None = None,
        fresh_lag_minutes: int | None = None,
        record_count: int | None = None,
    ) -> SyncWatermark:
        """更新水位。

        `fresh_lag_minutes` 是最重要的可观测指标 ——
        数据异常排查的第一步永远是看它。
        """
        watermark = await self.get_watermark(shop_id, resource)
        if watermark is None:
            watermark = SyncWatermark(
                shop_id=shop_id,
                resource=resource,
                last_incremental_at=utc_now(),
                data_max_time=data_max_time,
                fresh_lag_minutes=fresh_lag_minutes,
                record_count=record_count,
            )
            await self.add(watermark)
        else:
            watermark.last_incremental_at = utc_now()
            if data_max_time is not None:
                watermark.data_max_time = data_max_time
            if fresh_lag_minutes is not None:
                watermark.fresh_lag_minutes = fresh_lag_minutes
            if record_count is not None:
                watermark.record_count = record_count
            await self.session.flush()
        return watermark


# ============================================================
# 同步版本（Celery worker 用）
# ============================================================


class SyncSyncCursorRepository(SyncBaseRepository[SyncCursor]):
    model = SyncCursor

    def get_cursor(self, shop_id: int, resource: str) -> SyncCursor | None:
        stmt = (
            self._stmt().where(SyncCursor.shop_id == shop_id).where(SyncCursor.resource == resource)
        )
        return self.session.execute(stmt).scalars().first()

    def advance(
        self, shop_id: int, resource: str, cursor_value: str, *, cursor_type: str = "TIMESTAMP"
    ) -> SyncCursor:
        cursor = self.get_cursor(shop_id, resource)
        if cursor is None:
            cursor = SyncCursor(
                shop_id=shop_id,
                resource=resource,
                cursor_type=cursor_type,
                cursor_value=cursor_value,
                status=SyncStatus.IDLE.value,
            )
            self.add(cursor)
        else:
            cursor.cursor_value = cursor_value
            cursor.last_success_at = utc_now()
            cursor.consecutive_failures = 0
            cursor.status = SyncStatus.IDLE.value
            cursor.error_msg = None
            self.session.flush()
        return cursor

    def record_failure(self, shop_id: int, resource: str, error: str) -> SyncCursor:
        cursor = self.get_cursor(shop_id, resource)
        if cursor is None:
            cursor = SyncCursor(
                shop_id=shop_id,
                resource=resource,
                status=SyncStatus.ERROR.value,
                consecutive_failures=1,
                error_msg=(error or "")[:1000],
            )
            self.add(cursor)
            return cursor

        cursor.consecutive_failures += 1
        cursor.last_attempt_at = utc_now()
        cursor.error_msg = (error or "")[:1000]
        cursor.status = (
            SyncStatus.PAUSED.value
            if cursor.consecutive_failures >= SYNC_FAILURE_PAUSE_THRESHOLD
            else SyncStatus.ERROR.value
        )
        self.session.flush()
        return cursor

    def list_paused(self) -> list[SyncCursor]:
        """已暂停的同步（需要人工介入）。"""
        stmt = self._stmt().where(SyncCursor.status == SyncStatus.PAUSED.value)
        return list(self.session.execute(stmt).scalars().all())


class SyncSyncWatermarkRepository(SyncBaseRepository[SyncWatermark]):
    model = SyncWatermark

    def get_watermark(self, shop_id: int, resource: str) -> SyncWatermark | None:
        stmt = (
            self._stmt()
            .where(SyncWatermark.shop_id == shop_id)
            .where(SyncWatermark.resource == resource)
        )
        return self.session.execute(stmt).scalars().first()
