# 数据源调研纪要（2026-07-26）

> 由联网调研整理，供 `decisions.md` 决策一/二引用。价格以调研时官网为准。

## 1. Norgate Data

- **价格**（US Stocks 包）：Silver $270/年（仅现存股票，近 10 年）；Gold $360/年
  （近 20 年 + 基本面）；**Platinum $630/年——含退市股与历史指数成分，
  数据回溯至 1990**；Diamond $787.5/年（回溯至 1950）。
- **退市股**：约 25,222 只美股退市证券（1950 年起）。
- **复权**：Python 端可选 NONE / CAPITAL（仅拆股）/ TOTALRETURN（含分红，默认），
  复权口径固定、**不随刷新漂移**。
- **Python 接入**：`norgatedata` PyPI 包，但**必须本机运行 Norgate Data Updater，
  仅支持 Windows**（macOS 需 Parallels/VM——本项目在 Mac 上开发，这是关键限制）。
- **指数成分时间序列**：`index_constituent_timeseries()` 逐日标记；
  覆盖 S&P 500（1957 起）、**S&P MidCap 400（1991 起）**、SmallCap 600、
  Russell 系列、Nasdaq-100——需 Platinum 及以上。

## 2. EODHD

- **价格**：EOD All World **$19.99/月**（年付约 $199/年），30+ 年日线、
  含拆分/分红/复权、含退市数据；限额 10 万次/天。纯 REST API，Mac 无缝。
- **退市覆盖分层**：2018 年前退市的仅有价格数据（日线回测够用），
  2018 年后含基本面。
- **指数成分**：S&P 500 历史成分免费自带（含进出日期）；
  **S&P 400 历史成分需 Marketplace 附加包 $29.99/月，且仅约 12 年历史**。

## 3. 免费 S&P 400 点时成分数据

**结论：不存在 fja05680/sp500 级别的开源等价物。**
最接近的替代：(a) Wikipedia 变更表 DIY——仅回溯到 2012 且自称不完整；
(b) 用 MDY/IJH ETF 历史持仓年报重建；(c) 付费（Norgate 1991 起最全）。

## 4. 推荐

- **一步到位**：Norgate Platinum（$630/年），三个痛点全解，
  但 Mac 用户需要 Windows 虚拟机常驻——运维成本不可忽视。
- **性价比折中（推荐起点）**：EODHD $19.99/月 补退市股日线 + 固定复权口径；
  S&P 500 历史成分免费自带；S&P 400 扩展再议。
- **继续 yfinance**：零成本，三个痛点靠快照/别名/披露缓解（现状）。

## 出处

- Norgate 套餐: https://norgatedata.com/stockmarketpackages.php
- Norgate 数据内容: https://norgatedata.com/data-content-tables.php
- norgatedata 包: https://pypi.org/project/norgatedata/
- EODHD 定价: https://eodhd.com/pricing
- EODHD 退市股说明: https://eodhd.com/financial-apis/delisted-stock-companies-data-2
- EODHD S&P 400 附加包: https://eodhd.com/marketplace/unicornbay/spglobal
- Wikipedia S&P 400 变更表: https://en.wikipedia.org/wiki/List_of_S%26P_400_companies
- 现用点时方案: https://github.com/fja05680/sp500
