# AI 合约方向信号系统 V1 开发与部署规格

> 用途：本文件交给 VPS 上的 Codex，要求其直接完成项目开发、Docker 化、数据库初始化、数据源接入、AI 分析链路、REST 接口与部署。
>
> 核心边界：**本项目只负责产生 AI 方向信号，不负责真实交易执行。** 不实现交易所账户登录、下单、仓位管理、杠杆、TP/SL、保证金管理等逻辑。交易系统由其他项目负责，只需要消费本项目的标准化信号。

---

## 1. 项目目标

开发一套部署在 Linux VPS 上、持续运行的 AI 加密货币合约方向分析系统。

系统从多种公开/授权信息源中先做排行榜过滤与信息发现，再采集、清洗、结构化信息；随后用 AI 从候选信息中选出最值得分析的 10 个币种并为每个币种生成独立摘要；市场数据采集器只针对这 10 个币种采集实时合约市场数据；最终 AI 将“外部信息摘要 + 真实市场数据”结合，对未来 **30~60 分钟**方向进行判断，并每轮固定输出 **1~3 个置信度最高的 LONG / SHORT 信号**。

### 1.1 强制业务约束

1. 只分析 **Binance USDT-M 永续合约当前可交易的币种**。
2. 所有币种统一使用 Binance 合约 symbol，例如：`BTCUSDT`、`ETHUSDT`、`HYPEUSDT`。
3. 方向周期：未来约 **30~60 分钟**，属于短线偏“小长线”的方向判断，不做 1~5 分钟超短线预测，也不做日线/周线长期预测。
4. 每一个成功完成的分析周期，最终必须输出 **1~3 个方向**。
5. 最终方向只能是 `LONG` 或 `SHORT`，**最终结果不得整轮为空，也不得全部 WAIT/NEUTRAL**。
6. 如果市场非常不明确，仍然从 Top10 中选出相对置信度最高的 1 个，并允许其置信度较低；**不要人为设置最低置信度阈值导致无信号**。
7. `confidence` 表示模型基于当前信息的相对方向确信程度，不应描述为“盈利概率”。
8. 不实现作者历史胜率、自动回测加权、作者信誉学习等功能。
9. 所有原始信息、AI 摘要、市场快照和最终结果都应保存，方便 Debug 与未来扩展，但 V1 不基于历史战绩动态调权。
10. 优先使用免费公共数据源；付费数据源必须做成可插拔 Optional Provider，没有 Key 时自动跳过，不得阻塞主流程。

---

## 2. AI 调用成本原则

### 2.1 每轮正常情况只允许 2 次主要 LLM 调用

为了节省 AI 额度，V1 禁止采用“Top10 每个币单独调用一次 AI”的实现。

正常每个周期只需要：

- **AI Call #1：Candidate AI**
  - 一次批量读取程序预过滤后的候选信息。
  - 完成：Top10 币种选择 + Top10 每币独立 Information Packet 总结。

- **AI Call #2：Direction AI**
  - 一次批量读取 Top10 的 Information Packet + Top10 的 Market Packet。
  - 完成：10 个币逐个方向评分 + 最终挑选 1~3 个最高置信度 LONG/SHORT。

只有以下情况才允许额外 AI 调用：

- JSON Schema 解析失败，需要一次结构化重试；
- 模型响应中断；
- 单次 Prompt 超过服务商硬限制，需要分批降级。

不允许为了“复核”无条件重复调用模型。

### 2.2 能用程序计算的内容绝不交给 AI 计算

例如以下数据应由 Python 本地计算：

- 各种排名分数；
- 24h / 1h / 30m / 15m / 5m 涨跌；
- 成交量变化；
- RSI / MACD / EMA / SMA；
- ATR / 波动率；
- OI 变化率；
- Taker Buy/Sell Ratio；
- Order Book Imbalance；
- Bid/Ask Spread；
- 买墙/卖墙统计；
- 数据新鲜度；
- Exact Hash / SimHash / MinHash 去重；
- Binance USDT-M symbol 映射。

AI 的任务应集中在：

- 理解自然语言观点；
- 归纳多个来源；
- 判断信息的重要性与冲突；
- 结合压缩后的市场事实判断方向。

---

## 3. 总体数据流

```text
┌──────────────────────────────────────────────────────┐
│                    Information Sources               │
│ TradingView / Telegram / Binance / CryptoQuant ...   │
└──────────────────────────┬───────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│ Ranking / Discovery Filter                           │
│ 热门 / 最新 / 成交量 / 涨跌 / 技术评级 / 社交 / 事件 │
│ 这里完全程序化，不调用 AI                            │
└──────────────────────────┬───────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│ Raw Collector + Normalizer                           │
│ 原文 / 来源 / URL / 时间 / 排名 / symbol / 元数据    │
└──────────────────────────┬───────────────────────────┘
                           │
                           ▼
                      PostgreSQL
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│ Deterministic Pre-Ranker                             │
│ 去重 + 时间衰减 + 多榜单聚合 + symbol 过滤           │
│ 将大池子压缩到约 15~25 个候选币                      │
└──────────────────────────┬───────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│ Candidate AI  (LLM Call #1)                          │
│ 选 Top10 + 每币生成独立 Information Packet           │
└──────────────────────────┬───────────────────────────┘
                           │
                    Top10 Binance Symbols
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│ Market Data Collector                                │
│ Binance 主数据 + Bybit/OKX 旁证 + Optional Providers │
│ 本地计算全部技术/订单流特征                          │
└──────────────────────────┬───────────────────────────┘
                           │
                    10 x Market Packet
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│ Direction AI  (LLM Call #2)                          │
│ 批量但逐币隔离分析；输出每币方向/置信度/理由          │
│ 最终固定选择 1~3 个最高置信度 LONG/SHORT             │
└──────────────────────────┬───────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│ Signal Store + REST API                              │
│ 供外部交易系统消费，本项目不下单                     │
└──────────────────────────────────────────────────────┘
```

