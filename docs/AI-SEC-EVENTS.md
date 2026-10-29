# SEC 8-K 历史归档与事件研究

这一模块提供两步能力：按完整季度索引归档指定公司的 8-K / 8-K/A；用本地历史价格研究事件可得之后的固定交易日窗口。它与现有「最近两份 8-K 差分」并列，后者不作为完整历史事件数据库。

## 归档

`sec-archive` 需要明确日期、CIK 和 SEC 要求的 User-Agent 名称与联系邮箱，不使用当前股票代码映射反推历史公司池。示例中的联系信息必须替换为使用者自己的有效信息：

```bash
.venv/bin/quantbot sec-archive \
  --start 2024-12-01 --end 2025-03-31 \
  --cik 0000320193 0000789019 \
  --user-agent '你的研究名称 你的联系邮箱' \
  --directory runs/sec-archive
```

一次最多跨 8 个季度、20 个 CIK、200 份命中文件；索引结束日须早于今天。遍历开始到结束的每个季度，包含跨年和季度边界，再以 filing date 精确过滤。命中超过上限会失败，不截断为貌似完整的股票池。单个归档器顺序请求，至少间隔 0.2 秒；不自动重试 403/429 或跳过缺失季度。

SEC 的季度索引与当季 full index、索引修订和请求要求见 [Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)。公司 submissions API 只返回最近一段历史，更多历史在附加文件中；不能只用 `filings.recent` 代表全量，见 [SEC API 说明](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。本模块直接使用季度索引，未采用 recent 截断列表。

归档目录内容：

- `edgar.sqlite`：按 accession 和正文哈希存储文件版本及首次取得时间。同样版本再次下载不改变首次取得时间；内容变化会新增版本。
- `blobs/<sha256>`：不可变原始响应，重复内容校验后复用。
- `runs/<ID>/capture.json`：覆盖季度、请求来源、哈希、成功文件数和失败记录。
- 同目录 `bundle.json`：可移植事件数据包，含原始完整文件、解析信息、覆盖范围和全部已观察版本。失败采集标记 `coverage.complete=false`，事件研究拒绝将其当作完整样本。

解析时核对 accession、发行人 CIK、form 和 filing date；从完整 submission 选择类型匹配的主文档，避免误取附件。accession 前十位可能是代理申报人，不能当作股票发行人 CIK。8-K/A 单独保留，不凭“同一公司最新文件”猜测修订关系。

## 三种时间依据

受理时间从 SGML `ACCEPTANCE-DATETIME` 读取，按 `America/New_York` 转为 UTC，保留原始值并处理夏令时。官方明确说明：受理时间与首次公开时间不同，公开延迟没有保证，网页没有首次公开时间字段，见 [SEC 时间戳与公开延迟 FAQ](https://www.sec.gov/files/about/webmaster-faq.htm)。

| 模式 | 研究使用的时点 | 边界 |
|---|---|---|
| `observed`，默认 | 本地首次成功取得该版本的时间 | 今天下载的旧文件不会自动成为过去已知的信息 |
| `declared` | 数据包逐条声明的 `public_available_at` | 必须附 `availability_evidence`，时间不能早于受理/修订日期或晚于首次取得；来源仍未独立认证 |
| `acceptance_proxy` | 受理时间加预设延迟，且不早于 filing date 当地零点 | 仅在显式选择时启用，默认假定延迟 15 分钟；不是 SEC 保证。后续正文版本及已标记事后修订使用实际首次取得时间 |

对相同 accession，过去的内容不被后来修订覆盖；可见版本查询只返回当时可得的最新版本。默认不把 8-K/A 再当成一次普通 8-K 信号；如需研究修订公告，可事先选择纳入。`amends_accession` 可显式声明，但必须指向同一公司更早的申报，不自动推断关系。

## 回放与输出

网页「运行中心 → AI 量化交易 → SEC 历史事件研究」可导入归档和行情、填写有有效日期的公司/股票映射，选择基准、窗口和可得时间模式。点击「加载跨季度合成样例」即可离线验证；样例中的文件和价格均为合成。

```bash
.venv/bin/python docs/examples/validate_sec_events.py --output runs/my-sec-example

.venv/bin/quantbot sec-study \
  --events runs/my-sec-example/bundle.json \
  --prices runs/my-sec-example/prices.json \
  --universe runs/my-sec-example/universe.json \
  --policy runs/my-sec-example/policy.json \
  --output runs/my-sec-replay
```

`universe.json` 为带 `cik`、`symbol`、`start`、`end` 的数组。同一 CIK 的日期范围不可重叠，股票必须有完整行情；不使用现在的 ticker 列表充当历史映射。`prices.json` 顶层为 `schema_version=1`、`data_kind`、`source`、`captured_at`、`price_basis="raw"`、`bars`、`dividends`、`actions=[]`。股价行使用 `date,symbol,open,high,low,close,volume,available_at`；分红行使用 `symbol,ex_date,amount`。最多 1500 个完整交易日、20 个股票与一个基准，总输入 64 MB；拆股、退市和合并会计尚未支持。

默认获知后至少延迟 60 秒，寻找真实交易日历中的下一次开盘。行情必须包含该次开盘，不能把更早事件推迟到行情首日；周末、假日和夏令时按 XNYS 处理。窗口从入场开盘到第 N 个交易日收盘，计双边成本与买入日之后的持有期分红，不用复权价格重复加分红。

同一标的同一入场日的多个公告合并为一个事件组；默认按最大观察窗口排除该标的重叠事件。尾部不完整窗口单独排除，完整的较短窗口仍可保留。报告列出每个窗口实际事件数量、排除原因、净收益、同期基准、超额百分点和成本加倍结果。

日期聚类自助抽样使用固定种子，至少 5 个入场日期组才输出区间。其中心是等权日期组平均超额，不一定等于按事件数加权平均；它不能消除跨日相关、公司池选择和反复试验偏差。

API：`POST /api/quantbot/sec-study` 接收 `bundle,prices,universe,policy`；`GET /example` 返回显式样例；响应 `download_url` 下载带完整事件原文与行情的报告。CLI 与网页都在执行前冻结输入、策略和源码哈希，技术完成与研究结论分开。

## 尚未完成

当前是独立事件窗口描述统计，尚不是受现金、持仓和订单约束的组合策略回测；没有因果结论、生产事件订阅、完整历史证券主表、真实数据源可得性认证或 LLM 事件决策。条目从主文档规则提取，不能替代人工/模型语义核验。事件与期权研究尚未接入通用研究卡片的留出门禁。合成数据结论固定为 `insufficient`，历史数据为 `not_assessed`，不会自动启用交易。

本批验证记录见 [AI-SEC-VALIDATION.md](AI-SEC-VALIDATION.md)。
