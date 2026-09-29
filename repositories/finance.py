"""财务仓储：汇率、会计期间、成本、结算、利润快照。

**财务口径的三条硬规则**（TDD-05 §2）：

1. **退款按 `settle_time` 归属**，不按下单时间 ——
   否则跨月退款会算进上个月的收入，关账后再发现就得重开期间。
2. **汇率优先用结算汇率**，没有才用记账汇率。
   用错汇率会让利润凭空多出或少掉几个点，且很难被发现。
3. **关账期间禁止写入**（`PeriodClosedError`）。
   校验放在 service 层，但仓储必须能高效回答"这个期间关了吗"。
"""

from __future__ import annotations

from datetime import datetime

from core.models import (
    AccountingPeriod,
    CostItem,
    CostRule,
    ExchangeRate,
    ProfitSnapshot,
    SettlementFee,
    SettlementRecord,
)
from repositories.base import BaseRepository, SyncBaseRepository

__all__ = [
    "AccountingPeriodRepository",
    "CostItemRepository",
    "CostRuleRepository",
    "ExchangeRateRepository",
    "ProfitSnapshotRepository",
    "SettlementFeeRepository",
    "SettlementRecordRepository",
]

#: 结算对账的终态。
#:
#: 模型层未为 `reconcile_status` 定义枚举（见 `core/models/finance.py`），
#: 取值是 PENDING / MATCHED / DISCREPANCY。
#: 这里显式命名，避免在查询里散落裸字符串。
SETTLEMENT_RECONCILED = "MATCHED"


class ExchangeRateRepository(BaseRepository[ExchangeRate]):
    """汇率仓储。"""

    model = ExchangeRate

    async def list_by_pair(self, from_currency: str, to_currency: str) -> list[ExchangeRate]:
        """取某货币对的全部汇率记录（按时间倒序，调用方取最近一条）。"""
        stmt = (
            self._stmt()
            .where(ExchangeRate.from_currency == from_currency)
            .where(ExchangeRate.to_currency == to_currency)
        )
        stmt = self._apply_ordering(stmt, "-created_at")
        return list((await self.session.execute(stmt)).scalars().all())