---

## 4. Binance 可交易币种 Universe

系统启动时以及定时刷新 Binance USD-M Futures `exchangeInfo`，建立本系统唯一合法 symbol Universe。

只保留满足以下条件的交易对：

```text
quoteAsset = USDT
contractType = PERPETUAL
status = TRADING
```

所有外部来源出现的币种必须先经过映射：

```text
BTC        -> BTCUSDT
BTCUSD     -> BTCUSDT
BTC/USDT   -> BTCUSDT
BINANCE:BTCUSDT.P -> BTCUSDT
```

无法映射，或 Binance USDT-M 不可交易的币，直接丢弃，禁止进入 Candidate AI。

Universe 建议每 30~60 分钟刷新一次即可，不需要高频调用。

---

# 5. 信息发现层（Information Discovery）

V1 的重点是“先排名筛选，再采集”，不能无脑抓全网全文。

## 5.1 TradingView —— V1 核心来源，必须实现

TradingView 单独建立 `TradingViewCollector`，使用 **Playwright + Chromium + Persistent Profile**。

不要把 TradingView 当作一个单独的“观点网站”；它同时承担：

1. Coin Screener 发现币；
2. CEX Screener 发现可交易合约异动；
3. Ideas 获取人工交易观点。

### 5.1.1 Crypto Coins Screener

建议抓取以下榜单，各取默认 Top 20，可由配置修改：

- `24h_gainers`
- `24h_losers`
- `volume_usd_24h`
- `volume_change_24h`
- `technical_rating_bullish`
- `technical_rating_bearish`
- `social_volume`（页面可用时）
- `social_dominance`（页面可用时）
- `sentiment`（页面可用时）
- `circulating_supply`（仅作为背景/排行信息，不应高权重用于方向）
- `market_cap`（背景信息）

注意：TradingView 的 Technical Rating 是多种 MA 和 Oscillator 的复合评级。它主要用于 **Candidate Discovery**，最终 Direction AI 不应把它当作与本地 RSI/MACD/EMA 完全独立的另一份强证据，避免重复计票。

### 5.1.2 CEX Screener

因为本项目只交易 Binance USDT-M，所以优先配置 TradingView CEX Screener：

```text
Exchange = Binance
Quote = USDT
Symbol Type = Perpetual
```

抓取：

- 24h USD Volume Top 20
- 24h Volume Change Top 20
- 24h Gainers Top 20
- 24h Losers Top 20
- Technical Rating Strong Buy / Buy Top 20
- Technical Rating Strong Sell / Sell Top 20
- 可读取时：Oscillator Rating、Moving Average Rating

### 5.1.3 TradingView Ideas

Crypto Ideas 抓取：

- Most Recent：建议最新 30~50 条
- Most Popular：建议 Top 20

每条尽量保存：

```json
{
  "source": "tradingview_ideas",
  "source_section": "most_popular",
  "rank": 4,
  "symbol_raw": "BTCUSDT",
  "symbol": "BTCUSDT",
  "author": "example_author",
  "published_at": "...",
  "collected_at": "...",
  "direction_tag": "LONG|SHORT|NEUTRAL|UNKNOWN",
  "title": "...",
  "raw_text": "...",
  "url": "...",
  "views": null,
  "likes": null,
  "comments": null
}
```

如果页面只提供部分字段，不要因为字段缺失让整条采集失败。

### 5.1.4 TradingView 抓取技术要求

1. 使用 Playwright Persistent Context 保存 Cookie/Session。
2. Selector 集中维护，不允许散落大量硬编码 XPath。
3. 优先通过列标题/字段名称映射读取表格，而不是依赖 DOM 第 N 列。
4. 如果页面网络请求中已经返回结构化 JSON，可以优先读取页面实际使用的数据响应，但实现必须有 DOM fallback。
5. 页面改版时，一个 Collector 挂掉不能影响其它数据源。
6. 每次抓取记录 `collector_version` 与失败原因。
7. 页面加载、登录失效、Cloudflare、人机验证等必须能明确报错到 `source_health`。

### 5.1.5 TradingView 首次登录

Codex 必须提供一个“首次登录辅助流程”，不能要求用户将 TradingView 明文账号密码写进 `.env`。

建议实现：

```text
scripts/tradingview_login.sh
```

在 VPS 启动一个临时可视 Chromium / noVNC 会话，使用 persistent profile。

要求：

- 默认只绑定 localhost；
- 如果需要 URL 给用户操作，必须有随机认证 token 或通过用户现有安全入口暴露；
- 登录成功后关闭可视会话，后续正常 headless 使用同一个 profile；
- 不记录用户密码。

部署时 Codex 应明确告诉用户一次性登录入口/方式。

---

## 5.2 Binance Futures Public Market —— 免费核心发现源

无需账户交易权限；只使用公共行情。

信息发现阶段可以低成本使用全市场 24h ticker，获得：

- Binance USDT-M 24h 涨幅/跌幅榜；
- 24h quote volume 榜；
- 价格、成交量基础异常。

