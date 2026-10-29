# 美股个人自动量化系统设计方案

**版本** v1.0 ｜ **日期** 2026-06 ｜ **定位** 个人全自动量化投资系统（美股）

---

## 一、项目概述

### 1.1 核心理念

本系统以「多源异构信号融合 + 动态市场自适应」为核心，通过三个互补的引擎（Alpha158 / Kronos / RD-Agent）构建候选因子池，再由 TradingAgent 聚合为可交易的观察池。Regime Switching 贯穿整个决策链，确保策略在不同市场状态下均保持鲁棒性，从根本上遏制过拟合。

系统面向美股市场（S&P 500 / Russell 1000 成分股），日频信号为主，支持盘后生成次日目标组合，通过 Alpaca / IBKR API 自动执行。

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| 多样性 | 三引擎覆盖规则因子、时序预测信号、LLM 自动发现因子三个信息维度，降低因子间相关性 |
| 鲁棒性 | Regime Switching 保证策略跨市场周期稳定，Walk-Forward 优化防止参数过拟 |
| 可解释性 | 每个因子必须有明确经济逻辑，拒绝黑箱 |
| 自动化 | 数据拉取 → 因子计算 → 信号聚合 → 下单执行全流程无人工干预 |
| 可维护性 | 个人系统优先简洁，模块独立解耦，每个模块可单独升级替换 |

### 1.3 目标市场规格

```
交易所         NYSE / NASDAQ
投资域         S&P 500 成分股 + 扩展至 Russell 1000（流动性过滤后约 600-800 只）
信号频率       日频（盘后计算，次日开盘/VWAP 执行）
策略类型       多头为主，可选对冲空头
资金规模       $50K-$500K（影响仓位管理和冲击成本估算）
基准           SPY（S&P 500 ETF）
目标超额       年化 Alpha > 5%，Sharpe > 1.2
```

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         数据基础设施层                                    │
│  Alpaca/Polygon.io  │  SEC EDGAR  │  FRED  │  yfinance  │  另类数据     │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      因子挖掘层  ·  三引擎并联                            │
│                                                                          │
│   ┌──────────────┐    ┌──────────────┐    ┌────────────────────────┐   │
│   │   Alpha158   │    │    Kronos    │    │       RD-Agent          │   │
│   │  规则驱动因子 │    │  时序基础模型 │    │  LLM 自动因子发现       │   │
│   └──────┬───────┘    └──────┬───────┘    └──────────┬─────────────┘   │
│          └──────────────────┴──────────────────────┘                   │
│                              │ 因子评估与筛选                             │
│                   IC / ICIR / 分层回测 / 相关性检验                       │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         候选因子池                                        │
│                  20-40 个核心因子  ·  持续迭代更新                         │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
             ▼                 ▼                 ▼
   ┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐
   │  Regime 识别引擎  │ │  自研因子接入 │ │  组合优化 / 权重  │
   │  VIX + HMM + 宏观 │ │  持续补充候选池│ │  Walk-Forward    │
   └────────┬─────────┘ └──────┬───────┘ └──────────┬───────┘
            └──────────────────┼────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          TradingAgent                                    │
│            Regime 感知 · 多因子加权评分 · LLM 辅助过滤                    │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                            观察池                                        │
│                  Top 20-50 多头标的  ·  动态进出                          │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       风险控制 & 自动执行层                               │
│           仓位管理 / 止损 / 行业集中度 / Alpaca API 下单                  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 三、数据基础设施

### 3.1 数据源矩阵

| 数据类型 | 推荐来源 | 费用 | 更新频率 | 备注 |
|---------|---------|------|---------|------|
| 日频 OHLCV | Alpaca Data API | 免费（有账号） | 实时/日更 | 优先选择，API 稳定 |
| 分钟级数据 | Polygon.io Basic | $29/月 | 实时 | 用于日内 VWAP 执行 |
| 历史数据补充 | yfinance | 免费 | 日更 | 作为 Alpaca 备用 |
| 财务基本面 | SEC EDGAR API | 免费 | 季度 | 三大报表、10-K/10-Q |
| 盈利预期 | Nasdaq Data Link | 部分免费 | 日更 | 分析师预期修正因子 |
| 宏观数据 | FRED API | 免费 | 月/日 | 利率、CPI、就业数据 |
| VIX / 期权 | CBOE / yfinance | 免费 | 日更 | Regime 识别核心 |
| 短卖数据 | FINRA / Quandl | 免费（延迟） | 双周 | 空头兴趣因子 |
| 机构持仓 | SEC 13F | 免费 | 季度 | 聪明钱因子 |
| 新闻/情绪 | Alpaca News / GDELT | 免费/免费 | 实时 | LLM 情绪因子 |

