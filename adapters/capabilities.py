"""平台能力枚举与能力矩阵（TDD-03 §2）。

设计要点（TDD-01 原则 1）：
    业务层的分支条件是**能力**，不是**平台名**。

    ✅ 正确：
        if caps.get(Capability.LISTING_CREATE) == CapabilityState.ASYNC:
            return await self._publish_via_async_flow(...)

    ❌ 错误：
        if platform == "amazon":
            return await self._publish_via_async_flow(...)

为什么能力状态是六态而不是布尔：
    "支持 / 不支持"会掩盖大量灰色地带。例如 Amazon 的退款：
    不是"不能退款"，而是"卖家侧没有执行退款的 API，只能人工去后台操作"。
    这两种情况对系统的要求完全不同 —— 前者应该隐藏功能，
    后者应该生成人工待办。
"""

from __future__ import annotations

from enum import StrEnum

from core.constants import CapabilityState, Platform

__all__ = [
    "Capability",
    "CapabilityMatrix",
    "AMAZON_CAPABILITIES",
    "MOCK_CAPABILITIES",
    "TIKTOK_CAPABILITIES",
    "TEMU_CAPABILITIES",
]


class Capability(StrEnum):
    """平台能力枚举。

    适配器必须声明**完整覆盖**本枚举（未声明的视为 UNSUPPORTED，
    但契约测试会要求显式声明，避免"忘记写"被当成"不支持"）。
    """

    # ===== 读：商品与刊登 =====
    LISTING_READ = "LISTING_READ"
    LISTING_SEARCH = "LISTING_SEARCH"
    CATEGORY_TREE_READ = "CATEGORY_TREE_READ"
    CATEGORY_SCHEMA_READ = "CATEGORY_SCHEMA_READ"
    LISTING_ELIGIBILITY_CHECK = "LISTING_ELIGIBILITY_CHECK"

    # ===== 读：订单与履约 =====
    ORDER_READ = "ORDER_READ"
    ORDER_SEARCH = "ORDER_SEARCH"
    SHIPMENT_READ = "SHIPMENT_READ"

    # ===== 读：库存 =====
    INVENTORY_READ = "INVENTORY_READ"

    # ===== 读：财务 =====
    SETTLEMENT_READ = "SETTLEMENT_READ"
    FEE_READ = "FEE_READ"
    REIMBURSEMENT_READ = "REIMBURSEMENT_READ"
    REPORT_REQUEST = "REPORT_REQUEST"

    # ===== 写：商品与刊登 =====
    LISTING_CREATE = "LISTING_CREATE"
    LISTING_UPDATE = "LISTING_UPDATE"
    LISTING_DELETE = "LISTING_DELETE"
    LISTING_PRICE_UPDATE = "LISTING_PRICE_UPDATE"
    LISTING_BULK_CREATE = "LISTING_BULK_CREATE"
    PRICING_RULE_WRITE = "PRICING_RULE_WRITE"

    # ===== 写：库存 =====
    INVENTORY_UPDATE = "INVENTORY_UPDATE"
    INVENTORY_BULK_UPDATE = "INVENTORY_BULK_UPDATE"

    # ===== 写：订单与售后 =====
    ORDER_SHIP_CONFIRM = "ORDER_SHIP_CONFIRM"
    ORDER_CANCEL = "ORDER_CANCEL"
    REFUND_CREATE = "REFUND_CREATE"
    REFUND_READ = "REFUND_READ"
    RETURN_READ = "RETURN_READ"
    MESSAGE_SEND = "MESSAGE_SEND"
    MESSAGE_READ = "MESSAGE_READ"

    # ===== 广告（Phase 2 预留）=====
    AD_CAMPAIGN_READ = "AD_CAMPAIGN_READ"
    AD_CAMPAIGN_WRITE = "AD_CAMPAIGN_WRITE"
    AD_REPORT_READ = "AD_REPORT_READ"

    # ===== 订阅与通知 =====
    NOTIFICATION_SUBSCRIBE = "NOTIFICATION_SUBSCRIBE"
    NOTIFICATION_READ = "NOTIFICATION_READ"

    # ===== 通用 =====
    BULK_OPERATION = "BULK_OPERATION"
    SANDBOX_AVAILABLE = "SANDBOX_AVAILABLE"

    @property
    def is_write(self) -> bool:
        """是否为写操作。

        Phase 1 只允许只读能力被调用（TDD-01 §5.3）。
        staging 环境对平台只读，靠这个属性做开关。
        """
        return self.value.endswith(
            ("_CREATE", "_UPDATE", "_DELETE", "_WRITE", "_CONFIRM", "_CANCEL", "_SEND")
        )

    @property
    def group(self) -> str:
        """能力分组（用于前端分类展示与权限映射）。"""
        return self.value.split("_", 1)[0].lower()