Binance 的作用不仅是后面的 Market Collector，也应为 Discovery 提供一份完全免费的原始市场异动榜，避免系统只依赖社区热门内容。

---

## 5.3 Telegram —— V1 核心人工观点源

采用 **白名单频道模式**，不做全 Telegram 搜索。

推荐使用 MTProto 客户端（如 Telethon），因为普通 Bot API 无法自由读取任意公开频道历史。

每个白名单频道采集最近一个 `information_window` 内的消息，例如默认 60 分钟。

保存：

- channel id / username；
- message id；
- published time；
- views；
- forwards；
- raw text；
- media metadata（可选）；
- permalink；
- 可能的币种 symbol。

先不实现频道历史胜率。

需要：

```text
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_SESSION (文件或 StringSession)
```

首次 Telegram 用户会话登录应由 Codex提供一次性交互命令，验证码/2FA 不写入配置文件。

---

## 5.4 CryptoQuant —— Optional / 免费优先

用途：

- QuickTake / Verified Authors 的数据型观点；
- BTC/ETH 等主流币的链上/交易所流向补充；
- 研究文章补充。

优先利用免费 Basic/可用额度；没有 Key 或额度不足时自动跳过。

V1 不要求为了 CryptoQuant 强制付费。

---

## 5.5 Nansen —— Optional / 免费额度优先

用途：

- Token Screener；
- Smart Money Netflow；
- Smart Money DEX Trades；
- Flow Intelligence。

Nansen 适合山寨币与链上 Smart Money，但不是主流程必需依赖。

无 Key / 免费 Credits 用尽：自动降级，不阻断周期。

---

## 5.6 CoinMarketCal —— Optional / 免费层优先

用途：发现未来短时间内可能影响行情的事件：

- listing；
- unlock；
- mainnet / upgrade；
- governance；
- product launch；
- major announcement。

只保留能映射到 Binance USDT-M symbol 的事件。

30~60 分钟方向中，事件应作为背景/催化剂，不应单独决定 LONG/SHORT。

---

## 5.7 CoinGlass —— Optional Paid Enhancement

CoinGlass 当前 API 属于付费增强源，V1 不得硬依赖。

有 Key 时可用于：

- aggregated OI；
- aggregated funding；
- liquidation；
- long/short ratio；
- taker buy/sell；
- liquidation heatmap；
- orderbook / large orders。

如果没有 `COINGLASS_API_KEY`：系统仍应依靠 Binance + Bybit + OKX 正常工作。

---

## 5.8 LunarCrush —— Optional Paid Social Enhancement

免费层主要适合基础 market data，完整 social/creator/AI 数据需要付费层，因此 V1 不作为必需依赖。

有 Key 且套餐允许时，可获取：

- AltRank；
- Galaxy Score；
- Social Volume；
- Social Dominance；
- Sentiment；
- Trending topics/posts/creators。

注意：TradingView 部分社交字段可能本身源自 LunarCrush，数据层要保存 provenance，不能当成两份独立证据重复计权。

---

# 6. Raw Information 统一格式

所有来源进入数据库前必须 Normalize 为统一结构。

建议 Schema：

```json
{
  "id": "uuid",
  "cycle_id": "uuid",
  "source": "tradingview|telegram|binance|cryptoquant|nansen|coinmarketcal|coinglass|lunarcrush",
  "source_type": "ranking|article|social_post|market_anomaly|event|smart_money|research",
  "source_name": "...",
  "provider_item_id": "...",
  "symbol": "HYPEUSDT",
  "base_asset": "HYPE",
  "published_at": "2026-09-16T06:00:00Z",
  "collected_at": "2026-09-16T06:01:00Z",
  "ranking_type": "volume_change_24h",
  "ranking_position": 3,
  "ranking_value": 42.1,
  "direction_tag": "LONG|SHORT|NEUTRAL|UNKNOWN",
  "title": "...",
  "raw_text": "原文必须保留",
  "url": "...",
  "author": "...",
  "metrics": {},
  "provenance": {
    "provider": "tradingview",
    "underlying_sources": []
  },
  "raw_payload": {},
  "content_hash": "..."
}
```

`raw_text` 和 `raw_payload` 不允许因为已经做了摘要就删除。

---

# 7. 去重与第一层程序化 Pre-Ranker

## 7.1 去重

为了减少 AI Token，在送给 Candidate AI 之前先做程序去重。

按优先级：

1. `(source, provider_item_id)` 精确去重；
2. URL canonicalization；
3. normalized text SHA256；
4. SimHash / MinHash 近似文本去重；
5. 同一作者短时间重复内容合并。

V1 不强制使用付费 Embedding API 去重。

## 7.2 币种聚合

所有 Raw Information 先按 `symbol` 聚合。

一个币出现 30 条消息，只是一个 candidate，不允许占多个 candidate slot。

## 7.3 程序化 Ranking Score

AI Call #1 之前先用确定性分数把币池压缩到约 15~25 个。

推荐基础公式：

```text
item_score =
    source_weight
  * ranking_type_weight
  * reciprocal_rank(rank)
  * freshness_decay(age)
```

推荐：

```text
reciprocal_rank(rank) = 1 / log2(rank + 1.5)
```

币种总分：

```text
candidate_score = sum(item_score) + diversity_bonus + repeated_presence_bonus
```

但同一来源同一榜单大量重复不得无限累加，需要 cap。

### 初始权重只作为工程默认值，不是策略真理