### 3.2 数据清洗标准

**复权处理**：全部使用后复权（Adjusted Close）为因子计算基础，防止股息/拆股引起的虚假信号。

**缺失值策略**：停市/节假日前向填充；IPO 不足 252 日的股票排除；退市股票保留历史数据（Survivor Bias 防护）。

**流动性过滤**：过滤掉 20 日均成交额低于 $5M 的标的，确保实盘可执行性。

**财务数据对齐**：使用 SEC 实际发布日期（非报告期末），季报发布后 2 日才允许使用对应数据，彻底消除 Look-ahead Bias。

**数据库方案**：DuckDB + Parquet 文件（列存储，查询快，无需服务器，免运维）。按日期分区存储，历史数据拉取至少覆盖 2010 年至今（含多个完整牛熊周期）。

---

## 四、因子挖掘层：三引擎并联

### 4.1 Alpha158 模块

**定位**：规则驱动的标准化技术因子库，提供系统的"基础代谢"。

**来源**：Microsoft Qlib 框架的 Alpha158，适用于任何股票市场，通过表达式引擎高效计算。

**因子分类与代表因子**：

| 类别 | 数量 | 代表因子 | 经济逻辑 |
|------|------|---------|---------|
| 动量类 | ~40 | 多周期回报率、MACD 偏离 | 趋势延续效应 |
| 量价关系 | ~35 | 量价相关性、换手率偏离 | 资金行为信号 |
| 波动率类 | ~25 | 历史波动率、ATR | 风险定价偏差 |
| 技术形态 | ~30 | RSI、布林带位置 | 超买超卖修正 |
| 均线系统 | ~28 | 多周期均线偏离 | 趋势状态刻画 |

**与美股的特殊适配**：Alpha158 原始设计基于 A 股数据，用于美股时需要特别注意以下调整：去除 A 股涨跌停因子（±10% 不适用于美股）；在日历效应上增加财报季（Earnings Season）虚拟变量；对于 Nasdaq 科技股适当调整换手率阈值（美股换手率整体低于 A 股）。

```python
import qlib
from qlib.contrib.data.handler import Alpha158

# 美股数据接入 Qlib
qlib.init(provider_uri="~/.qlib/us_data", region="us")

handler = Alpha158(
    instruments="sp500",
    start_time="2015-01-01",
    end_time="2024-12-31",
)
```

**输出**：日频因子矩阵 `[Date × Ticker]`，需做截面 Z-Score 标准化 + 5%/95% 分位数截尾处理。

---

### 4.2 Kronos 模块

**定位**：捕捉 Alpha158 线性规则难以表达的非线性时序结构，将"时序预测置信度"转化为多空信号。

**名称来源**：Kronos（希腊时间之神）寓意对时间序列规律的深度挖掘。在本系统中指代时序深度学习层，可对接多种基础模型。

#### 方案 A：接入时序基础模型（推荐初期）

使用 Amazon Chronos、Google TimesFM 或 Salesforce Moirai 等预训练时序基础模型，对每只股票的收益率序列做零样本或少样本预测，提取预测分布特征作为因子：

```python
from chronos import ChronosPipeline
import torch

pipeline = ChronosPipeline.from_pretrained(
    "amazon/chronos-t5-base",
    device_map="cuda",
    torch_dtype=torch.bfloat16,
)

# 对每只股票：输入过去 T 天收益率，输出未来 H 天预测分布
# 从预测分布中提取三个因子：
#   kronos_mean   = 预测期望收益（方向性信号）
#   kronos_vol    = 预测不确定性（风险信号）
#   kronos_skew   = 预测分布偏度（尾部风险）
forecast = pipeline.predict(returns_context, prediction_length=5)
kronos_mean  = forecast.mean(dim=1).squeeze().numpy()
kronos_vol   = forecast.std(dim=1).squeeze().numpy()
```

**优点**：无需训练、快速出信号、避免过拟合（基础模型泛化能力强）。

#### 方案 B：自训练时序模型（中后期升级）

基于 Qlib 内置的 PatchTST / Transformer / LSTM 模型，以 Alpha158 因子矩阵 + OHLCV 为输入，预测未来 N 日超额收益。采用滚动重训策略：每季度用过去 4 年数据重训，避免模型参数过时。

**建议路径**：Phase 1-3 使用方案 A 快速验证信号有效性；Phase 5 之后迁移方案 B 进行精细化调优。

---

### 4.3 RD-Agent 模块