class CapabilityMatrix:
    """能力矩阵的只读封装。

    提供比裸 dict 更友好的查询接口，并强制"未声明 = UNSUPPORTED"
    的语义（而不是 KeyError）。
    """

    __slots__ = ("_platform", "_matrix")

    def __init__(self, platform: str, matrix: dict[Capability, CapabilityState]) -> None:
        self._platform = platform
        self._matrix = dict(matrix)

    @property
    def platform(self) -> str:
        return self._platform

    def get(self, cap: Capability) -> CapabilityState:
        """查能力状态。未声明的返回 UNSUPPORTED（而非报错）。"""
        return self._matrix.get(cap, CapabilityState.UNSUPPORTED)

    def supports(self, cap: Capability) -> bool:
        """是否可直接调用（NATIVE / ASYNC / WEBHOOK）。"""
        return self.get(cap).callable

    def needs_human(self, cap: Capability) -> bool:
        """是否需要人工介入。"""
        return self.get(cap).needs_human

    def writable_capabilities(self) -> list[Capability]:
        """全部可写能力。"""
        return [c for c, s in self._matrix.items() if c.is_write and s.callable]

    def missing(self) -> list[Capability]:
        """未声明的能力（契约测试要求为空）。"""
        declared = set(self._matrix)
        return [c for c in Capability if c not in declared]

    def as_dict(self) -> dict[str, str]:
        """序列化（API 响应用）。"""
        return {c.value: self.get(c).value for c in Capability}

    def summary(self) -> str:
        """可读摘要（排障用）。"""
        counts: dict[str, int] = {}
        for c in Capability:
            state = self.get(c)
            counts[state.value] = counts.get(state.value, 0) + 1
        parts = [f"{k}={v}" for k, v in sorted(counts.items()) if v]
        return f"{self._platform}: " + ", ".join(parts)


# ============================================================
# 各平台能力矩阵
# ============================================================
#
# 数据来源与置信度说明（TDD-03 §2.3）：
#   本表基于官方文档公开信息整理，标注需实测的项在 comments 中说明。
#   在拿到授权并实测前，这些是**开发假设**而非承诺。

_N = CapabilityState.NATIVE
_A = CapabilityState.ASYNC
_W = CapabilityState.WEBHOOK
_F = CapabilityState.FILE_IMPORT
_M = CapabilityState.MANUAL
_U = CapabilityState.UNSUPPORTED


#: Amazon SP-API 能力矩阵。
#:
#: 三个**必须记住**的硬事实（TDD-03 §2.3、§4.4）：
#:   1. REFUND_CREATE = UNSUPPORTED
#:      卖家侧没有"由卖家直接发起退款"的 API。退款由平台处理，
#:      卖家只能读退款记录。因此 PRD 12.5 的"退换货自动化"
#:      在 Amazon 上必须降级为"自动分类 + 人工待办"。
#:   2. MESSAGE_READ = UNSUPPORTED
#:      Messaging API 只提供 getMessagingActionsForOrder（查可执行动作）
#:      与发送消息，**没有**"拉取买家和卖家会话全文"的接口。
#:   3. SANDBOX_AVAILABLE = MANUAL（覆盖不全）
#:      部分接口有静态沙箱，多数只有生产环境。
#:      这是 ADR-005（Mock 适配器）的另一个理由。
AMAZON_CAPABILITIES: dict[Capability, CapabilityState] = {
    # 读：商品
    Capability.LISTING_READ: _N,
    Capability.LISTING_SEARCH: _N,
    Capability.CATEGORY_TREE_READ: _N,
    Capability.CATEGORY_SCHEMA_READ: _N,  # Product Type Definitions API
    Capability.LISTING_ELIGIBILITY_CHECK: _N,  # Listings Restrictions
    # 读：订单
    Capability.ORDER_READ: _N,
    Capability.ORDER_SEARCH: _N,  # 有时间窗限制
    Capability.SHIPMENT_READ: _N,
    # 读：库存
    Capability.INVENTORY_READ: _N,
    # 读：财务（主要靠 Reports API 异步拉）
    Capability.SETTLEMENT_READ: _A,
    Capability.FEE_READ: _A,
    Capability.REIMBURSEMENT_READ: _A,
    Capability.REPORT_REQUEST: _A,
    # 写：商品（Feeds 是异步的，四步缺一不可）
    Capability.LISTING_CREATE: _A,
    Capability.LISTING_UPDATE: _A,
    Capability.LISTING_DELETE: _A,
    Capability.LISTING_PRICE_UPDATE: _N,
    Capability.LISTING_BULK_CREATE: _A,
    Capability.PRICING_RULE_WRITE: _U,  # 自动定价在 Seller Central 配置
    # 写：库存
    Capability.INVENTORY_UPDATE: _N,
    Capability.INVENTORY_BULK_UPDATE: _A,
    # 写：订单与售后
    Capability.ORDER_SHIP_CONFIRM: _N,
    Capability.ORDER_CANCEL: _N,
    Capability.REFUND_CREATE: _U,  # ← 关键：无卖家侧退款 API
    Capability.REFUND_READ: _A,  # 通过 Reports
    Capability.RETURN_READ: _A,
    Capability.MESSAGE_SEND: _N,  # 仅限平台允许的模板
    Capability.MESSAGE_READ: _U,  # ← 关键：无历史消息读取 API
    # 广告（独立授权）
    Capability.AD_CAMPAIGN_READ: _N,
    Capability.AD_CAMPAIGN_WRITE: _N,
    Capability.AD_REPORT_READ: _A,
    # 通知
    Capability.NOTIFICATION_SUBSCRIBE: _W,  # SNS / SQS
    Capability.NOTIFICATION_READ: _N,
    # 通用
    Capability.BULK_OPERATION: _A,
    Capability.SANDBOX_AVAILABLE: _M,  # 覆盖不全
}


