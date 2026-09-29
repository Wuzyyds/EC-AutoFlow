"""金额计算工具。

核心约定（TDD-01 原则 3、ADR-007 §3.2）：
    1. 金额一律用 Decimal，**禁止 float**
    2. 数据库存储精度 `DECIMAL(18,6)`
    3. 展示精度 2 位小数
    4. 舍入用 ROUND_HALF_UP（财务惯例），**不用 Python 默认的 ROUND_HALF_EVEN**

为什么舍入规则要显式指定：
    Python 的 Decimal 默认是银行家舍入（ROUND_HALF_EVEN），
    即 0.125 → 0.12。而财务惯例是四舍五入，0.125 → 0.13。
    如果依赖默认值，会产生系统性偏差 —— 单笔看不出来，
    汇总到万级订单时就是对不上账的几分钱，且极难追溯。

为什么禁止 float：
    0.1 + 0.2 == 0.30000000000000004。
    金额一旦经过 float，误差就不可逆。本模块的 to_decimal()
    对 float 走 str 中转，把误差挡在入口。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation

__all__ = [
    "MONEY_SCALE",
    "DISPLAY_SCALE",
    "ZERO",
    "ONE",
    "HUNDRED",
    "to_decimal",
    "money",
    "display",
    "display_str",
    "safe_div",
    "pct",
    "apply_rate",
    "sum_money",
    "allocate",
    "is_balanced",
    "parse_money_str",
]

#: 存储精度：6 位小数（与 DECIMAL(18,6) 对齐）
MONEY_SCALE = Decimal("0.000001")

#: 展示精度：2 位小数
DISPLAY_SCALE = Decimal("0.01")

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

#: 金额字段的数值上限（DECIMAL(18,6) 的整数部分是 12 位）
MAX_AMOUNT = Decimal("999999999999.999999")


def to_decimal(value: int | float | str | Decimal) -> Decimal:
    """转 Decimal。

    float 会被先转成 str 再转 Decimal，避免二进制表示误差污染：
        Decimal(0.1)      → 0.1000000000000000055511151231257827...
        Decimal(str(0.1)) → 0.1

    Raises:
        TypeError: 传入不支持的类型（如 None、list）。
        ValueError: 字符串无法解析为数字。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # bool 是 int 的子类，但语义上不该出现在金额里
        raise TypeError("布尔值不能作为金额")
    if isinstance(value, float):
        # 允许但从 str 中转；调用方若在意精度应直接传 str/Decimal
        return Decimal(str(value))
    if isinstance(value, (int, str)):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"无法解析为金额：{value!r}") from exc
    raise TypeError(f"不支持的类型 {type(value).__name__}，金额只接受 int/float/str/Decimal")


def money(value: int | float | str | Decimal) -> Decimal:
    """规整为存储精度（6 位小数，四舍五入）。

    写库前必须经过此函数，保证与 DECIMAL(18,6) 一致。
    """
    result = to_decimal(value).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
    if abs(result) > MAX_AMOUNT:
        raise ValueError(f"金额超出 DECIMAL(18,6) 范围：{result}")
    return result


def display(value: int | float | str | Decimal) -> Decimal:
    """规整为展示精度（2 位小数，四舍五入）。"""
    return to_decimal(value).quantize(DISPLAY_SCALE, rounding=ROUND_HALF_UP)


def display_str(value: int | float | str | Decimal, *, symbol: str = "") -> str:
    """格式化为展示字符串，如 "¥1,234.56"。"""
    amount = display(value)
    return f"{symbol}{amount:,.2f}"


def safe_div(
    numerator: int | float | str | Decimal,
    denominator: int | float | str | Decimal,
    *,
    default: Decimal = ZERO,
) -> Decimal:
    """安全除法。

    分母为 0 时返回 default 而不是抛异常。

    为什么需要它：报表里到处是比率（利润率、退货率、ACOS）。
    分母为 0 的情况（新店无销售、新 SKU 无曝光）非常常见，
    如果每次都写 try/except，代码会被淹没。
    """
    num = to_decimal(numerator)
    den = to_decimal(denominator)
    if den == ZERO:
        return default
    try:
        return num / den
    except (DivisionByZero, InvalidOperation):
        return default


