"""仓位与风险管理。

核心思想：每笔交易在"入场价到止损价"之间只允许亏掉
账户权益的固定比例（默认 2.5%，见 config.yaml），因此：

    股数 = (权益 × 单笔风险比例) / (入场价 - 止损价)

止损越近仓位越大、止损越远仓位越小，
把每笔交易的美元风险标准化，是趋势跟随系统的标准做法。

另有两道组合级约束：
    1. 单只股票市值不超过权益的 max_weight（默认 20%），
       防止低波动股票算出超大仓位。
    2. 可用现金不足时按现金缩减（不使用杠杆）。
"""

from __future__ import annotations

import math


def position_size(
    equity: float,
    available: float,
    price: float,
    stop: float,
    risk_cfg: dict,
    risk_scale: float = 1.0,
) -> int:
    """计算一笔新开仓的股数。

    参数:
        equity:    当前账户总权益（现金 + 持仓市值）
        available: 当前可用购买力。未开杠杆时就是现金；
                   开启波动率目标杠杆后为 现金 + (f-1) × 权益，
                   f 为当前杠杆倍数（可小于 1，即主动降杠杆）
        price:     预计成交价
        stop:      初始止损价
        risk_cfg:  配置中的 risk 小节
        risk_scale: 风险缩放系数（= 杠杆倍数 f）。同时缩放单笔
                   风险预算与单仓市值上限，使杠杆真正作用于仓位规模

    返回股数（整数，可能为 0 表示放弃本次开仓）。
    """
    risk_per_share = price - stop
    # 止损价必须低于成交价，且距离不能小到失去意义（防除零 / 天量仓位）
    if risk_per_share <= 0 or price <= 0:
        return 0

    # 第一道：按单笔风险预算计算（随杠杆倍数缩放）
    shares = (equity * risk_cfg["risk_per_trade"] * risk_scale) / risk_per_share

    # 第二道：单仓市值上限（随杠杆倍数缩放）
    shares = min(shares, equity * risk_cfg["max_weight"] * risk_scale / price)

    # 第三道：购买力约束（预留 0.5% 缓冲以覆盖滑点）
    shares = min(shares, available * 0.995 / price)

    return max(0, math.floor(shares))
