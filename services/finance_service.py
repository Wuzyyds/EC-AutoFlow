"""财务服务。

**三条财务口径硬规则**（TDD-05 §2）：

1. **退款按 `settle_time` 归属**，不按下单时间
   跨月退款若按订单时间归入上月收入，关账后再发现就得重开期间 ——
   而重开期间需要 admin 权限 + 审计，成本远高于一开始就归对。

2. **汇率优先用结算汇率**，没有才回退记账汇率
   用错汇率会让利润凭空多出或少掉几个点，且几乎无法被发现。

3. **关账期间禁止写入**（`PeriodClosedError`）
   校验放在服务层而不是数据库触发器 —— 需要给出"请先 reopen"
   这样的可操作提示，触发器做不到（TDD-05 §212）。

**本服务不替代法定会计**：算的是"履约后 / 广告后贡献利润"，
与会计口径的差异在报表层显式标注。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import PeriodStatus
from core.exceptions import PeriodClosedError
from core.money import money, safe_div
from repositories.finance import AccountingPeriodRepository
from services.base import ServiceContext

__all__ = ["FinanceService", "ProfitBreakdown"]


class ProfitBreakdown:
    """贡献利润分解。

    分层展示而非只给一个净利数字 ——
    这样"利润为什么掉了"能直接定位到是哪一层出的问题。
    """

    __slots__ = (
        "revenue",
        "cogs",
        "platform_fees",
        "shipping",
        "ad_cost",
        "refunds",
    )

    def __init__(
        self,
        *,
        revenue: Decimal = Decimal("0"),
        cogs: Decimal = Decimal("0"),
        platform_fees: Decimal = Decimal("0"),
        shipping: Decimal = Decimal("0"),
        ad_cost: Decimal = Decimal("0"),
        refunds: Decimal = Decimal("0"),
    ) -> None:
        self.revenue = revenue
        self.cogs = cogs
        self.platform_fees = platform_fees
        self.shipping = shipping
        self.ad_cost = ad_cost
        self.refunds = refunds

    @property
    def gross_profit(self) -> Decimal:
        """履约后贡献利润 = 收入 - 成本 - 平台费 - 运费 - 退款。"""
        return money(
            self.revenue - self.cogs - self.platform_fees - self.shipping - self.refunds
        )

    @property
    def contribution_profit(self) -> Decimal:
        """广告后贡献利润 = 履约后 - 广告费。

        这是"这个品到底值不值得做"的判断依据；
        TACOS 分母用净销售额，与广告口径一致（PRD v1.1 修正项）。
        """
        return money(self.gross_profit - self.ad_cost)

    @property
    def gross_margin(self) -> Decimal:
        """履约后毛利率。收入为 0 时返回 0，避免除零。

        用 `safe_div` 而不是裸除 —— 促销期间出现零收入订单是正常的，
        裸除会抛 `InvalidOperation` 让整个报表生成失败。
        """
        return money(safe_div(self.gross_profit, self.revenue) * 100)

    def to_dict(self) -> dict[str, str]:
        return {
            "revenue": str(self.revenue),
            "cogs": str(self.cogs),
            "platform_fees": str(self.platform_fees),
            "shipping": str(self.shipping),
            "ad_cost": str(self.ad_cost),
            "refunds": str(self.refunds),
            "gross_profit": str(self.gross_profit),
            "contribution_profit": str(self.contribution_profit),
            "gross_margin": str(self.gross_margin),
        }


class FinanceService:
    """财务编排服务。"""

    def __init__(self, session: AsyncSession, ctx: ServiceContext) -> None:
        self.session = session
        self.ctx = ctx
        self.periods = AccountingPeriodRepository(session, tenant_id=ctx.tenant_id)

    # ========================================================
    # 关账校验
    # ========================================================

    async def assert_period_open(self, period: str, *, shop_id: int | None = None) -> None:
        """校验会计期间未关账，否则抛 `PeriodClosedError`。

        写任何财务数据前都要调一次。关账后写入会让报表与账面不一致，
        而且这种不一致很难被发现 —— 它不会报错，只是数字对不上。

        Raises:
            PeriodClosedError: 期间已关账，需先申请重开。
        """
        record = await self.periods.get_by_period(period, shop_id=shop_id)
        if record is None:
            # 期间未建立视为未关账（首次录入的常见情形）
            return
        if record.status == PeriodStatus.CLOSED.value:
            raise PeriodClosedError(period, shop_id=shop_id)

    async def is_period_closed(self, period: str, *, shop_id: int | None = None) -> bool:
        """查询期间是否已关账（不抛异常，供展示用）。"""
        record = await self.periods.get_by_period(period, shop_id=shop_id)
        return record is not None and record.status == PeriodStatus.CLOSED.value

    # ========================================================
    # 利润计算
    # ========================================================

    @staticmethod
    def build_breakdown(
        *,
        revenue: Decimal,
        cogs: Decimal,
        platform_fees: Decimal,
        shipping: Decimal,
        ad_cost: Decimal,
        refunds: Decimal,
    ) -> ProfitBreakdown:
        """组装贡献利润分解。

        **不做汇率换算** —— 换算涉及"用哪个汇率"的业务判断
        （优先结算汇率，见 TDD-05），属于口径决策，
        应由调用方在明确口径后传入同币种金额。
        这里混进汇率逻辑会让"算错了"和"口径不同"难以区分。

        所有入参必须已是 `Decimal`；用 `float` 会引入二进制浮点误差，
        在金额上表现为"差一分钱"这类无法解释的尾差。
        """
        return ProfitBreakdown(
            revenue=money(revenue),
            cogs=money(cogs),
            platform_fees=money(platform_fees),
            shipping=money(shipping),
            ad_cost=money(ad_cost),
            refunds=money(refunds),
        )

    @staticmethod
    def total_revenue(amounts: list[Decimal]) -> Decimal:
        """汇总收入。

        先 `sum` 再 `money()` 规整 —— 反过来（先规整再累加）
        会在分项数量多时累积舍入误差，表现为"分项之和 ≠ 总额"。
        """
        return money(sum(amounts, Decimal("0")))