**定位**：LLM 驱动的自动化因子发现引擎，持续生成新的因子假设并自动验证，构成系统的"进化机制"。

**工作闭环**：

```
① 假设生成
   输入：市场异象观察 / 金融学术论文摘要 / 已失效因子的特征
   输出：新因子的经济逻辑描述（1-2句）

② 代码生成
   LLM 将假设转化为 Python / Qlib 表达式代码

③ 验证阶段
   自动运行全市场 IC 分析 + 分层回测 + 换手率检验

④ 决策
   通过 → 进入候选池候选名单
   失败 → 错误信息反馈 LLM → 最多 N 轮迭代

⑤ 定期综述
   每月由 LLM 生成"因子研究月报"，总结新发现与淘汰因子
```

**对美股的特有优势**：RD-Agent 可直接阅读 SEC 8-K 公告、earnings call transcript、分析师研报，提炼出文本信号，自动构建情绪/事件驱动因子，这在美股数据丰富的生态下尤其有价值。

```python
# RD-Agent 与 Qlib 的官方集成示例
from rdagent.scenarios.qlib.factor_experiment import QlibFactorExperiment

exp = QlibFactorExperiment(
    factor_description="Short interest ratio increases when institutional 
                         sentiment shifts negative, predicting future underperformance",
    market="us_sp500",
    validation_period=("2020-01-01", "2023-12-31"),
)
result = exp.run()
print(result.ic_mean, result.icir)
```

---

## 五、候选因子池管理

### 5.1 因子准入标准

| 指标 | 门槛（美股日频） | 说明 |
|------|-------------|------|
| IC 均值 | > 0.025 | 美股市场效率更高，门槛略低于 A 股 |
| ICIR | > 0.35 | IC 的信噪比，衡量信号稳定性 |
| IC 正向率 | > 54% | IC > 0 的月份占比 |
| 分层多空收益 | 年化 > 8%（扣费后） | Top Quintile vs Bottom Quintile |
| 信号衰减周期 | > 3 个交易日 | 衰减太快则换手成本不可控 |
| 样本外 IC | > 0.02（2 年 OOS） | 强制样本外验证，防过拟合 |

### 5.2 因子去相关与精简

```python
import scipy.cluster.hierarchy as sch
import pandas as pd

def select_representative_factors(factor_df, corr_threshold=0.65):
    corr_matrix = factor_df.rank().corr(method="spearman")
    linkage = sch.linkage(corr_matrix.values, method="complete")
    clusters = sch.fcluster(linkage, t=corr_threshold, criterion="distance")
    
    selected = []
    for cluster_id in set(clusters):
        cluster_factors = [f for f, c in zip(factor_df.columns, clusters) if c == cluster_id]
        # 每组保留 ICIR 最高的因子
        best = max(cluster_factors, key=lambda f: icir_dict[f])
        selected.append(best)
    return selected
```

### 5.3 候选池规模与来源配比

目标保留 20-40 个核心因子，建议来源配比：Alpha158 贡献约 40%（成熟的基础信号），Kronos 贡献约 30%（预测性时序信号），RD-Agent 与自研因子共贡献约 30%（差异化 Alpha）。来源多样化本身就是一种"分散投资"，防止因单一来源失效而导致系统崩溃。

### 5.4 定期审查机制

每季度执行一次因子审查：计算最近 6 个月的 IC 衰减率，若某因子 IC 均值连续两个季度低于 0.01，则标记为"观察名单"，连续三个季度不达标则淘汰并触发 RD-Agent 补充新因子。这一机制模仿了机构的因子生命周期管理流程。

---

## 六、Market Regime Switching

### 6.1 设计理念

Regime Switching 是本系统防过拟合的核心武器。其逻辑是：**同一因子在不同市场状态下有效性截然不同**。动量因子在趋势牛市中极有效，在恐慌期间却会严重失效。如果只用全样本历史优化参数，模型会把"只有一种市场"作为假设，这是过拟合的根源。

通过先识别市场状态，再针对各状态独立验证因子有效性，可以确保每个因子在它真正有效的环境中才被赋予高权重，这从机制上消除了状态相关的过拟合。

### 6.2 美股 Regime 的定义与特征

美股相较 A 股，拥有更多标准化的 Regime 指示器（VIX、收益率曲线、信用利差），Regime 识别更可靠。

