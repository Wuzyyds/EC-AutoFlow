"""商品、刊登与 SKU 映射仓储。

**SKU 主映射的唯一性**（ADR-007 §3.7）：
    PostgreSQL 用排除约束表达"同一店铺同一 SKU 只能有一个主映射"，
    MySQL 不支持，改用**生成列 + 唯一索引**实现：
    非主映射生成 NULL（多个 NULL 不冲突），主映射生成真实 SKU 受唯一保护。

    本模块的 `get_primary` 依赖这一约束 ——
    如果哪天生成列被误删，这里会返回多条，属于数据事故。

**刊登状态**：`partial_active` 是独立状态（TDD-04 §2），
    表示"多变体部分成功、商品在售但不完整"。
    既不能归为 active（会漏监控库存），也不能归为 failed（会被重试覆盖）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select

from core.models import (
    CategorySchema,
    PlatformCategory,
    PriceHistory,
    PricingRule,
    Product,
    ProductListing,
    SKUMapping,
)
from repositories.base import BaseRepository, SyncBaseRepository

__all__ = [
    "CategorySchemaRepository",
    "PlatformCategoryRepository",
    "PriceHistoryRepository",
    "PricingRuleRepository",
    "ProductListingRepository",
    "ProductRepository",
    "SKUMappingRepository",
]


class ProductRepository(BaseRepository[Product]):
    """商品（SPU）仓储。"""

    model = Product

    async def get_by_internal_sku(self, internal_sku: str) -> Product | None:
        """按内部 SKU 查。内部 SKU 是全局唯一标识。"""
        stmt = self._stmt().where(Product.internal_sku == internal_sku)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_by_status(self, status: str, *, limit: int | None = None) -> list[Product]:
        stmt = self._stmt().where(Product.status == status)
        stmt = self._apply_ordering(stmt, "-created_at")
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class ProductListingRepository(BaseRepository[ProductListing]):
    """刊登仓储。"""

    model = ProductListing

    async def get_by_platform_sku(
        self, shop_id: int, platform_sku: str
    ) -> ProductListing | None:
        """按平台 SKU 查刊登（订单回填商品信息用）。"""
        stmt = (
            self._stmt()
            .where(ProductListing.shop_id == shop_id)
            .where(ProductListing.platform_sku == platform_sku)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_by_shop(self, shop_id: int, *, limit: int | None = None) -> list[ProductListing]:
        stmt = self._stmt().where(ProductListing.shop_id == shop_id)
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_stale(self, before: datetime, *, limit: int | None = None) -> list[ProductListing]:
        """超过指定时间未同步的刊登（含从未同步的）。

        只按 `last_synced_at < before` 会漏掉 `NULL` 的行 ——
        而"从未同步过"恰恰是最需要补的。
        """
        stmt = self._stmt().where(
            (ProductListing.last_synced_at.is_(None))
            | (ProductListing.last_synced_at < before)
        )
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class SKUMappingRepository(BaseRepository[SKUMapping]):
    """SKU 映射仓储。"""

    model = SKUMapping

    async def get_primary(self, shop_id: int, internal_sku: str) -> SKUMapping | None:
        """取主映射。

        唯一性由"生成列 + 唯一索引"保证（ADR-007 §3.7），
        因此这里最多返回一条。
        """
        stmt = (
            self._stmt()
            .where(SKUMapping.shop_id == shop_id)
            .where(SKUMapping.internal_sku == internal_sku)
            .where(SKUMapping.is_primary.is_(True))
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_conflicts(self, *, shop_id: int | None = None) -> list[SKUMapping]:
        """冲突映射列表（同一平台 SKU 被多个内部 SKU 占用）。

        冲突不阻断同步，只告警 —— 但必须有人处理，
        否则成本会算到错误的商品上。
        """
        stmt = self._stmt().where(SKUMapping.has_conflict.is_(True))
        if shop_id is not None:
            stmt = stmt.where(SKUMapping.shop_id == shop_id)
        return list((await self.session.execute(stmt)).scalars().all())


class PlatformCategoryRepository(BaseRepository[PlatformCategory]):
    """平台类目仓储（类目树缓存）。"""

    model = PlatformCategory

    async def list_by_platform(
        self, platform: str, region: str, *, leaf_only: bool = False
    ) -> list[PlatformCategory]:
        """列平台类目。`leaf_only=True` 只取叶子类目（可刊登的类目）。"""
        stmt = (
            self._stmt()
            .where(PlatformCategory.platform == platform)
            .where(PlatformCategory.region == region)
        )
        if leaf_only:
            stmt = stmt.where(PlatformCategory.is_leaf.is_(True))
        return list((await self.session.execute(stmt)).scalars().all())


class CategorySchemaRepository(BaseRepository[CategorySchema]):
    """类目属性 schema 仓储（动态表单来源）。"""

    model = CategorySchema

    async def get_latest(
        self, platform: str, region: str, category_id: str
    ) -> CategorySchema | None:
        """取类目最新的 schema。

        按 `fetched_at` 倒序取第一条 ——
        平台会改 schema，旧版本留着做审计但不能用于新刊登。
        """
        stmt = (
            self._stmt()
            .where(CategorySchema.platform == platform)
            .where(CategorySchema.region == region)
            .where(CategorySchema.category_id == category_id)
        )
        stmt = self._apply_ordering(stmt, "-fetched_at").limit(1)
        return (await self.session.execute(stmt)).scalars().first()


class PriceHistoryRepository(BaseRepository[PriceHistory]):
    """价格变更历史仓储（append-only）。"""

    model = PriceHistory

    async def list_by_listing(
        self, listing_id: int, *, limit: int | None = None
    ) -> list[PriceHistory]:
        stmt = self._apply_ordering(
            self._stmt().where(PriceHistory.listing_id == listing_id), "-created_at"
        )
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list((await self.session.execute(stmt)).scalars().all())


class PricingRuleRepository(BaseRepository[PricingRule]):
    """定价规则仓储。"""

    model = PricingRule

    async def get_by_code(self, rule_code: str) -> PricingRule | None:
        stmt = self._stmt().where(PricingRule.rule_code == rule_code)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_active(self) -> list[PricingRule]:
        stmt = self._stmt().where(PricingRule.is_active.is_(True))
        return list((await self.session.execute(stmt)).scalars().all())


# ============================================================
# 同步版本（Celery worker 用）
# ============================================================


class SyncProductRepository(SyncBaseRepository[Product]):
    model = Product

    def get_by_internal_sku(self, internal_sku: str) -> Product | None:
        return (
            self.session.execute(self._stmt().where(Product.internal_sku == internal_sku))
            .scalars()
            .first()
        )


class SyncProductListingRepository(SyncBaseRepository[ProductListing]):
    model = ProductListing

    def get_by_platform_sku(self, shop_id: int, platform_sku: str) -> ProductListing | None:
        stmt = (
            self._stmt()
            .where(ProductListing.shop_id == shop_id)
            .where(ProductListing.platform_sku == platform_sku)
        )
        return self.session.execute(stmt).scalars().first()

    def list_stale(self, before: datetime, *, limit: int | None = None) -> list[ProductListing]:
        stmt = self._stmt().where(
            (ProductListing.last_synced_at.is_(None))
            | (ProductListing.last_synced_at < before)
        )
        stmt = stmt.limit(self._normalize_pagination(limit, 0)[0])
        return list(self.session.execute(stmt).scalars().all())


class SyncSKUMappingRepository(SyncBaseRepository[SKUMapping]):
    model = SKUMapping

    def get_primary(self, shop_id: int, internal_sku: str) -> SKUMapping | None:
        stmt = (
            self._stmt()
            .where(SKUMapping.shop_id == shop_id)
            .where(SKUMapping.internal_sku == internal_sku)
            .where(SKUMapping.is_primary.is_(True))
        )
        return self.session.execute(stmt).scalars().first()

    def list_conflicts(self, *, shop_id: int | None = None) -> list[SKUMapping]:
        stmt = self._stmt().where(SKUMapping.has_conflict.is_(True))
        if shop_id is not None:
            stmt = stmt.where(SKUMapping.shop_id == shop_id)
        return list(self.session.execute(stmt).scalars().all())


class SyncCategorySchemaRepository(SyncBaseRepository[CategorySchema]):
    model = CategorySchema

    def get_latest(self, platform: str, region: str, category_id: str) -> CategorySchema | None:
        stmt = (
            self._stmt()
            .where(CategorySchema.platform == platform)
            .where(CategorySchema.region == region)
            .where(CategorySchema.category_id == category_id)
        )
        stmt = self._apply_ordering(stmt, "-fetched_at").limit(1)
        return self.session.execute(stmt).scalars().first()
