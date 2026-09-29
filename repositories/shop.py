"""店铺与凭据仓储。

凭据的特殊性（TDD-06 §2.2）：

    `shop_credentials` 里存的是**密文**（信封加密）。
    本模块只负责"取密文行"，解密由 `core.security.crypto` 完成，
    且每次解密必须写 `credential_audit_logs`（高危操作留痕）。

    **明文永不落库、永不进日志** —— 本模块的任何方法都不得返回明文，
    也不得把 token 内容拼进异常消息。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select

from core.constants import CredentialStatus, ShopStatus
from core.models import CredentialAuditLog, Shop, ShopCredential
from core.timeutil import utc_now
from repositories.base import BaseRepository, SyncBaseRepository

__all__ = [
    "CredentialAuditLogRepository",
    "ShopCredentialRepository",
    "ShopRepository",
    "SyncCredentialAuditLogRepository",
    "SyncShopCredentialRepository",
    "SyncShopRepository",
]


class _ShopQueries:
    """查询构建（async / sync 共用）。"""

    # ---------- 店铺 ----------

    def _q_shop_by_platform_id(self, platform: str, platform_shop_id: str) -> Select:
        """按平台侧店铺 ID 查。

        同步任务回填数据时用 —— 平台回调里只有平台侧 ID，
        没有我们的内部 ID。
        """
        return (
            self._stmt()
            .where(Shop.platform == platform)
            .where(Shop.platform_shop_id == platform_shop_id)
        )

    def _q_shops_active(self) -> Select:
        return self._stmt().where(Shop.status == ShopStatus.ACTIVE.value)

    def _q_shops_syncable(self) -> Select:
        """可同步店铺：状态活跃 **且** 未暂停同步。

        同步任务每轮都要调这个。必须排除 `sync_paused=True` 的店铺，
        否则用户在界面上点"暂停同步"将形同虚设。
        """
        return (
            self._stmt()
            .where(Shop.status == ShopStatus.ACTIVE.value)
            .where(Shop.sync_paused.is_(False))
        )

    # ---------- 凭据 ----------

    def _q_credential_active(self, shop_id: int, credential_type: str | None) -> Select:
        """有效凭据：状态 ACTIVE 且未被撤销。

        `revoked_at IS NULL` 与 `status=ACTIVE` 两个条件都要 ——
        撤销流程可能先写 revoked_at 再改状态，中间态必须排除。
        """
        stmt = (
            self._stmt()
            .where(ShopCredential.shop_id == shop_id)
            .where(ShopCredential.status == CredentialStatus.ACTIVE.value)
            .where(ShopCredential.revoked_at.is_(None))
        )
        if credential_type:
            stmt = stmt.where(ShopCredential.credential_type == credential_type)
        return stmt

    def _q_credentials_expiring(self, before: datetime) -> Select:
        """到期时间早于 `before` 的有效凭据。

        用于提前告警 —— 等 token 真过期了才处理，
        意味着这期间该店铺的同步任务全部失败。
        """
        return (
            self._stmt()
            .where(ShopCredential.access_token_expires_at.is_not(None))
            .where(ShopCredential.access_token_expires_at <= before)
            .where(ShopCredential.status == CredentialStatus.ACTIVE.value)
            .where(ShopCredential.revoked_at.is_(None))
        )

    def _q_credentials_unhealthy(self, threshold: int) -> Select:
        """连续失败次数达到阈值的凭据。

        连续失败通常意味着授权被平台侧撤销，需要人工重新授权。
        """
        return self._stmt().where(ShopCredential.consecutive_failures >= threshold)


class ShopRepository(BaseRepository[Shop], _ShopQueries):
    """店铺仓储。"""

    model = Shop

    async def get_by_platform_shop_id(self, platform: str, platform_shop_id: str) -> Shop | None:
        stmt = self._q_shop_by_platform_id(platform, platform_shop_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_active(self) -> list[Shop]:
        return list((await self.session.execute(self._q_shops_active())).scalars().all())

    async def list_syncable(self) -> list[Shop]:
        return list((await self.session.execute(self._q_shops_syncable())).scalars().all())

    async def set_sync_paused(
        self, shop_id: int, paused: bool, *, reason: str | None = None
    ) -> Shop:
        """暂停/恢复店铺同步。

        暂停时必须写原因 —— 三个月后没人记得为什么这家店不跑了。
        """
        shop = await self.get_or_raise(shop_id, resource="店铺")
        shop.sync_paused = paused
        shop.sync_paused_reason = reason if paused else None
        await self.session.flush()
        return shop


class ShopCredentialRepository(BaseRepository[ShopCredential], _ShopQueries):
    """店铺凭据仓储（**只碰密文**）。"""

    model = ShopCredential

    async def get_active(
        self, shop_id: int, credential_type: str | None = None
    ) -> ShopCredential | None:
        """取店铺当前有效凭据。"""
        stmt = self._q_credential_active(shop_id, credential_type)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_expiring(self, before: datetime) -> list[ShopCredential]:
        """列出即将过期的凭据。"""
        stmt = self._q_credentials_expiring(before)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_unhealthy(self, *, threshold: int = 3) -> list[ShopCredential]:
        """列出连续失败达阈值的凭据。"""
        stmt = self._q_credentials_unhealthy(threshold)
        return list((await self.session.execute(stmt)).scalars().all())

    async def record_failure(self, credential_id: int, error: str) -> ShopCredential:
        """记录一次调用失败。

        `error` 必须已脱敏 —— 平台返回的报文里可能回显 token 片段。
        这里再做一次长度截断，防止超长文本写库失败。
        """
        credential = await self.get_or_raise(credential_id, resource="凭据")
        credential.consecutive_failures += 1
        credential.last_error = (error or "")[:1000]
        await self.session.flush()
        return credential

    async def record_success(self, credential_id: int) -> ShopCredential:
        """记录一次调用成功，清零失败计数。"""
        credential = await self.get_or_raise(credential_id, resource="凭据")
        credential.consecutive_failures = 0
        credential.last_error = None
        credential.last_verified_at = utc_now()
        await self.session.flush()
        return credential


class CredentialAuditLogRepository(BaseRepository[CredentialAuditLog], _ShopQueries):
    """凭据审计日志仓储。

    **append-only**：只提供写入与查询，不提供 update / delete。
    审计记录可删就失去了审计意义。
    """

    model = CredentialAuditLog

    async def log(
        self,
        *,
        shop_id: int | None,
        credential_id: int | None,
        operation: str,
        actor_type: str,
        result: str,
        actor_id: int | None = None,
        reason: str | None = None,
        client_ip: str | None = None,
        trace_id: str | None = None,
        key_version: int | None = None,
    ) -> CredentialAuditLog:
        """写入一条凭据操作审计。"""
        entry = CredentialAuditLog(
            shop_id=shop_id,
            credential_id=credential_id,
            operation=operation,
            actor_type=actor_type,
            actor_id=actor_id,
            result=result,
            reason=reason,
            client_ip=client_ip,
            trace_id=trace_id,
            key_version=key_version,
        )
        return await self.add(entry)

    async def list_by_shop(
        self, shop_id: int, *, limit: int | None = None
    ) -> list[CredentialAuditLog]:
        stmt = self._apply_ordering(self._stmt(), "-created_at")
        stmt = self._apply_filters(stmt, {"shop_id": shop_id})
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


# ============================================================
# 同步版本（Celery worker 用）
# ============================================================


class SyncShopRepository(SyncBaseRepository[Shop], _ShopQueries):
    model = Shop

    def get_by_platform_shop_id(self, platform: str, platform_shop_id: str) -> Shop | None:
        return (
            self.session.execute(self._q_shop_by_platform_id(platform, platform_shop_id))
            .scalars()
            .first()
        )

    def list_active(self) -> list[Shop]:
        return list(self.session.execute(self._q_shops_active()).scalars().all())

    def list_syncable(self) -> list[Shop]:
        return list(self.session.execute(self._q_shops_syncable()).scalars().all())


class SyncShopCredentialRepository(SyncBaseRepository[ShopCredential], _ShopQueries):
    model = ShopCredential

    def get_active(
        self, shop_id: int, credential_type: str | None = None
    ) -> ShopCredential | None:
        return (
            self.session.execute(self._q_credential_active(shop_id, credential_type))
            .scalars()
            .first()
        )

    def list_expiring(self, before: datetime) -> list[ShopCredential]:
        return list(self.session.execute(self._q_credentials_expiring(before)).scalars().all())

    def list_unhealthy(self, *, threshold: int = 3) -> list[ShopCredential]:
        return list(self.session.execute(self._q_credentials_unhealthy(threshold)).scalars().all())

    def record_failure(self, credential_id: int, error: str) -> ShopCredential:
        credential = self.get_or_raise(credential_id, resource="凭据")
        credential.consecutive_failures += 1
        credential.last_error = (error or "")[:1000]
        self.session.flush()
        return credential


class SyncCredentialAuditLogRepository(SyncBaseRepository[CredentialAuditLog], _ShopQueries):
    model = CredentialAuditLog

    def log(
        self,
        *,
        shop_id: int | None,
        credential_id: int | None,
        operation: str,
        actor_type: str,
        result: str,
        actor_id: int | None = None,
        reason: str | None = None,
        client_ip: str | None = None,
        trace_id: str | None = None,
        key_version: int | None = None,
    ) -> CredentialAuditLog:
        entry = CredentialAuditLog(
            shop_id=shop_id,
            credential_id=credential_id,
            operation=operation,
            actor_type=actor_type,
            actor_id=actor_id,
            result=result,
            reason=reason,
            client_ip=client_ip,
            trace_id=trace_id,
            key_version=key_version,
        )
        return self.add(entry)