| Regime | 代码 | 核心特征 | 典型案例 |
|--------|------|---------|---------|
| 低波动趋势牛市 | R1 | VIX < 15，强动量，信用利差收窄 | 2017、2019、2021 |
| 高波动趋势牛市 | R2 | 15 < VIX < 25，上涨但波动大 | 2016、2023-2024 |
| 震荡/整理 | R3 | VIX < 20，无明显趋势，均值回归 | 2015、2018 年中 |
| 趋势熊市 | R4 | VIX > 25，持续下跌，动量反转 | 2022 年加息周期 |
| 危机/恐慌 | R5 | VIX > 35，流动性枯竭 | 2020 年 3 月，2008 年 |

### 6.3 Regime 特征工程

```python
def build_regime_features(market_data: pd.DataFrame) -> pd.DataFrame:
    features = pd.DataFrame(index=market_data.index)
    
    # 波动率维度
    features["vix_level"]    = market_data["VIX"]
    features["vix_ma_ratio"] = market_data["VIX"] / market_data["VIX"].rolling(60).mean()
    features["vol_regime"]   = market_data["SPY"].pct_change().rolling(21).std() * 252**0.5
    
    # 趋势维度
    features["spy_mom_1m"]   = market_data["SPY"].pct_change(21)
    features["spy_mom_3m"]   = market_data["SPY"].pct_change(63)
    features["ma_signal"]    = (market_data["SPY"].rolling(50).mean() > 
                                market_data["SPY"].rolling(200).mean()).astype(int)
    
    # 宏观/利率维度
    features["yield_curve"]  = market_data["US10Y"] - market_data["US2Y"]  # 收益率曲线斜率
    features["credit_spread"]= market_data["HY_SPREAD"] - market_data["IG_SPREAD"]
    features["real_rate"]    = market_data["US10Y"] - market_data["INFLATION_1Y"]
    
    # 市场宽度维度
    features["breadth"]      = market_data["NYSE_ADV"] / (market_data["NYSE_ADV"] + market_data["NYSE_DEC"])
    features["new_highs"]    = market_data["NYSE_NH"] / (market_data["NYSE_NH"] + market_data["NYSE_NL"])
    
    # 资金流向
    features["spy_flow"]     = market_data["SPY_NET_FLOW"].rolling(10).mean()
    
    return features
```

### 6.4 检测模型

**首选：HMM（隐马尔可夫模型）**，捕捉状态转移概率，避免 Regime 频繁跳动：

```python
from hmmlearn import hmm
from sklearn.preprocessing import StandardScaler

scaler   = StandardScaler()
X_scaled = scaler.fit_transform(regime_features)

model = hmm.GaussianHMM(
    n_components=5,
    covariance_type="full",
    n_iter=1000,
    random_state=42,
)
model.fit(X_scaled)

# 获取每日 Regime 概率分布
regime_probs = model.predict_proba(X_scaled)  # shape: [T, 5]
current_regime = regime_probs[-1].argmax()
confidence = regime_probs[-1].max()

# 低置信度时维持上一状态（避免信号抖动）
if confidence < 0.60:
    current_regime = prev_regime
```

**辅助验证：规则引擎**（显式规则与 HMM 状态进行交叉验证，两者一致时置信度更高）：

```python
def rule_based_regime(vix, spy_1m, yield_curve):
    if vix > 35:
        return "R5_CRISIS"
    elif vix > 25 and spy_1m < -0.05:
        return "R4_BEAR"
    elif vix < 15 and spy_1m > 0.02:
        return "R1_BULL_LOW_VOL"
    elif 15 <= vix <= 25 and spy_1m > 0:
        return "R2_BULL_HIGH_VOL"
    else:
        return "R3_SIDEWAYS"
```

### 6.5 各 Regime 下的因子策略矩阵

| Regime | 上调权重的因子 | 下调权重的因子 | 仓位水平 | 换手限制 |
|--------|------------|------------|--------|---------|
| R1 低波牛市 | 动量、质量、小盘 | 低波动、反转 | 满仓 (90-100%) | 宽松 |
| R2 高波牛市 | 动量、成长 | 价值、高贝塔 | 中高仓 (70-85%) | 中等 |
| R3 震荡整理 | 价值、反转、质量 | 动量 | 中仓 (50-70%) | 严格 |
| R4 趋势熊市 | 低波动、质量、短卖 | 所有多头动量 | 轻仓 (20-40%) | 严格 |
| R5 危机恐慌 | 防御性 / 现金 | 全部进攻因子 | 空仓/对冲 (0-15%) | 暂停 |

---

## 七、TradingAgent 与观察池

### 7.1 信号聚合框架

TradingAgent 接收候选因子池的多维信号，结合当前 Regime 动态调整权重，输出截面排名评分：

