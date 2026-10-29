/**
 * Config-driven feature guides for tab and sub-tab walkthroughs.
 */
import type { FeatureGuide } from "../components/guides/guideTypes";

const featureGuides = {
  "market.chart": {
    title: "走势图操作指南",
    summary: "这里是看盘主工作台，适合做查行情、切周期、叠指标和快速下单。",
    goal: "先把标的和周期看对，再决定要不要继续下单或切去别的分析模块。",
    steps: [
      {
        title: "先确认看的是什么标的",
        description: "在图表顶部输入代码并点“查询”，先把图切到你真正想看的股票或资产。",
        tips: ["如果刚换过自选股，这里会自动同步当前标的。"],
      },
      {
        title: "再选时间周期和指标",
        description: "先从底部时间周期切到 1D、1H 或 5m，再按需要打开 EMA、布林带或成交量。",
        tips: ["长周期看趋势，短周期看入场节奏。"],
      },
      {
        title: "需要动作时再联动右侧功能",
        description: "看完图之后，可以去右侧自选区换标的，或者直接用快捷下单进入交易执行流程。",
        tips: ["如果数据不够新，可以点顶部刷新按钮重新拉行情。"],
      },
    ],
    quickTips: ["先看趋势，再看位置，最后才考虑交易动作。"],
  },
  "market.sentiment": {
    title: "新闻情绪操作指南",
    summary: "这里用来快速判断最近新闻偏多还是偏空，适合做辅助判断，不建议单独当作买卖依据。",
    goal: "先看整体情绪，再回到图表确认价格有没有真的响应。",
    steps: [
      {
        title: "先输入想看的标的",
        description: "左侧选择标的、时间范围和新闻数量，先把分析范围缩到你真正关心的资产。",
      },
      {
        title: "先看聚合结果，再看新闻明细",
        description: "右侧先看总分、趋势和分布，再往下看每条新闻的来源、发布时间和情绪标签。",
        tips: ["如果总分很强但价格没动，说明情绪还没形成有效共识。"],
      },
      {
        title: "把情绪当作辅助证据",
        description: "当情绪和图表方向一致时，结论更稳；如果两边明显打架，优先重新核查行情和风险。",
      },
    ],
  },
  "market.live": {
    title: "实时推送操作指南",
    summary: "这里适合盯实时更新的数据流，快速看到最新行情、事件或系统推送。",
    goal: "确认现在推过来的内容是不是你在意的资产和时间窗口。",
    steps: [
      {
        title: "先看推送是否在更新",
        description: "进入后先确认列表在刷新，有没有新的时间戳、价格或提示消息。",
      },
      {
        title: "遇到噪音时先筛重点",
        description: "先关注价格异动、重要事件和异常提醒，不要一条条细读全部更新。",
      },
      {
        title: "需要深挖时切回其他模块",
        description: "看到有价值的推送后，回图表看走势，或切去交易/选股继续处理。",
      },
    ],
    quickTips: ["实时推送更像雷达，不是最终决策面板。"],
  },
  "market.chart.main": {
    title: "主图区域操作指南",
    summary: "这块是你最常用的看盘区，负责查标的、切周期、看 K 线和叠加指标。",
    goal: "用最少的步骤把图切到对的状态。",
    steps: [
      {
        title: "输入代码并查询",
        description: "顶部输入标的代码后点“查询”，先确认主图显示的是对的资产。",
      },
      {
        title: "切周期看节奏",
        description: "底部切换时间周期，长周期看趋势，短周期看入场点和波动节奏。",
      },
      {
        title: "按需打开指标",
        description: "只有当你真的需要验证趋势或波动时，再打开 EMA、布林带和成交量，避免图上信息太杂。",
      },
    ],
  },
  "market.chart.watchlist.default": {
    title: "默认关注列表操作指南",
    summary: "这里是系统默认的关注标的区，适合快速切换常看资产。",
    goal: "一键切换到下一个想看的标的。",
    steps: [
      {
        title: "先找标的代码",
        description: "列表会显示系统内置热门标的和已拉过价格的数据。",
      },
      {
        title: "点一下直接换图",
        description: "点击任意标的后，主图会立刻切换过去，不需要再手动输入。",
      },
      {
        title: "用它做快切，不做长期收藏",
        description: "如果你要维护自己的长期盯盘池，建议切到“自选股”标签管理。",
      },
    ],
  },
  "market.chart.watchlist.custom": {
    title: "自选股操作指南",
    summary: "这里适合维护你自己的盯盘列表，方便长期跟踪固定标的。",
    goal: "建立一组你每天都会反复切换查看的自定义标的。",
    steps: [
      {
        title: "先点击添加自选股",
        description: "底部点“添加自选股”，输入代码后保存，就能把标的放进自己的列表。",
      },
      {
        title: "点击列表快速切图",
        description: "和默认列表一样，点一下就能切换主图，不用重复搜索。",
      },
      {
        title: "不需要的及时移除",
        description: "鼠标移上去后点删除图标，把已经不关注的标的清掉，列表会更干净。",
      },
    ],
  },
  "market.chart.order": {
    title: "快捷下单操作指南",
    summary: "这块用来从图表旁边进入交易执行，适合边看行情边处理下单，不用跳出看盘页。",
    goal: "先确认可交易，再提交数量合适的订单。",
    steps: [
      {
        title: "先看标的是否已映射",
        description: "如果这里显示“未映射”或提示不支持，先去交易页用完整代码确认标的。",
      },
      {
        title: "选订单类型并填数量",
        description: "市价单更快，限价单更可控；填数量前先看最大可买和最大可卖。",
      },
      {
        title: "提交前看一眼提示",
        description: "如果当前是 mock fallback、休市或数量不合法，面板会先给出提醒，再决定要不要提交。",
      },
    ],
    quickTips: ["在图表页下单更适合快反，不适合做复杂组合交易。"],
  },
  "market.chart.news": {
    title: "新闻资讯操作指南",
    summary: "这里展示和当前标的相关的新闻，帮助你快速知道最近发生了什么。",
    goal: "先筛出有影响的消息，再决定是否继续深挖。",
    steps: [
      {
        title: "先看标题和时间",
        description: "先扫一眼新闻标题和发布时间，判断是不是刚发生的关键事件。",
      },
      {
        title: "优先看情绪标签",
        description: "利好、利空和中性标签能帮你先做第一层过滤，避免被冗长新闻拖慢。",
      },
      {
        title: "和价格一起看",
        description: "如果新闻很强但价格没反应，说明市场还没真正交易这条消息。",
      },
    ],
  },
  "market.chart.positions": {
    title: "持仓记录操作指南",
    summary: "这里是图表页下方的成交/持仓历史，适合边复盘边对照图形位置。",
    goal: "把历史成交放回到当时的价格环境里看。",
    steps: [
      {
        title: "先确认有没有记录",
        description: "如果当前为空，先去交易执行页完成订单并成交，这里才会开始出现记录。",
      },
      {
        title: "重点看方向、价格和时间",
        description: "把买卖方向、成交价和成交时间对上主图，能快速复盘你的操作质量。",
      },
      {
        title: "把它当复盘线索",
        description: "这块适合回答“我当时为什么在这里买/卖”，不适合做复杂报表统计。",
      },
    ],
  },
  "strategy.code": {
    title: "代码编辑操作指南",
    summary: "这里是策略工坊的主入口，适合创建、编辑、保存和检查策略代码。",
    goal: "先把策略骨架写出来，再保存，再去回测验证。",
    steps: [
      {
        title: "先选现有策略或新建策略",
        description: "左侧选择已有策略，或点加号新建一份空白策略，从明确的起点开始。",
      },
      {
        title: "在编辑器里改代码和描述",
        description: "先写策略名称、描述和核心逻辑，保存状态会告诉你当前有没有未保存修改。",
      },
      {
        title: "保存后再跑回测",
        description: "不要一边改一边猜结果，先点保存，再用右上角按钮跳去回测页验证。",
      },
    ],
  },
  "strategy.optimize": {
    title: "参数优化操作指南",
    summary: "这里适合比较不同参数组合的表现，找出更稳而不是只看收益最高的参数。",
    goal: "先定范围，再看结果分布，而不是盯住单个最佳值。",
    steps: [
      {
        title: "先缩小参数范围",
        description: "不要一上来把范围拉太大，先锁定你最怀疑的几个关键参数。",
      },
      {
        title: "看结果时兼顾收益和回撤",
        description: "收益高但回撤也很夸张的组合，不一定适合继续推进。",
      },
      {
        title: "优化后回测复核",
        description: "挑出候选参数后，回到回测页重新跑一次完整验证，别直接拿最优参数当真理。",
      },
    ],
  },
  "strategy.ml": {
    title: "机器学习操作指南",
    summary: "这里适合试验 ML 策略思路，但更像研究台，不建议直接当作可交易结论。",
    goal: "先把模型当成辅助信号源，而不是自动赚钱机器。",
    steps: [
      {
        title: "先看模型能解决什么问题",
        description: "确认你是想做方向判断、分类筛选，还是概率排序，不同目标不要混在一起。",
      },
      {
        title: "先验证输入和输出",
        description: "看清模型用了什么数据、输出了什么信号，再决定它能不能接入你的策略流程。",
      },
      {
        title: "最后回到回测验证",
        description: "无论模型说得多好，只有实盘前回测验证能说明它值不值得继续推进。",
      },
    ],
  },
  "trading.execution": {
    title: "交易执行操作指南",
    summary: "这里是统一的交易执行工作台，适合看账户、查委托、处理订单和做复盘。",
    goal: "先查标的，再估算，再下单，最后复查结果。",
    steps: [
      {
        title: "先确认账户和标的都准备好了",
        description: "顶部先看账户概况，再搜索标的，别在标的还没解析出来时就直接下单。",
      },
      {
        title: "下单前先看估算信息",
        description: "数量、价格、订单类型和最大可买可卖都先看一遍，避免提交后才发现条件不对。",
      },
      {
        title: "下单后去账户明细复查",
        description: "订单提交后，马上去持仓、委托、成交和流水几个标签确认系统到底发生了什么。",
      },
    ],
  },
  "trading.signals": {
    title: "交易信号操作指南",
    summary: "这里适合集中看系统给出的买卖信号，但信号本身不等于最终执行。",
    goal: "先筛信号，再回图表和交易页确认。",
    steps: [
      {
        title: "先看哪些信号最值得处理",
        description: "优先处理最新、强度更高或和你当前持仓有关的信号。",
      },
      {
        title: "不要直接照单全收",
        description: "看到信号后，先回看走势、成交量和风险条件，确认它不是噪音。",
      },
      {
        title: "决定执行时去交易页",
        description: "想处理信号时，先回到图表、回测和风控条件确认，再进入交易执行。",
      },
    ],
  },
  "trading.execution.positions": {
    title: "持仓标签操作指南",
    summary: "这里专门看你现在拿着什么仓位，以及这些仓位赚了还是亏了。",
    goal: "先确认仓位结构，再决定要不要调仓或快速卖出。",
    steps: [
      {
        title: "先看持仓和可卖数量",
        description: "持仓数量告诉你总仓位，可卖数量告诉你现在能不能立刻卖出。",
      },
      {
        title: "再看成本价和最新价",
        description: "把成本和最新价对照起来，你能很快知道这笔仓位现在处在哪个位置。",
      },
      {
        title: "需要减仓时用快速卖出",
        description: "如果这笔仓位已经不想继续持有，可以直接用右侧按钮发起卖出动作。",
      },
    ],
  },
  "trading.execution.orders.today": {
    title: "今日委托操作指南",
    summary: "这里看今天还在处理中的委托，适合追踪订单有没有排队、成交或撤掉。",
    goal: "盯住今天的订单状态变化。",
    steps: [
      {
        title: "先看状态列",
        description: "优先关注“待提交”“已提交”“部分成交”这些还没真正结束的状态。",
      },
      {
        title: "需要时点单查看详情",
        description: "点一条委托就能在下方看到更完整的订单明细。",
      },
      {
        title: "状态不对就及时撤单",
        description: "如果订单方向、价格或时机已经不合适，直接在右侧做撤单处理。",
      },
    ],
  },
  "trading.execution.orders.history": {
    title: "历史委托操作指南",
    summary: "这里用来复查过去的委托记录，适合排查你以前到底是怎么下单的。",
    goal: "把过去的下单行为复盘清楚。",
    steps: [
      {
        title: "先按分页慢慢翻",
        description: "历史记录多时不要急着找，先按页往后翻，逐步定位大概时间段。",
      },
      {
        title: "重点看订单类型和状态",
        description: "同一个标的，用市价还是限价、最后是成交还是失效，能反映你当时的执行方式。",
      },
      {
        title: "需要细节就看订单详情",
        description: "点击具体订单后，下方会显示完整时间、数量、价格和备注。",
      },
    ],
  },
  "trading.execution.executions.today": {
    title: "当日成交操作指南",
    summary: "这里专门看今天已经成交的订单，适合确认实际成交价和执行时间。",
    goal: "判断今天的真实成交质量。",
    steps: [
      {
        title: "先看方向和价格",
        description: "买入还是卖出、成交价是多少，是判断执行质量最直接的两列。",
      },
      {
        title: "再看成交时间",
        description: "把成交时间和图表对起来，你能看到自己是不是买在冲高、卖在回落。",
      },
      {
        title: "当天问题当天复盘",
        description: "今天的成交最有记忆，最好当天就回头检查有没有追高、慢半拍或误操作。",
      },
    ],
  },
  "trading.execution.executions.history": {
    title: "历史成交操作指南",
    summary: "这里适合回头统计过去真实成交过的单子，看自己长期执行有没有偏差。",
    goal: "复盘长期成交质量，而不是只看单次结果。",
    steps: [
      {
        title: "先按标的或时间段回想",
        description: "带着问题来看，比如“上周某只股票为什么成交这么差”，会更容易找到线索。",
      },
      {
        title: "看价格和方向是否符合计划",
        description: "如果原计划是低吸，但历史成交总在高位追进去，问题通常不在策略本身。",
      },
      {
        title: "把发现带回策略或下单流程",
        description: "历史成交更像执行审计，看到重复问题后要回去修流程，而不是只做情绪复盘。",
      },
    ],
  },
  "trading.execution.cashflows": {
    title: "资金流水操作指南",
    summary: "这里看账户资金怎么变化，适合确认每一笔交易或账户动作有没有真正反映到账上。",
    goal: "看清资金进出，而不是只盯持仓盈亏。",
    steps: [
      {
        title: "先看金额和余额",
        description: "金额告诉你这一笔动了多少钱，余额告诉你动完以后账户还剩多少可用资金。",
      },
      {
        title: "再看业务类型和说明",
        description: "买卖成交、费用、资金调整等类型不同，解释也不同，先看清楚是什么动作。",
      },
      {
        title: "对异常流水及时核查",
        description: "如果金额变化和你的理解不一致，先回到成交和委托标签交叉核对。",
      },
    ],
  },
  "backtest.workspace": {
    title: "回测操作指南",
    summary: "这里适合做实盘前验证，先看策略过去表现，再决定值不值得进入交易执行。",
    goal: "先把参数配对，再跑，再读结果。",
    steps: [
      {
        title: "先选策略和标的",
        description: "左侧先选好策略、标的和周期，别用错标的去验证不匹配的策略。",
      },
      {
        title: "再配资金和成本参数",
        description: "初始资金、手续费、滑点和止盈止损都会影响结果，先尽量接近真实情况。",
      },
      {
        title: "结果出来先看回撤和胜率",
        description: "不要只盯总收益，先看最大回撤、波动和交易次数，判断它是不是能长期拿得住。",
      },
    ],
    quickTips: ["回测好看，只说明过去不错；能不能进入实盘，还要看稳定性和风险承受。"],
  },
  "options.payoff": {
    title: "盈亏图操作指南",
    summary: "这里用来直观看期权到期时可能赚多少、亏多少，适合先理解结构再谈交易。",
    goal: "先知道这张期权在不同价格下的结局。",
    steps: [
      {
        title: "先填基础参数",
        description: "把标的现价、行权价、到期时间、利率和波动率填对，盈亏图才有参考价值。",
      },
      {
        title: "看零轴和行权价",
        description: "零轴告诉你盈亏分界，虚线行权价告诉你标的在哪个位置开始出现关键变化。",
      },
      {
        title: "把盈亏图当结构说明书",
        description: "它最适合回答“这单大概怎么赔、怎么赚”，不负责预测一定会发生什么。",
      },
    ],
  },
  "options.sensitivity": {
    title: "Greeks 曲线操作指南",
    summary: "这里用来观察 Delta、Gamma、Vega 等指标怎么随标的价格变化。",
    goal: "先看风险敏感度，再决定要不要继续交易。",
    steps: [
      {
        title: "先选一个你关心的指标",
        description: "如果你想看方向暴露就看 Delta，想看波动率影响就看 Vega，不要一次什么都想看。",
      },
      {
        title: "再看当前价格在哪个位置",
        description: "图上的现价线能告诉你，现在这张期权正落在风险曲线的哪一段。",
      },
      {
        title: "最后判断风险是否可承受",
        description: "如果曲线太陡，说明价格稍微一动风险就会大变，这时仓位要更保守。",
      },
    ],
  },
  "options.scenario": {
    title: "情景矩阵操作指南",
    summary: "这里把标的价格和波动率同时变化的结果摊开来看，适合做情景推演。",
    goal: "提前知道不同市场组合下大概会发生什么。",
    steps: [
      {
        title: "先把当前参数算出来",
        description: "先完成基础参数计算，情景矩阵才会基于你当前这套设定展开。",
      },
      {
        title: "横着看价格，竖着看波动率",
        description: "列通常代表标的价格变化，行代表波动率变化，按这个思路读就不容易乱。",
      },
      {
        title: "优先关注靠近当前值的格子",
        description: "离当前价格和波动率太远的场景，更像极端推演；先看近处更有现实意义。",
      },
    ],
  },
  "system.alerts": {
    title: "价格告警操作指南",
    summary: "这里适合设置盯盘提醒，让你不用一直盯着屏幕。",
    goal: "把真正重要的提醒设出来，降低无效盯盘。",
    steps: [
      {
        title: "先选标的和触发条件",
        description: "先决定你要盯的是价格上破、下破还是别的阈值，再创建规则。",
      },
      {
        title: "通知不要设太多",
        description: "只保留高价值规则，不然提醒太频繁，真正重要的告警反而会被淹没。",
      },
      {
        title: "触发后回工作台确认",
        description: "告警只负责把你叫回来，真正的判断还是要回图表和交易页做。",
      },
    ],
  },
  "screener.results": {
    title: "选股结果操作指南",
    summary: "这里会列出评分后的候选股票，适合先做粗筛，再深入分析。",
    goal: "先找到值得继续看的票，而不是直接决定买卖。",
    steps: [
      {
        title: "先看总分和是否命中",
        description: "总分高说明多个维度都更靠前，命中标签说明它符合你当前筛选条件。",
      },
      {
        title: "点中候选股再看详情",
        description: "选股结果只是入口，点击后继续去“个股详情”看分解原因。",
      },
      {
        title: "不要只看一项指标",
        description: "趋势、量能、MACD 和 RSI 一起看，才更容易避免误判。",
      },
    ],
  },
  "screener.market": {
    title: "市场概况操作指南",
    summary: "这里适合先判断大盘环境，再决定今天值不值得去做选股和执行。",
    goal: "先看市场大势，再看个股。",
    steps: [
      {
        title: "先看市场强弱",
        description: "最上面的强势/中性/弱势标签，能帮你先判断今天环境偏顺风还是逆风。",
      },
      {
        title: "再看指数和板块",
        description: "指数告诉你大盘方向，领涨领跌板块告诉你钱主要往哪里走。",
      },
      {
        title: "把市场环境带回选股结果",
        description: "如果市场很弱，就算个股分数不错，也要更谨慎地看后续执行。",
      },
    ],
  },
  "screener.detail": {
    title: "个股详情操作指南",
    summary: "这里用来看某只股票为什么得这个分，适合做二次筛选。",
    goal: "先知道分数怎么来的，再决定这只票值不值得继续跟。",
    steps: [
      {
        title: "先看综合评分",
        description: "综合评分是总入口，先判断它是高分候选还是只勉强过线。",
      },
      {
        title: "再看因子分解",
        description: "趋势、量能、支撑、MACD、RSI 分别贡献多少，能帮你找到强弱项。",
      },
      {
        title: "最后看筹码结构",
        description: "平均成本、盈利比例和支撑位，能帮助你判断这只票现在是不是容易承压。",
      },
    ],
  },
  "screener.analyze": {
    title: "AI 分析操作指南",
    summary: "这里会把选中的股票做多阶段 AI 解读，适合拿来整理思路，而不是机械执行。",
    goal: "让 AI 帮你串起数据、指标和建议。",
    steps: [
      {
        title: "先选股票再启动分析",
        description: "没有选中股票时，这块不会开始工作，所以先在选股结果里点中一只票。",
      },
      {
        title: "按阶段看输出",
        description: "分析会分阶段推进，建议按市场数据、技术指标、情报收集、投资建议的顺序阅读。",
      },
      {
        title: "重点看最后的理由",
        description: "不要只看 BUY / HOLD / SELL，重点看 AI 为什么这么判断，理由比结论更重要。",
      },
    ],
  },
} satisfies Record<string, FeatureGuide>;

export type FeatureGuideKey = keyof typeof featureGuides;

export const FEATURE_GUIDE_KEYS = Object.keys(featureGuides) as FeatureGuideKey[];

export function getFeatureGuide(key: FeatureGuideKey): FeatureGuide {
  return featureGuides[key];
}

export { featureGuides };