def pct(
    part: int | float | str | Decimal,
    whole: int | float | str | Decimal,
    *,
    default: Decimal = ZERO,
) -> Decimal:
    """百分比（返回 0–100 的数值，不是 0–1 的小数）。

    TACOS、退货率、毛利率全部走这里，保证口径一致。
    """
    return safe_div(part, whole, default=default) * HUNDRED


def apply_rate(
    amount: int | float | str | Decimal,
    rate: int | float | str | Decimal,
) -> Decimal:
    """按汇率换算金额，结果规整到存储精度。

    换算后必须 quantize —— 否则会引入 6 位以上的小数，
    写库时被 DECIMAL(18,6) 静默截断（非严格模式下）或报错。
    """
    return money(to_decimal(amount) * to_decimal(rate))


def sum_money(values: list[int | float | str | Decimal]) -> Decimal:
    """求和并规整。空列表返回 0。

    禁止用内置 sum() 直接加 Decimal 列表 —— 它从 int 0 开始累加，
    虽然结果正确，但缺少最后的 quantize，容易产生精度残留。
    """
    total = ZERO
    for v in values:
        total += to_decimal(v)
    return money(total)


def allocate(
    total: int | float | str | Decimal,
    weights: list[int | float | str | Decimal],
) -> list[Decimal]:
    """按权重分摊总额，**保证分项之和精确等于总额**。

    这是本模块最重要的函数。头程运费分摊、平台费分摊、优惠分摊都依赖它。

    朴素实现的问题：
        total=100，weights=[1,1,1] → 每项 33.333333，
        三项之和 = 99.999999 ≠ 100。差 0.000001。
        单个订单看不出来，10 万订单后报表就差 0.1 元，
        财务对账时会被追问"这 0.1 从哪来的"。

    本实现的处理：
        前 n-1 项按比例舍入，最后一项 = 总额 − 已分配之和。
        尾差全部落在最后一项，保证恒等。

    Args:
        total: 待分摊总额。
        weights: 权重列表（如各 SKU 的货值或重量）。全为 0 时平均分摊。

    Returns:
        与 weights 等长的金额列表，sum(result) == money(total)。

    Raises:
        ValueError: weights 为空。
    """
    if not weights:
        raise ValueError("分摊权重不能为空")

    target = money(total)
    n = len(weights)
    decimals = [to_decimal(w) for w in weights]
    weight_sum = sum(decimals)

    if weight_sum == ZERO:
        # 权重全为 0：平均分摊，同样用"最后一项兜底"处理尾差
        if n == 1:
            return [target]
        each = money(target / n)
        allocated = [each] * (n - 1)
        allocated.append(money(target - sum(allocated)))
        return allocated

    allocated: list[Decimal] = []
    running = ZERO
    for i, w in enumerate(decimals):
        if i == n - 1:
            # 最后一项兜底，吸收全部尾差
            allocated.append(money(target - running))
        else:
            part = money(target * w / weight_sum)
            allocated.append(part)
            running += part

    return allocated


def is_balanced(
    parts: list[int | float | str | Decimal],
    total: int | float | str | Decimal,
    *,
    tolerance: Decimal = ZERO,
) -> bool:
    """校验分项之和是否等于总额（报表自洽性检查）。

    TDD-06 §1.3 要求"分项之和 = 合计"必须被测试覆盖。
    本函数用于运行时自检与测试断言。

    Args:
        tolerance: 允许误差。默认 0（要求精确相等），
                   因为 allocate() 已保证精确。
    """
    diff = abs(sum_money(parts) - money(total))
    return diff <= tolerance


def parse_money_str(text: str) -> Decimal:
    """解析带货币符号与千分位的金额字符串。

    平台账单/CSV 里常见 "¥1,234.56"、"$ 12.30"、"1.234,56"(欧式)。
    本函数处理前两种常见形式；欧式千分位分隔需调用方预处理。
    """
    cleaned = (
        text.strip()
        .replace(",", "")
        .replace("¥", "")
        .replace("$", "")
        .replace("€", "")
        .replace("£", "")
        .replace(" ", "")
    )
    if not cleaned or cleaned in {"-", "--"}:
        return ZERO
    try:
        return money(cleaned)
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"无法解析金额字符串：{text!r}") from exc