class AccountingPeriodRepository(BaseRepository[AccountingPeriod]):
    """会计期间仓储。"""

    model = AccountingPeriod

    async def get_by_period(self, period: str, *, shop_id: int | None = None) -> AccountingPeriod | None:
        """按期间查（如 `2026-09`）。

        `shop_id` 为 None 时查全局期间；指定时查店铺期间。
        """
        stmt = self._stmt().where(AccountingPeriod.period == period)
        if shop_id is None:
            stmt = stmt.where(AccountingPeriod.shop_id.is_(None))
        else:
            stmt = stmt.where(AccountingPeriod.shop_id == shop_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_closed(self, *, shop_id: int | None = None) -> list[AccountingPeriod]:
        """已关账期间列表。"""
        from core.constants import PeriodStatus

        stmt = self._stmt().where(AccountingPeriod.status == PeriodStatus.CLOSED.value)
        if shop_id is not None:
            stmt = stmt.where(AccountingPeriod.shop_id == shop_id)
        return list((await self.session.execute(stmt)).scalars().all())


class CostItemRepository(BaseRepository[CostItem]):
    """成本项仓储（L4 数据配置）。"""

    model = CostItem

    async def list_by_period(self, period: str, *, limit: int | None = None) -> list[CostItem]:
        stmt = self._stmt().where(CostItem.period == period)
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class CostRuleRepository(BaseRepository[CostRule]):
    """成本规则仓储。"""

    model = CostRule

    async def get_by_code(self, rule_code: str) -> CostRule | None:
        stmt = self._stmt().where(CostRule.rule_code == rule_code)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_active(self) -> list[CostRule]:
        stmt = self._stmt().where(CostRule.is_active.is_(True))
        return list((await self.session.execute(stmt)).scalars().all())


class SettlementRecordRepository(BaseRepository[SettlementRecord]):
    """结算记录仓储（对账的基础）。"""

    model = SettlementRecord

    async def list_by_shop_period(
        self,
        shop_id: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[SettlementRecord]:
        """按店铺与结算期间查询。"""
        stmt = self._stmt().where(SettlementRecord.shop_id == shop_id)
        if start is not None:
            stmt = stmt.where(SettlementRecord.period_start >= start)
        if end is not None:
            stmt = stmt.where(SettlementRecord.period_end <= end)
        stmt = self._apply_ordering(stmt, "-period_start")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_unreconciled(self, *, shop_id: int | None = None) -> list[SettlementRecord]:
        """未完成对账的结算记录。

        对账未完成的记录不能进利润报表 ——
        否则报表会随对账进度反复变化，没人敢用。
        """
        stmt = self._stmt().where(
            SettlementRecord.reconcile_status != SETTLEMENT_RECONCILED
        )
        if shop_id is not None:
            stmt = stmt.where(SettlementRecord.shop_id == shop_id)
        return list((await self.session.execute(stmt)).scalars().all())


class SettlementFeeRepository(BaseRepository[SettlementFee]):
    """结算费用明细仓储。"""

    model = SettlementFee

    async def list_by_shop(self, shop_id: int, *, limit: int | None = None) -> list[SettlementFee]:
        stmt = self._stmt().where(SettlementFee.shop_id == shop_id)
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_order(self, platform_order_id: str) -> list[SettlementFee]:
        """按平台订单号查费用（订单级利润核算用）。"""
        stmt = self._stmt().where(SettlementFee.platform_order_id == platform_order_id)
        return list((await self.session.execute(stmt)).scalars().all())


class ProfitSnapshotRepository(BaseRepository[ProfitSnapshot]):
    """利润快照仓储。"""

    model = ProfitSnapshot

    async def list_by_period(self, period_key: str, *, limit: int | None = None) -> list[ProfitSnapshot]:
        stmt = self._stmt().where(ProfitSnapshot.period_key == period_key)
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_sku(
        self, shop_id: int, period_key: str, platform_sku: str
    ) -> ProfitSnapshot | None:
        """取某店铺某期间某 SKU 的利润快照。"""
        stmt = (
            self._stmt()
            .where(ProfitSnapshot.shop_id == shop_id)
            .where(ProfitSnapshot.period_key == period_key)
            .where(ProfitSnapshot.platform_sku == platform_sku)
        )
        return (await self.session.execute(stmt)).scalars().first()


# ============================================================
# 同步版本（Celery worker 用）
# ============================================================


class SyncSettlementRecordRepository(SyncBaseRepository[SettlementRecord]):
    model = SettlementRecord

    def list_unreconciled(self, *, shop_id: int | None = None) -> list[SettlementRecord]:
        stmt = self._stmt().where(
            SettlementRecord.reconcile_status != SETTLEMENT_RECONCILED
        )
        if shop_id is not None:
            stmt = stmt.where(SettlementRecord.shop_id == shop_id)
        return list(self.session.execute(stmt).scalars().all())


class SyncExchangeRateRepository(SyncBaseRepository[ExchangeRate]):
    model = ExchangeRate

    def list_by_pair(self, from_currency: str, to_currency: str) -> list[ExchangeRate]:
        stmt = (
            self._stmt()
            .where(ExchangeRate.from_currency == from_currency)
            .where(ExchangeRate.to_currency == to_currency)
        )
        stmt = self._apply_ordering(stmt, "-created_at")
        return list(self.session.execute(stmt).scalars().all())


class SyncAccountingPeriodRepository(SyncBaseRepository[AccountingPeriod]):
    model = AccountingPeriod

    def get_by_period(self, period: str, *, shop_id: int | None = None) -> AccountingPeriod | None:
        stmt = self._stmt().where(AccountingPeriod.period == period)
        if shop_id is None:
            stmt = stmt.where(AccountingPeriod.shop_id.is_(None))
        else:
            stmt = stmt.where(AccountingPeriod.shop_id == shop_id)
        return self.session.execute(stmt).scalars().first()