例如：

```text
TradingView CEX volume/gainers/losers     高
TradingView Ideas popular/recent           高
TradingView Technical Rating               中
Binance market anomaly                     高
Telegram whitelist                         高
CryptoQuant QuickTake                      中高
Nansen Smart Money                         中高
CoinMarketCal Event                        中
Circulating Supply                         低
Market Cap                                 低
```

这些配置全部放数据库/配置文件，后续可调整，但 V1 不自动学习权重。

---

# 8. Candidate AI（LLM Call #1）

## 8.1 输入

不要把整个数据库直接塞给 AI。

程序先取 Pre-Ranker Top 15~25 symbols，对每个 symbol 构建一个压缩 `Candidate Evidence Bundle`。

每币最多保留：

- 最有价值的 5~8 条自然语言原文片段；
- 每条文本限制合理长度（例如 500~1000 字符），保留原始记录 ID；
- 各排行榜位置；
- 各来源出现次数；
- 关键 numeric metrics；
- 事件摘要；
- 最新度。

重复文章只能留最原始/信息量最高版本。

## 8.2 Candidate AI 职责

一次模型调用完成：

1. 在 15~25 个候选中选择 **Top 10 不同 Binance USDT-M symbols**；
2. 每个 Top10 生成一份独立 Information Packet；
3. 总结“外界在为什么讨论这个币”，而不是直接做最终交易决定；
4. 区分 Bullish evidence / Bearish evidence / contradictions；
5. 提取各来源共同提到的支撑、阻力、突破/失效位置（如果存在）；
6. 保留 evidence IDs，Direction AI 需要时可追溯。

## 8.3 Information Packet Schema

```json
{
  "symbol": "HYPEUSDT",
  "candidate_rank": 1,
  "candidate_reason": "...",
  "attention_state": "rising|high|normal|falling",
  "information_bias": "BULLISH|BEARISH|MIXED",
  "source_counts": {
    "tradingview": 8,
    "telegram": 3,
    "binance": 2,
    "other": 1
  },
  "key_information": [
    "...",
    "..."
  ],
  "bullish_arguments": ["..."],
  "bearish_arguments": ["..."],
  "contradictions": ["..."],
  "key_levels": {
    "support": [],
    "resistance": [],
    "breakout": [],
    "breakdown": []
  },
  "events": [],
  "ranking_evidence": [
    {
      "source": "tradingview_cex",
      "ranking_type": "volume_change_24h",
      "rank": 3
    }
  ],
  "evidence_ids": ["uuid1", "uuid2"],
  "information_freshness": "fresh|mixed|stale",
  "summary": "控制在较短篇幅的结构化总结"
}
```

要求：Top10 的 packet 必须分别独立，禁止生成一篇大段“十个币混在一起”的自由文本。

---

# 9. 市场数据采集器（Market Data Collector）

Candidate AI 完成后，只对 Top10 采集较重的市场数据。

## 9.1 Binance —— Primary / 必须

Binance USDT-M 永续作为主口径。

建议采集：

### Price / Market

- last price
- mark price
- index price
- best bid / best ask
- 24h quote volume

### Kline

用于 30~60m 方向：

- 1m
- 5m
- 15m
- 30m
- 1h
- 4h（只作为更大结构背景）

不要把原始几百根 K 线全部发给 AI；采集后本地计算特征。

### Open Interest

- current OI
- OI history
- change 5m
- change 15m
- change 30m
- change 1h

### Funding

- current funding
- next funding time
- recent funding history（必要时）

### Position / Sentiment

- Global Long/Short Account Ratio
- Top Trader Long/Short Account Ratio
- Top Trader Long/Short Position Ratio

### Taker

- Taker Buy Volume
- Taker Sell Volume
- Buy/Sell Ratio
- 5m / 15m / 30m / 1h

### Orderbook

从 Binance Depth 数据计算：

- bid/ask spread
- ±0.1% imbalance
- ±0.5% imbalance
- ±1% imbalance
- largest bid wall
- largest ask wall
- wall distance from current price

不需要把几千档原始 Orderbook 直接发给 AI。

### Trades

必要时使用 aggTrades / WebSocket 计算短周期 buy/sell pressure、CVD 类特征。

---

## 9.2 Bybit + OKX —— 免费 Cross-Exchange Confirmation

只针对 Top10，做轻量旁证。

目的：验证 Binance 的异常是否只是单所现象。

每币建议只拿：

- price / mark price；
- 5m/15m/1h Kline 或回报；
- funding；
- OI（支持时）；
- top-of-book / 轻量 depth。

不必像 Binance 一样采全量特征。

如果某个币在 Bybit/OKX 不存在，标记 `unavailable`，不报错终止。

---

## 9.3 CoinGlass Optional Enrichment

如果配置 API Key，再补：

- aggregated OI；
- OI-weighted funding；
- aggregated liquidation；
- long/short；
- large orders；
- liquidation heatmap / orderbook heatmap（套餐支持时）。

没有 Key 则相关字段为 `null` + `provider_status=disabled`。

---

## 9.4 On-chain / Smart Money Enrichment

不是所有币都强制采。

- BTC / ETH：优先 CryptoQuant；
- 支持链上 Smart Money 的山寨：Nansen；
- 没有可靠数据：不硬填。

Direction AI 必须能接受部分字段为空。

---

# 10. Market Packet

所有原始市场数据先本地计算，最终给 AI 的是压缩后的 Market Packet。

示例：