```python
class TradingAgent:
    def __init__(self, candidate_pool, regime_model, weight_config):
        self.factors    = candidate_pool
        self.regime     = regime_model
        self.weights    = weight_config  # Dict[regime_id, Dict[factor_name, weight]]

    def compute_composite_score(self, factor_matrix: pd.DataFrame, date: str) -> pd.Series:
        current_regime = self.regime.predict(date)
        regime_weights = self.weights[current_regime]

        # 加权融合
        score = sum(
            factor_matrix[f] * regime_weights.get(f, 0)
            for f in self.factors
        )

        # 截面 Z-Score 标准化
        return (score - score.mean()) / (score.std() + 1e-8)

    def update_watchlist(self, scores: pd.Series, top_n=30,
                         min_adv=5e6, exclude_earnings_week=True) -> list:
        """
        从评分排名中生成观察池，叠加流动性过滤和财报周规避
        """
        candidates = scores.nlargest(top_n * 2).index.tolist()
        filtered   = [t for t in candidates
                      if self.liquidity_ok(t, min_adv)
                      and not (exclude_earnings_week and self.in_earnings_window(t))]
        return filtered[:top_n]
```

### 7.2 LLM 辅助层（可选升级）

在规则驱动的 TradingAgent 基础上，可叠加 LLM 辅助过滤层，处理定性信息：

```python
# 对观察池中的股票，LLM 逐一阅读最近 earnings call transcript
# 输出：sentiment_score（-1 到 +1）和 flag_keywords（如 guidance cut / beat）
# 只有 sentiment_score > -0.3 且无 negative_flag 的标的才保留在观察池中
```

这一层并非必须，但对于财报密集期（Earnings Season）的信号质量提升显著。

### 7.3 观察池进出机制

**进入条件**：综合评分截面排名 Top 15%；日均成交额 > $5M；非财报发布日前后 2 日内（规避事件风险）；当前 Regime 下该因子组合的历史 Sharpe > 0.8。

**退出条件**：评分跌出 Top 25%（设置迟滞区间，避免频繁进出）；触发个股止损线（-8%）；出现重大负面事件（由 LLM 监控 SEC 8-K 公告）；Regime 切换至 R4/R5 时清空进攻型标的。

**观察池规模**：20-40 只（资金量 $50K 取 20 只，$200K+ 取 40 只，确保每只持仓有意义且不过度分散）。

**换手控制**：预期年化换手率目标 < 400%（等同于平均持有期约 3 个月），高于此目标时自动提高退出评分门槛。

---

## 八、自研因子体系

### 8.1 最有价值的美股自研方向

| 方向 | 描述 | 数据可及性 | 预期 ICIR |
|------|------|---------|---------|
| 盈利预期修正 | 分析师 EPS 上调/下调方向和幅度 | 中（Nasdaq Data Link） | 0.5-0.8 |
| 短卖压力因子 | FINRA 短卖量占比变化 | 中（FINRA 免费但延迟） | 0.4-0.6 |
| 13F 聪明钱 | 顶级基金本季度新建仓/减仓 | 高（SEC EDGAR 免费） | 0.3-0.5 |
| 财报文本情绪 | 10-K/earnings call 的 LLM 情绪打分 | 高（SEC EDGAR 免费） | 0.3-0.5 |
| 期权 IV 偏斜 | Call/Put IV 差值变化（资金方向信号） | 中（需 Polygon Options） | 0.4-0.6 |
| 内部人交易 | Form 4 高管买卖时机 | 高（SEC EDGAR 免费） | 0.4-0.7 |
| 供应链传导 | 上游原材料价格变化对利润率的影响 | 中等 | 0.3-0.4 |
| 谷歌搜索趋势 | SV 搜索量变化作为零售关注度代理 | 高（PyTrends 免费） | 0.2-0.4 |

### 8.2 因子研究标准流程

每个自研因子必须经过以下严格验证流程才能进入候选池：

```
Step 1  假设陈述（一句话：为什么这个因子应该有效？）
Step 2  数据获取（确认时间戳对齐，无 Look-ahead Bias）
Step 3  因子计算（向量化，全市场覆盖）
Step 4  IC 分析（全市场 + 分行业 IC，覆盖至少 5 年）
Step 5  分层回测（五分位，检验单调性）
Step 6  换手率分析（换手 > 1000%/年 = 交易成本将吃掉 Alpha）
Step 7  行业/市值中性化（剔除暴露，确认因子独立性）
Step 8  与现有因子相关性检验（Spearman 相关 > 0.65 则不引入）
Step 9  样本外验证（最近 2 年 Hold-out，不得参与参数选择）
Step 10 容量分析（估算最大可承载资金量）
```