#: TikTok Shop 能力矩阵。
TIKTOK_CAPABILITIES: dict[Capability, CapabilityState] = {
    Capability.LISTING_READ: _N,
    Capability.LISTING_SEARCH: _N,
    Capability.CATEGORY_TREE_READ: _N,
    Capability.CATEGORY_SCHEMA_READ: _N,
    Capability.LISTING_ELIGIBILITY_CHECK: _N,
    Capability.ORDER_READ: _N,
    Capability.ORDER_SEARCH: _N,
    Capability.SHIPMENT_READ: _N,
    Capability.INVENTORY_READ: _N,
    Capability.SETTLEMENT_READ: _N,
    Capability.FEE_READ: _N,
    Capability.REIMBURSEMENT_READ: _M,
    Capability.REPORT_REQUEST: _A,
    Capability.LISTING_CREATE: _N,
    Capability.LISTING_UPDATE: _N,
    Capability.LISTING_DELETE: _N,
    Capability.LISTING_PRICE_UPDATE: _N,
    Capability.LISTING_BULK_CREATE: _A,
    Capability.PRICING_RULE_WRITE: _U,
    Capability.INVENTORY_UPDATE: _N,
    Capability.INVENTORY_BULK_UPDATE: _N,
    Capability.ORDER_SHIP_CONFIRM: _N,
    Capability.ORDER_CANCEL: _N,
    Capability.REFUND_CREATE: _N,  # 支持卖家侧退款
    Capability.REFUND_READ: _N,
    Capability.RETURN_READ: _N,
    Capability.MESSAGE_SEND: _N,
    Capability.MESSAGE_READ: _N,  # 需特别审批，且必须卖家授权
    Capability.AD_CAMPAIGN_READ: _N,
    Capability.AD_CAMPAIGN_WRITE: _N,
    Capability.AD_REPORT_READ: _N,
    Capability.NOTIFICATION_SUBSCRIBE: _W,
    Capability.NOTIFICATION_READ: _N,
    Capability.BULK_OPERATION: _A,
    Capability.SANDBOX_AVAILABLE: _N,
}