```json
{
  "symbol": "HYPEUSDT",
  "snapshot_at": "2026-09-16T06:30:02Z",
  "reference_price": 52.81,
  "data_age_seconds": 3,

  "returns_pct": {
    "1m": 0.10,
    "5m": 0.41,
    "15m": 1.12,
    "30m": 1.54,
    "1h": 2.38,
    "4h": 3.10
  },

  "trend": {
    "5m": "bullish",
    "15m": "bullish",
    "30m": "bullish",
    "1h": "neutral",
    "4h": "bullish"
  },

  "technical_features": {
    "rsi_5m": 61.2,
    "rsi_15m": 58.4,
    "rsi_1h": 55.0,
    "macd_15m_state": "bullish",
    "ema_alignment_15m": "bullish",
    "atr_pct_15m": 1.21,
    "volume_zscore_15m": 2.1
  },

  "open_interest": {
    "current": 0,
    "change_5m_pct": 0,
    "change_15m_pct": 0,
    "change_30m_pct": 0,
    "change_1h_pct": 0
  },

  "funding": {
    "current": 0,
    "state": "normal|high_positive|high_negative"
  },

  "taker_flow": {
    "ratio_5m": 0,
    "ratio_15m": 0,
    "ratio_30m": 0,
    "state": "buy_dominant|sell_dominant|balanced"
  },

  "long_short": {
    "global": 0,
    "top_accounts": 0,
    "top_positions": 0
  },

  "orderbook": {
    "spread_bps": 0,
    "imbalance_0_1pct": 0,
    "imbalance_0_5pct": 0,
    "imbalance_1pct": 0,
    "largest_bid_wall_usd": 0,
    "largest_ask_wall_usd": 0,
    "bid_wall_distance_pct": 0,
    "ask_wall_distance_pct": 0
  },

  "liquidation": {
    "available": false,
    "long_liq_15m_usd": null,
    "short_liq_15m_usd": null,
    "notes": null
  },

  "cross_exchange": {
    "bybit": {
      "available": true,
      "price_delta_vs_binance_bps": 0,
      "funding": 0,
      "oi_change_15m_pct": 0
    },
    "okx": {
      "available": true,
      "price_delta_vs_binance_bps": 0,
      "funding": 0,
      "oi_change_15m_pct": null
    }
  },

  "onchain_or_smart_money": {},

  "data_quality": {
    "score": 0.92,
    "missing_fields": [],
    "stale_fields": []
  }
}
```

---

# 11. Direction AI（LLM Call #2）

## 11.1 输入

一次请求包含：

```text
Top10 Information Packets
+
Top10 Market Packets
```

必须以 symbol 为一级边界，结构化传入，不使用混乱大段文本。

模型 Prompt 必须明确：

1. 每个币独立分析，不允许 BTC 的证据污染 SOL；
2. 先看 Information Packet 是“市场参与者在讨论什么”；
3. 再看 Market Packet 是否确认/反驳这些观点；
4. 预测窗口是未来 30~60 分钟；
5. 每个币必须给一个方向倾向 `LONG` 或 `SHORT`，并给 confidence；
6. 最终从 10 个币中必须选择 1~3 个 confidence 最高的方向；
7. 不允许因为“信号不够完美”整轮输出空列表；
8. 如果最强方向仍然弱，可以降低 confidence，但仍选第 1 名；
9. 不输出仓位、杠杆、下单数量、交易账户操作。

## 11.2 Direction Result Schema

```json
{
  "cycle_id": "uuid",
  "time_horizon_minutes": [30, 60],
  "analyzed": [
    {
      "symbol": "HYPEUSDT",
      "direction": "LONG",
      "confidence": 82,
      "information_alignment": "supports|conflicts|mixed",
      "market_state": "trend|range|breakout|breakdown|squeeze|uncertain",
      "supporting_factors": ["..."],
      "opposing_factors": ["..."],
      "key_levels": {
        "support": [],
        "resistance": []
      },
      "invalidation_condition": "...",
      "reason_summary": "控制长度"
    }
  ],
  "selected": [
    {
      "rank": 1,
      "symbol": "HYPEUSDT",
      "direction": "LONG",
      "confidence": 82,
      "reference_price": 52.81,
      "reason_summary": "...",
      "invalidation_condition": "..."
    }
  ]
}
```

### 强制 Validator

程序端必须验证：

```text
len(selected) >= 1
len(selected) <= 3
all(direction in {LONG, SHORT})
all(symbol belongs to current Top10)
selected 按 confidence DESC 排序
selected 不得重复 symbol
```

如果模型第一次返回 0 个 selected：

- 不要直接保存空信号；
- 用同一个响应里的 `analyzed` 在程序端按 confidence 排序，自动取 Top1；
- 只有当 `analyzed` 本身解析失败时才重试 LLM。

这样可以保证“至少一个方向”，同时减少无意义的二次 AI 调用。

---

# 12. Signal 输出接口

本项目不负责交易，因此最终信号必须是稳定的机器接口。

优先实现 FastAPI REST，并同时写 PostgreSQL。

## 12.1 GET `/api/v1/signals/latest`

返回最近一个成功周期的 1~3 个信号。

示例：

```json
{
  "cycle_id": "...",
  "generated_at": "...",
  "time_horizon_minutes": [30, 60],
  "signals": [
    {
      "rank": 1,
      "symbol": "HYPEUSDT",
      "direction": "LONG",
      "confidence": 82,
      "reference_price": 52.81,
      "reason_summary": "...",
      "invalidation_condition": "..."
    }
  ]
}
```