### 8.3 财报情绪因子示例

```python
from anthropic import Anthropic
import edgar

client = Anthropic()

def score_earnings_call(ticker: str, quarter: str) -> float:
    transcript = edgar.get_earnings_transcript(ticker, quarter)
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Analyze this earnings call transcript and return ONLY a JSON:
            {{"sentiment": <float -1 to 1>, "guidance": <"raised"|"maintained"|"lowered">, 
              "confidence": <"high"|"medium"|"low">}}
            
            Transcript excerpt: {transcript[:3000]}"""
        }]
    )
    result = json.loads(response.content[0].text)
    return result["sentiment"] * (1.3 if result["guidance"] == "raised" else 
                                   0.7 if result["guidance"] == "lowered" else 1.0)
```

---

## 九、防过拟合机制

### 9.1 系统性防护框架

过拟合在量化投资中有三种形式：参数过拟（对历史参数死记硬背）、状态过拟（因子只在特定市场状态下有效）、数据窥探（在样本外数据上隐性调参）。本系统针对这三种形式分别建立防护机制。

**第一层：因子层防护**

每个因子强制设置 2 年 Hold-out 样本外验证集，评估使用的 IC 数据与优化使用的 IC 数据必须完全分开。因子复杂度惩罚机制：参数越多（如深度学习模型）的因子，IC 门槛相应提高。RD-Agent 生成的因子在接入候选池前必须通过额外的人工审核，防止 LLM 的"创意过拟合"。

**第二层：Regime 防护（核心）**

分 Regime 独立验证因子有效性，要求每个因子在至少 3 个不同 Regime 中的 IC 均为正，而非只在一种状态下有效。这直接防止了状态性过拟合。Regime 标注本身也用独立数据验证（HMM 参数在 2010-2018 年拟合，2019 年以后做预测）。

**第三层：Walk-Forward 优化**

```
时间轴示例（美股，季度滚动）：

训练窗口  2015-2018  →  优化权重 W1  →  验证窗口  2019 H1
训练窗口  2015-2019  →  优化权重 W2  →  验证窗口  2019 H2
训练窗口  2015-2020  →  优化权重 W3  →  验证窗口  2020 H1
（滚步长 3 个月，训练窗口每次增加 3 个月）

规则：
  权重只由训练窗口内数据确定
  验证窗口用于评估但不用于调参
  禁止在看到验证结果后手动调整训练参数（数据窥探）
```

**第四层：绩效监控预警**

策略上线后设置"策略退化预警线"：当近 3 个月的日频 IR 连续低于历史均值 -1.5σ 时，自动降低仓位至 50% 并触发人工审查，而非盲目继续运行。

### 9.2 美股特有的过拟合风险

美股市场效率高于 A 股，因子有效期普遍较短（更快被套利），这意味着需要更高频率的因子更新（季度而非年度）；历史上有效的"小盘溢价"在 2000 年后近乎消失，提醒我们不可盲目依赖学术论文的因子回测结果；财报季的异质性极强（不同行业的财报效应不同），Regime 需要叠加"财报季"维度。

---

## 十、风险控制与执行层

### 10.1 仓位管理框架

```python
risk_config = {
    "max_portfolio_vol_annual": 0.18,   # 组合年化波动率上限 18%
    "max_single_stock_weight": 0.05,    # 单股最大权重 5%
    "max_sector_weight": 0.25,          # 单一 GICS 行业最大权重 25%
    "max_factor_beta": 0.3,             # 对任何单一因子的暴露量
    "max_drawdown_halt": 0.15,          # 触发暂停的最大回撤 15%
    "stop_loss_per_stock": 0.08,        # 单只止损线 -8%
}

# 日内保证金资格、普通/日内购买力、账户限制与做空权限必须来自 broker
# 的实时账户快照；策略层不得用资产规模或本地滚动交易次数推断。

# Regime 感知的仓位缩放
position_scale = {
    "R1": 1.0,   # 满仓
    "R2": 0.80,
    "R3": 0.65,
    "R4": 0.30,
    "R5": 0.05,  # 几乎空仓
}
```

### 10.2 交易成本模型

美股交易成本相对较低，但对于频繁换手的量化策略仍不可忽视：