#: Temu 能力矩阵。
#:
#: **置信度低**：Temu 的 Partner Platform 文档公开程度有限，
#: 下表多数项需在拿到授权后实测确认（TDD-03 §2.3 附录 C）。
#: 在实测前不应作为排期依据。
TEMU_CAPABILITIES: dict[Capability, CapabilityState] = {
    Capability.LISTING_READ: _N,
    Capability.LISTING_SEARCH: _N,
    Capability.CATEGORY_TREE_READ: _N,
    Capability.CATEGORY_SCHEMA_READ: _U,  # 待实测：无动态属性 schema 接口
    Capability.LISTING_ELIGIBILITY_CHECK: _U,
    Capability.ORDER_READ: _N,
    Capability.ORDER_SEARCH: _N,
    Capability.SHIPMENT_READ: _N,
    Capability.INVENTORY_READ: _N,
    Capability.SETTLEMENT_READ: _M,  # 待实测：可能只能后台导出
    Capability.FEE_READ: _M,
    Capability.REIMBURSEMENT_READ: _U,
    Capability.REPORT_REQUEST: _M,
    Capability.LISTING_CREATE: _N,
    Capability.LISTING_UPDATE: _N,
    Capability.LISTING_DELETE: _N,
    Capability.LISTING_PRICE_UPDATE: _N,
    Capability.LISTING_BULK_CREATE: _N,
    Capability.PRICING_RULE_WRITE: _U,
    Capability.INVENTORY_UPDATE: _N,
    Capability.INVENTORY_BULK_UPDATE: _N,
    Capability.ORDER_SHIP_CONFIRM: _N,
    Capability.ORDER_CANCEL: _N,
    Capability.REFUND_CREATE: _N,
    Capability.REFUND_READ: _N,
    Capability.RETURN_READ: _N,
    Capability.MESSAGE_SEND: _U,  # 待实测
    Capability.MESSAGE_READ: _U,
    Capability.AD_CAMPAIGN_READ: _M,
    Capability.AD_CAMPAIGN_WRITE: _M,
    Capability.AD_REPORT_READ: _M,
    Capability.NOTIFICATION_SUBSCRIBE: _U,  # 待实测：可能无 webhook
    Capability.NOTIFICATION_READ: _N,
    Capability.BULK_OPERATION: _N,
    Capability.SANDBOX_AVAILABLE: _U,
}


#: Mock 适配器能力矩阵。
#:
#: 设计意图（ADR-005）：Mock 不是"假数据占位"，而是**能力超集**。
#: 它声明支持绝大多数能力，以便：
#:   1. 锁定接口契约（Mock 与真实适配器实现同一接口）
#:   2. 测试真实沙箱测不了的异常分支（429 / 部分成功 / 结构漂移）
#:   3. 验证数据模型（用真实响应结构建表）
#:   4. 支撑端到端演示
#:
#: 但**故意保留部分能力为 UNSUPPORTED**，用于测试降级路径：
#:   REFUND_CREATE / MESSAGE_READ 声明为 UNSUPPORTED，
#:   以模拟 Amazon 的行为，验证"生成人工待办"的逻辑。
MOCK_CAPABILITIES: dict[Capability, CapabilityState] = {
    Capability.LISTING_READ: _N,
    Capability.LISTING_SEARCH: _N,
    Capability.CATEGORY_TREE_READ: _N,
    Capability.CATEGORY_SCHEMA_READ: _N,
    Capability.LISTING_ELIGIBILITY_CHECK: _N,
    Capability.ORDER_READ: _N,
    Capability.ORDER_SEARCH: _N,
    Capability.SHIPMENT_READ: _N,
    Capability.INVENTORY_READ: _N,
    Capability.SETTLEMENT_READ: _N,
    Capability.FEE_READ: _N,
    Capability.REIMBURSEMENT_READ: _N,
    Capability.REPORT_REQUEST: _A,
    Capability.LISTING_CREATE: _A,  # 默认异步，用于测轮询链路
    Capability.LISTING_UPDATE: _A,
    Capability.LISTING_DELETE: _N,
    Capability.LISTING_PRICE_UPDATE: _N,
    Capability.LISTING_BULK_CREATE: _A,
    Capability.PRICING_RULE_WRITE: _N,
    Capability.INVENTORY_UPDATE: _N,
    Capability.INVENTORY_BULK_UPDATE: _N,
    Capability.ORDER_SHIP_CONFIRM: _N,
    Capability.ORDER_CANCEL: _N,
    # ↓ 故意不支持，用于验证"降级为人工待办"的路径
    Capability.REFUND_CREATE: _U,
    Capability.REFUND_READ: _N,
    Capability.RETURN_READ: _N,
    Capability.MESSAGE_SEND: _N,
    Capability.MESSAGE_READ: _U,
    # ↓
    Capability.AD_CAMPAIGN_READ: _N,
    Capability.AD_CAMPAIGN_WRITE: _U,  # Phase 1 不做写广告
    Capability.AD_REPORT_READ: _N,
    Capability.NOTIFICATION_SUBSCRIBE: _W,
    Capability.NOTIFICATION_READ: _N,
    Capability.BULK_OPERATION: _N,
    Capability.SANDBOX_AVAILABLE: _N,
}


#: 平台 → 能力矩阵。注册表与前端展示共用。
PLATFORM_CAPABILITIES: dict[str, dict[Capability, CapabilityState]] = {
    Platform.AMAZON.value: AMAZON_CAPABILITIES,
    Platform.TIKTOK.value: TIKTOK_CAPABILITIES,
    Platform.TEMU.value: TEMU_CAPABILITIES,
    Platform.MOCK.value: MOCK_CAPABILITIES,
}