## 12.2 GET `/api/v1/signals/history`

参数：

```text
limit
symbol
from
until
```

## 12.3 GET `/api/v1/cycles/latest`

用于 Debug 当前一轮：

- collector status；
- raw items count；
- prefiltered candidates；
- Top10；
- market collection status；
- AI latency / usage；
- final signals。

## 12.4 GET `/api/v1/candidates/latest`

返回 Candidate AI Top10 + Information Packets，便于人工检查。

## 12.5 GET `/api/v1/sources/status`

返回所有 Provider 健康状态、最后成功时间与错误。

交易系统应该只依赖 `/api/v1/signals/latest`，不要依赖内部表结构。

---

# 13. 数据库设计

建议 PostgreSQL。

至少包含：

## `analysis_cycles`

- id UUID PK
- started_at
- finished_at
- status
- information_window
- target_horizon
- error
- ai_calls
- ai_input_tokens
- ai_output_tokens

## `source_items`

保存 Raw Information。

重要索引：

- `(cycle_id)`
- `(symbol, published_at)`
- `(source, provider_item_id)` unique where possible
- `content_hash`

## `ranking_snapshots`

如果 source_items JSONB 过重，也可独立保存各榜单快照。

## `candidate_packets`

- cycle_id
- symbol
- candidate_rank
- information_packet JSONB
- evidence_ids JSONB

## `market_snapshots`

- cycle_id
- symbol
- snapshot_at
- market_packet JSONB
- raw_refs JSONB

## `direction_results`

- cycle_id
- symbol
- direction
- confidence
- result JSONB

## `signals`

- cycle_id
- rank
- symbol
- direction
- confidence
- reference_price
- generated_at
- valid_until
- payload JSONB

建议 `valid_until = generated_at + 60~90 分钟`，具体值可配置；API 必须同时返回 `generated_at`，交易系统自行判断是否过期。

## `source_health`

- source
- enabled
- last_success_at
- last_error_at
- error_message
- consecutive_failures
- metadata JSONB

## `system_settings`

保存可动态读取的配置，例如：

- cycle interval
- source enabled flags
- TradingView Top N
- information window
- candidate prefilter count
- final candidate count = 10
- final signal count max = 3

不需要实现“最低置信度”“Bot 选择 AI model”等无关 UI。

---

# 14. Telegram 管理 Bot 的集成原则

本项目不需要从零重新设计一个复杂 Telegram 管理 Bot。

用户后续会提供现有项目给 Codex 参考并兼容。

本项目只需要：

1. 配置存 DB / `.env` / Settings Service；
2. 提供清晰的 Python service 方法或内部 Config API；
3. Bot 可以后续接入以下核心设置：
   - 系统启停；
   - 运行周期；
   - 各信息源启停；
   - 每个 Ranking Top N；
   - Telegram 白名单频道；
   - 信息时间窗口；
   - 查看最新 Top10；
   - 查看最新 1~3 个 Signals；
   - 查看 Source Health。

不要在 V1 自行做大量用户没有要求的 Bot 菜单。

---

# 15. 调度策略

默认周期可先配置为，例如 20 分钟；最终以 Telegram 管理项目中的设置为准。

每个周期步骤：

```text
1 refresh symbol universe if needed
2 collect/discover ranked source data
3 normalize & dedupe
4 deterministic pre-rank -> 15~25 symbols
5 Candidate AI -> Top10 + Information Packets
6 collect Top10 market data concurrently
7 build Market Packets
8 Direction AI -> analyze Top10 + select 1~3
9 validate / fallback Top1
10 persist signals
```

### 防重入

一个周期尚未结束时，下一个周期不得并行重复启动。

单 VPS V1 推荐：

- APScheduler + PostgreSQL advisory lock；或
- 只有一个 scheduler worker。

无需第一版就引入 Kafka/Kubernetes。

---

# 16. 建议技术栈

```text
Python 3.12+
FastAPI
SQLAlchemy 2.x
Alembic
PostgreSQL
httpx / aiohttp
Pydantic v2
Playwright + Chromium
Telethon
APScheduler
pandas / numpy（只在确有需要时）
```

技术指标可选：

- `pandas-ta` / `ta`，或自行计算；
- 重点是输出稳定特征，而不是引入复杂 TA 框架。

V1 不强制 Redis。

---

# 17. 推荐项目目录

```text
ai-signal-system/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── signals.py
│   │   ├── candidates.py
│   │   ├── cycles.py
│   │   └── health.py
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   ├── scheduler.py
│   │   └── symbols.py
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py
│   │   └── repositories/
│   ├── collectors/
│   │   ├── base.py
│   │   ├── tradingview/
│   │   │   ├── browser.py
│   │   │   ├── coin_screener.py
│   │   │   ├── cex_screener.py
│   │   │   └── ideas.py
│   │   ├── telegram.py
│   │   ├── binance_discovery.py
│   │   ├── cryptoquant.py
│   │   ├── nansen.py
│   │   ├── coinmarketcal.py
│   │   ├── coinglass.py
│   │   └── lunarcrush.py
│   ├── market/
│   │   ├── binance.py
│   │   ├── bybit.py
│   │   ├── okx.py
│   │   ├── features.py
│   │   └── packet_builder.py
│   ├── ranking/
│   │   ├── dedupe.py
│   │   ├── symbol_mapper.py
│   │   └── pre_ranker.py
│   ├── ai/
│   │   ├── client.py
│   │   ├── candidate_analyzer.py
│   │   ├── direction_analyzer.py
│   │   ├── schemas.py
│   │   └── prompts/
│   ├── pipeline/
│   │   └── cycle_runner.py
│   └── services/
│       ├── settings.py
│       └── source_health.py
├── scripts/
│   ├── tradingview_login.sh
│   ├── telegram_login.py
│   └── bootstrap.sh
├── migrations/
├── tests/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pyproject.toml
└── README.md
```