| 成本项 | 估算 | 说明 |
|-------|------|------|
| 佣金 | $0（Alpaca）/ $0.005/股（IBKR） | Alpaca 免佣，IBKR 适合大资金 |
| 买卖价差 | 0.01-0.05%（大盘股） | S&P 500 成分股流动性极好 |
| 市场冲击 | 0-0.05%（日均成交额 0.5% 以内） | 小资金量几乎无冲击 |
| 滑点 | 约 0.02-0.05%（使用 VWAP 执行） | VWAP 单较好地控制冲击 |
| 总双边成本 | 约 0.05-0.15% | 目标换手 < 400%/年 → 年化成本 < 0.6% |

### 10.3 执行层设计（Alpaca API）

```python
import alpaca_trade_api as tradeapi

class AutoExecutor:
    def __init__(self, api_key, secret_key, base_url):
        self.api = tradeapi.REST(api_key, secret_key, base_url)

    def rebalance(self, target_weights: dict, current_holdings: dict):
        """
        差量执行：只交易需要调整的仓位，减少不必要的换手
        """
        for ticker, target_w in target_weights.items():
            current_w = current_holdings.get(ticker, 0)
            diff = target_w - current_w
            
            if abs(diff) < 0.005:  # 差异 < 0.5% 则跳过（避免碎股交易）
                continue
            
            portfolio_value = self.get_portfolio_value()
            dollar_amount   = abs(diff) * portfolio_value
            side = "buy" if diff > 0 else "sell"
            
            # 使用当日 VWAP 单（day + ioc）降低冲击成本
            self.api.submit_order(
                symbol=ticker, notional=dollar_amount,
                side=side, type="market", time_in_force="day"
            )
    
    def emergency_exit(self):
        """触发 R5 危机状态时，清空所有持仓"""
        self.api.close_all_positions()
```

### 10.4 税务优化（美股特有）

对于美国居民，持有超过 1 年的股票适用长期资本利得税率（0-20%），短期（< 1 年）适用普通所得税税率（高达 37%）。量化策略需要在组合优化目标中加入税务约束：在换手决策时，优先保留持有期接近 1 年的盈利仓位；止损单优先在短期持有的亏损仓位执行（可用于抵税）；避免触发 Wash Sale Rule（卖出亏损后 30 日内不得买入实质相同的标的）。对于非美国居民（如使用美股账户的海外投资者），此部分可忽略。

---

## 十一、技术栈

| 模块 | 推荐工具 | 版本/说明 |
|------|---------|---------|
| 数据获取 | Alpaca Python SDK + yfinance | 主备用架构 |
| 数据存储 | DuckDB + Parquet | 列存储，无需服务器 |
| 因子计算 | Qlib（Alpha158 + 表达式引擎） | Microsoft 开源 |
| 深度学习 | PyTorch 2.x + HuggingFace | 时序模型 |
| 时序基础模型 | Amazon Chronos-T5 / TimesFM | Kronos 模块 |
| LLM 驱动 | RD-Agent（官方）+ Claude API | 因子发现 + 情绪分析 |
| Regime 检测 | hmmlearn + scikit-learn | HMM + 聚类 |
| 回测框架 | Qlib Backtest / QuantConnect | Qlib 优先，QuantConnect 用于验证 |
| 组合优化 | PyPortfolioOpt / Riskfolio-Lib | 风险平价 / MVO |
| 任务调度 | APScheduler | 每日盘后自动运行 |
| 执行层 | Alpaca Trade API / IBKR API | 自动下单 |
| 监控看板 | Streamlit + Plotly | 本地 Dashboard |
| 版本控制 | Git + DVC | 数据和模型版本管理 |
| 运行环境 | Python 3.11，Conda + Docker | 确保可复现性 |

---

## 十二、实施路线图

### Phase 1：数据基础设施（第 1-2 周）

- 开通 Alpaca 账户（Paper Trading 模式），获取 API Key
- 搭建 DuckDB 数据库，拉取 S&P 500 成分股 2010 年至今历史数据
- 接入 FRED API，拉取 VIX、利率、宏观数据
- 完成数据清洗流程（复权、缺失值、流动性过滤）
- 验收标准：干净的历史数据库，日更自动化脚本运行无误

### Phase 2：Alpha158 基础因子（第 3-4 周）

- 配置 Qlib，接入 Alpaca 历史数据
- 计算全市场 Alpha158 因子矩阵
- 开发单因子 IC 评估和分层回测框架
- 筛选出 Alpha158 中在美股有效的因子子集（预期保留 50-70 个）
- 验收标准：Alpha158 子集平均 IC > 0.03，分层单调性检验通过率 > 60%

### Phase 3：Regime Switching 系统（第 5-6 周）

