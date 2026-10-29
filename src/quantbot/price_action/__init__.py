"""PABot —— 基于价格行为（Price Action）的美股交易系统。

子模块说明：
    data        行情数据下载与本地缓存、股票池定义
    priceaction 价格行为核心算法（摆动结构、K 线形态、支撑阻力）
    strategy    交易策略（信号生成，回测与实盘共用）
    risk        仓位与风险管理
    backtest    组合级回测引擎与绩效指标
    broker      券商接口抽象与 Alpaca 实现（模拟盘 / 实盘）
    live        实盘 / 模拟盘每日调度器
"""

__version__ = "1.1.0"