---

# 18. AI Provider 抽象

不要将项目逻辑与某一家模型 SDK 写死。

实现统一接口：

```python
class AIClient:
    async def structured_completion(
        self,
        system_prompt: str,
        payload: dict,
        response_model: type[BaseModel],
    ) -> BaseModel:
        ...
```

通过环境变量：

```text
AI_BASE_URL=
AI_API_KEY=
AI_MODEL=
AI_TIMEOUT_SECONDS=
```

Bot 不需要提供切换 AI 模型的菜单；部署配置即可。

要求尽可能使用模型服务商的 Structured Output / JSON Schema 功能。

---

# 19. Prompt 编写原则

## Candidate AI Prompt

必须包含：

- 只可选择 Binance USDT-M universe 中的币；
- 目标是找“值得进一步采集市场数据”的 Top10，而不是最终下单；
- 同一个币所有来源必须合并总结；
- 不因为帖子数量多就自动认为方向正确；
- 热门、最新、异常、事件、人工观点都要考虑；
- 每币返回简洁结构化 Packet；
- 不输出 Markdown，自始至终只返回 Schema JSON。

## Direction AI Prompt

必须包含：

- horizon 30~60 minutes；
- Information Packet 和 Market Packet 权责区分；
- 市场数据应验证/反驳外部观点；
- 逐 symbol 独立分析；
- 任何币都只给 LONG 或 SHORT directional lean；
- 最后必须选 1~3；
- 不允许空 selected；
- 不输出交易数量、杠杆、账户操作；
- confidence 不是数学盈利概率；
- 市场数据缺失时降低 confidence，而不是幻想数据。

---

# 20. Data Provenance

必须保存来源血缘，防止重复证据。

例如：

```json
{
  "provider": "coinglass",
  "underlying_sources": ["binance", "bybit", "okx"],
  "metric": "aggregated_open_interest"
}
```

如果：

- TradingView Social 指标来自 LunarCrush；
- CoinGlass 聚合 OI 包含 Binance；

AI Packet 生成前应标记为“相关/派生证据”，避免把同一事实理解为多个完全独立确认。

---

# 21. 错误处理与降级

任何一个 Optional Provider 失败，不得终止完整 cycle。

例如：

```text
TradingView failed
→ 仍使用 Binance + Telegram + 其他源

CoinGlass no key
→ Binance/Bybit/OKX 正常

Nansen quota exhausted
→ skip Nansen

CryptoQuant unavailable
→ skip
```

只有以下情况才使 Cycle 失败：

- Binance symbol universe 无法获取且无可用缓存；
- Candidate AI 无法得到合法 Top10（经过重试仍失败）；
- Top10 全部无法取得最基本 Binance 行情；
- Direction AI 无法返回可解析结果且无法恢复。

如果新 Cycle 失败，REST API 可以继续返回上一轮成功信号，但必须明确：

```json
{
  "is_stale": true,
  "generated_at": "..."
}
```

不得伪造新的信号。

---

# 22. 日志与可观测性

日志至少需要：

```text
cycle_id
source
symbol
stage
latency_ms
result_count
error
```

每轮结束输出摘要：

```text
Cycle XXX
Sources: 6/8 success
Raw items: 213
Unique items: 151
Candidate prefilter: 22
Candidate AI: Top10
Market packets: 10/10
Direction AI: success
Signals: HYPEUSDT LONG 82, SOLUSDT SHORT 76
AI calls: 2
```

不要把 API Key、Telegram session、Cookie、Authorization header 写进日志。

---

# 23. Docker / VPS 部署

推荐 Compose：

```text
services:
  api
  worker
  postgres
```

`worker` 镜像内安装 Playwright Chromium。

`api` 和 `worker` 共用：

- PostgreSQL；
- TradingView persistent profile volume；
- 配置/Secrets；
- application code。

建议持久卷：

```text
postgres_data
tradingview_profile
app_logs
telegram_session
```

启动后：

```text
docker compose up -d
```

要求：

- `restart: unless-stopped`
- 自动 Alembic migration
- `/health` healthcheck
- VPS 重启后自动恢复

---

# 24. 环境变量与 Key

## 第一版真正必要

```text
DATABASE_URL=
AI_BASE_URL=
AI_API_KEY=
AI_MODEL=
```

### Telegram 来源启用时

```text
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION_PATH=
```

### TradingView

不保存明文密码。

```text
TRADINGVIEW_PROFILE_DIR=/data/tradingview-profile
```

首次人工登录后持久化 Session。

## 免费公共市场数据不需要账户 Key

- Binance public market data
- Bybit public market data
- OKX public market data

## Optional

```text
COINGLASS_API_KEY=
CRYPTOQUANT_API_KEY=
NANSEN_API_KEY=
COINMARKETCAL_API_KEY=
LUNARCRUSH_API_KEY=
```

所有 Optional Key 为空时，对应 Provider 自动 disabled。

### 推荐最初部署成本最低组合

第一阶段直接运行：