- 构建 Regime 特征工程（VIX + 宏观 + 市场宽度）
- 训练 HMM 模型，历史 Regime 标注（2010 至今）
- 规则引擎交叉验证
- 分 Regime 计算 Alpha158 因子 IC（验证各 Regime 下有效因子差异显著性）
- 验收标准：5 个 Regime 在样本外识别准确率 > 70%，各 Regime 下因子 IC 差异显著

### Phase 4：Kronos + RD-Agent 接入（第 7-9 周）

- 接入 Amazon Chronos，对 S&P 500 个股生成时序预测信号
- 验证 Kronos 信号的 IC 和与 Alpha158 的相关性（目标相关系数 < 0.4）
- 配置 RD-Agent + Qlib 集成环境
- 运行首批 LLM 自动因子发现（目标发现 5-10 个新因子）
- 扩充候选因子池，完成最终相关性筛选
- 验收标准：候选池 20-40 个因子，来源多样，两两相关系数均值 < 0.4

### Phase 5：TradingAgent + 观察池（第 10-11 周）

- 开发多因子加权评分系统（Regime 感知）
- Walk-Forward 优化框架，季度滚动优化权重
- 观察池进出机制开发
- 全系统端到端回测（2015-2022 训练，2023-2024 样本外验证）
- 验收标准：样本外年化超额 Alpha > 5%，Sharpe > 1.0，最大回撤 < 20%

### Phase 6：执行层 + 监控（第 12 周）

- 接入 Alpaca API，完成 Paper Trading 模拟对接
- 每日自动化流程：盘后数据更新 → 因子计算 → 信号生成 → 下单
- Streamlit 监控 Dashboard（持仓、绩效、因子暴露、Regime 状态）
- 风险预警机制（回撤触发 / Regime 切换通知）
- 验收标准：Paper Trading 运行 4 周无错误，绩效与回测偏差 < 30%

### Phase 7：实盘上线 + 自研因子迭代（长期）

- 小仓位实盘（建议从 $10K 开始，验证模拟与实盘的差距）
- 每月 RD-Agent 自动运行一次因子发现流程
- 每季度因子审查和 Walk-Forward 更新
- 持续补充自研因子（内部人交易、期权信号、财报情绪等）

---

## 十三、关键风险与应对

| 风险 | 影响程度 | 应对策略 |
|------|---------|---------|
| Look-ahead Bias | 极高 | 财务数据强制使用发布日；禁止在时间切片中使用未来数据；代码审查清单 |
| 因子拥挤 | 高 | 监控因子 IC 衰减斜率；保持因子来源多样化；Regime 切换时降低热门因子权重 |
| Regime 误判 | 中高 | HMM + 规则引擎双重确认；低置信度时维持上一状态；误判时自动降仓 |
| 市场流动性突变 | 中高 | 实时监控 bid-ask spread；危机 Regime 下降低换手；持仓 ADV < 1% |
| Alpaca API 故障 | 中 | IBKR 作为备用执行渠道；关键单子设置手机短信通知 |
| 券商日内交易资格与购买力变化 | 中高 | broker 实时 day-trade 资格、普通/日内购买力、保证金状态与账户限制是唯一事实源；策略层不得按资产规模或本地交易次数推断 |
| 模型过拟合 | 高 | 全链路 Walk-Forward；Regime 独立验证；定期样本外检验 |
| 监管/税务变化 | 低 | 保留完整的交易日志；使用正规券商 |

---

## 十四、附录：关键参数参考

| 参数 | 建议值 | 说明 |
|------|-------|------|
| 投资域 | S&P 500 + 流动性过滤后 ~600 只 | 起步阶段足够 |
| 候选因子池规模 | 20-40 个 | 过多导致过拟合，过少信号不足 |
| IC 门槛 | ≥ 0.025（美股日频） | 美股效率高，门槛略低 |
| ICIR 门槛 | ≥ 0.35 | |
| Regime 状态数 | 5 | 牛低波 / 牛高波 / 震荡 / 熊 / 危机 |
| HMM 置信度 | ≥ 0.60 | 低于此值维持上一状态 |
| 观察池规模 | 20-40 只 | 取决于资金量 |
| Walk-Forward 步进 | 3 个月 | 每季度重优化权重 |
| 最大单股权重 | 5% | |
| 最大行业权重 | 25% | GICS 一级行业 |
| 目标年化换手 | < 400% | 平均持有期约 3 个月 |
| 最大回撤触发 | 15% | 触发人工审查和仓位减半 |
| 历史数据深度 | ≥ 10 年（2015 年至今） | 覆盖 2018/2020/2022 三个熊市 |

---

*文档版本：v1.0 ｜ 创建日期：2026-06 ｜ 本文档为个人量化研究用途，不构成投资建议*
