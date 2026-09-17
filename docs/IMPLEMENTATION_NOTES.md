# 实现决策与参考

用户于 2026-09-17 确认：本目录同时部署信号系统、Binance Demo 交易系统、独立分析 Bot 和交易 Bot。AI 调用本地 Codex，模型按本机可用标识为 `gpt-5.6-terra`，推理 `medium`；已实际验证结构化调用成功。

新确认覆盖 REQUIREMENTS_REVIEW.md 中待决的接口、Bybit Demo 和 Bot 范围。采用 REST 对接；无需兼容旧 SQLite 发布协议，交易账本仍独立 SQLite。

参考交易规则代码 `trader/risk.py` 取自 413hy/codex-single，提交 ad370f100e9b591b9b0105a5635b583db1cede92 的 auto-trader-longtime/src/longtime/risk.py；保留来源用于审计。其他服务为本项目实现。

官方接口依据：

- Binance Demo REST 地址：https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info
- 普通订单、条件单、仓位与杠杆：https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade
- Codex 非交互式调用与结构化输出：https://developers.openai.com/codex/noninteractive
- TradingView 实际浏览器请求：crypto-coins-screener 和 crypto-screener 页面。浏览器结构化响应优先，DOM fallback 单独标记部分覆盖。
- 测试频道：https://t.me/s/binance_announcements 和 https://t.me/s/cointelegraph 。用于公告与新闻采集，不声称频道具有历史盈利能力。

配置约定：保证金 10 USDT、3x、净 TP 0.5 USDT、净 SL 2.7 USDT，保留 20 USDT。交易权限未配置前服务待命。用户配置 Demo 凭据后再进行真实 Demo 验收，模拟测试不等同于真实订单验收。

可选 Provider 接口依据（2026-09-17 检查；无付费密钥，只有协议夹具验证）：

- [CoinMarketCal API](https://coinmarketcal.com/en/api)：v2/events，x-api-key。
- [LunarCrush API](https://lunarcrush.com/developers/api)：api4 public/coins/list/v1，Bearer。
- [Nansen API](https://docs.nansen.ai/)：POST /api/v1/token-screener，apikey；链与地址通过人工可信 NANSEN_TOKEN_MAP 映射 Binance 合约，未映射行跳过。
- [CryptoQuant API](https://docs.cryptoquant.com/)：BTC/ETH exchange-flows/netflow，Bearer；链上流量仅作背景，不伪装分钟级即时信号。
- [CoinGlass API](https://docs.coinglass.com/reference/overview)：v4 aggregated OI、OI-weight funding 与 aggregated liquidations，CG-API-KEY；聚合交易所指标记录相关性，不能重复当作独立确认。
- [Binance 订单与成交查询](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)：成交按 orderId 核对，使用 fromId 分页；超过查询窗口且没有缓存的历史成交必须保留待核账。

本轮恢复约束：HTTP 5xx/408、-1006/-1007、重复身份错误或成功响应缺订单身份均作 UNKNOWN；核对原身份，不用重发来猜测结果。已确认取消的保护单手动补挂使用新身份，所有历史腿共同参与结算。已缓存的实际入场成交不会因交易所订单历史到期而停止已有仓位保护。

当前 net_pnl 口径为已实现交易盈亏减成交手续费，不含资金费，账本和通知均明确标注；不等同于账户总收益。外部平仓/清算无法归入本系统订单时保留待核账。