```text
TradingView
Binance public
Bybit public
OKX public
Telegram whitelist
AI provider
```

然后根据实际价值逐个增加：

```text
CryptoQuant
Nansen
CoinMarketCal
CoinGlass
LunarCrush
```

不建议第一天就为了“数据看起来更多”购买全部付费 API。

---

# 25. TradingView / 数据源当前实现参考

Codex 开发时应以官方最新文档为准，不要把下面 endpoint/页面结构视为永久固定。

参考来源：

- TradingView CEX Screener / Crypto Coins Screener / Technical Ratings / Crypto Ideas
- Binance USD-M Futures REST + WebSocket Market Data
- Bybit V5 Public Market API
- OKX V5 Public Market API
- CoinGlass API V4
- CryptoQuant API/MCP + QuickTake
- Nansen API
- CoinMarketCal API
- LunarCrush API v4

TradingView 为浏览器采集目标，应预期网站 DOM 会变化，所以一定要模块化 selector 与健康检查。

---

# 26. V1 明确不做

Codex 不要自行扩展以下内容：

- ❌ Binance/Bybit/OKX 账户 API Key 交易权限
- ❌ 下单
- ❌ 自动开仓/平仓
- ❌ TP/SL
- ❌ 仓位管理
- ❌ 杠杆管理
- ❌ 保证金管理
- ❌ PnL 管理
- ❌ 作者历史胜率
- ❌ 自动回测作者
- ❌ 根据历史结果自动优化 source weight
- ❌ 强制最低 confidence 后无信号
- ❌ 每个 Top10 币分别调用一次 LLM
- ❌ 第一版 Kubernetes/Kafka 等过度工程化

这部分会由用户的其它交易项目负责或未来单独扩展。

---

# 27. 验收标准

项目只有满足以下条件才算 V1 完成：

### 数据层

- [ ] 能自动获取 Binance USDT-M 可交易 Universe
- [ ] TradingView Coin Screener 能抓至少 4 类榜单
- [ ] TradingView CEX Screener 能抓 Binance USDT 永续榜单
- [ ] TradingView Ideas 能抓 Most Recent + Most Popular
- [ ] Telegram 白名单可采集
- [ ] 所有来源 normalize 入 PostgreSQL
- [ ] 原文、时间、来源、URL、排名信息完整保留

### Candidate

- [ ] 能本地去重
- [ ] 能本地聚合 symbol
- [ ] 能程序化 pre-rank
- [ ] Candidate AI 只调用 1 次
- [ ] 输出 10 个不同 Binance USDT-M symbols
- [ ] 每币有独立 Information Packet

### Market

- [ ] Binance Top10 市场数据完整采集
- [ ] 本地计算核心特征
- [ ] Bybit / OKX 可用时做旁证
- [ ] Optional provider 无 Key 不会导致失败
- [ ] 每币输出独立 Market Packet

### Direction

- [ ] Direction AI 正常每轮只调用 1 次
- [ ] 同时分析 Top10 但结果按 symbol 分离
- [ ] 每币返回 LONG 或 SHORT lean + confidence
- [ ] 最终固定输出 1~3 个
- [ ] selected 永远不会因为“信号不足”为空
- [ ] 如果 AI selected 意外为空，程序自动取 analyzed 中最高 confidence Top1，不额外浪费 AI 调用

### API / 部署

- [ ] `/api/v1/signals/latest` 可供外部交易系统消费
- [ ] 有历史信号 API
- [ ] 有候选/周期 Debug API
- [ ] Docker Compose 一键启动
- [ ] VPS 重启后自动恢复
- [ ] TradingView Profile 持久化
- [ ] Secrets 不进入 Git/日志

---

# 28. Codex 执行要求

拿到本文件后，请按以下方式工作：

1. 先检查 VPS 当前系统、Docker、已有项目与端口占用情况。
2. 如果用户随后提供另一个 Telegram/交易项目，优先复用其配置方式、日志方式和 Bot 管理交互，不要重复造一套冲突的管理系统。
3. 创建本项目独立目录与 Git 仓库（若用户已有目标目录则遵从）。
4. 先实现 Binance Universe、DB Schema、Source Base Interface。
5. 再完成 TradingView + Binance + Telegram 这三个 V1 核心源。
6. 实现 pre-ranker 与 Candidate AI。
7. 实现 Binance/Bybit/OKX Market Collector 与本地 Features。
8. 实现 Direction AI 与最终 1~3 信号 Validator/Fallback。
9. 实现 REST API。
10. 完成 Docker Compose 与开机自启。
11. 提供 `.env.example`，明确告诉用户缺少哪些 Key。
12. 对需要人工登录的 TradingView / Telegram，给用户明确的一次性登录入口或命令。
13. 用真实公共数据跑通至少一个完整 cycle，并展示：Raw Count → Pre-Rank → Top10 → Market Packets → Final 1~3 Signals。
14. 不接交易账户、不执行真实订单。
15. 部署完成后更新 README，写清楚启动、重启、更新、日志查看、TradingView 登录失效恢复、Telegram Session 恢复与 API 使用方法。

---

# 29. 最终设计一句话

> **先用排行榜和程序规则便宜地发现“哪些币正在发生事情”，再用一次 AI 把信息压缩成 Top10 独立情报包；只对 Top10 拉重市场数据，最后再用一次 AI 对 10 个币做 30~60 分钟方向判断，并强制挑出置信度最高的 1~3 个 LONG/SHORT 给外部交易系统。**

