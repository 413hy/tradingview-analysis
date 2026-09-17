# AI 信号与 Binance Demo 自动交易

独立分析服务、Binance USDT-M Demo 执行服务，以及两个独立 Telegram 管理 Bot。分析每轮正常调用本地 Codex 两次：从预排名候选中选 Top10，再批量分析方向并发布 1～3 个 LONG/SHORT 信号。

## 功能

- TradingView 榜单与 Ideas、Binance 异动榜、Telegram 白名单采集；归一化、去重、可配置预排名、证据归属校验。
- Top10 行情采集与本地指标；结构化 AI 输出校验；PostgreSQL 原始记录、分析结果、来源健康和审计；FastAPI 发布。
- Binance Demo 独立交易执行：信号60秒有效、持久化去重、精度和资金检查、TP可达性、SL优先及reduce-only保护、未知订单响应核对和重启恢复。
- 分析 Bot 与交易 Bot 分离；中文 emoji 菜单、状态切换按钮、参数输入/预览/保存、授权检查、轮询恢复和持久化通知。
- 可选 CoinMarketCal、LunarCrush、Nansen、CryptoQuant、CoinGlass 适配；未配置密钥时跳过。

默认每笔保证金10 USDT、3倍杠杆（最大5倍）、保留20 USDT、净止盈目标0.5 USDT、净止损目标2.7 USDT。设置更新只影响新仓。结算净盈亏不包含资金费；成交证据不足时保留待核账，不编造盈亏。

## 目录

|目录|用途|
|---|---|
|signal-system|采集、模型分析、REST、分析 Bot|
|trading-system|Demo执行、交易账本、交易 Bot|
|shared|配置、数据库、HTTP客户端、Bot公共逻辑|
|deployment|Docker Compose、镜像、配置示例、bridge服务|
|scripts|安装、配置、管理、登录辅助、备份与验证|
|tests|模拟交易、故障恢复、Bot与PostgreSQL集成测试|

## 安装与配置

需要 Docker Compose、Python venv 和已登录的本地 Codex CLI。容器使用 Python3.12；默认模型为 `gpt-5.6-terra`，medium，需账户实际支持。

首次安装：

```bash
bash scripts/bootstrap.sh
.venv/bin/python scripts/configure.py
.venv/bin/python scripts/manage.py start
.venv/bin/python scripts/manage.py check
```

交互填写 Demo API Key/Secret、两个不同 Bot Token、管理员 chat_id/user_id；可选 Telegram MTProto API_ID/API_HASH。密钥保存在本地 `deployment/.env`，不提交 Git。两个 Bot 需分别发送 `/start`。

默认部署路径 `/root/tradingview-analysis`。其他路径需调整 `.env.example`、systemd单元中的路径；默认网段172.30.87.0/24及端口8010、55432应可用。API/PostgreSQL端口仅绑定localhost，模型bridge只绑定Docker网桥并要求令牌。

检查通过后启用 Demo：

```bash
.venv/bin/python scripts/manage.py enable-demo
```

此命令先做只读验证，再启用后续信号消费，不隐式发送测试订单。暂停新仓：

```bash
.venv/bin/python scripts/manage.py disable-entries
```

暂停新仓保留已有仓位管理。所有私有交易固定Binance Demo域名，不支持正式盘。

## Bot 操作

分析：状态、最新信号、Top10、来源启停、分析设置、暂停/恢复分析。

交易：当前持仓、状态、最近交易、异常、开仓设置、暂停/恢复新仓。内嵌按钮支持保证金、总价值、杠杆、止盈与止损编辑。`/retry 交易ID` 请求核对/重试保护，不盲目重发未知订单。

## 登录与验证

TradingView人工登录：`bash scripts/tradingview_login.sh`。Telegram采集会话需配置API_ID/API_HASH，再暂停worker并运行：

```bash
.venv/bin/python scripts/run.py .venv/bin/python scripts/telegram_login.py
```

完成后恢复worker。Bot Token不能替代MTProto用户会话。未配置MTProto时使用公开预览作为有限采集路径。

```bash
.venv/bin/pip install pytest pytest-asyncio ruff
.venv/bin/ruff check shared signal-system trading-system scripts tests migrations
.venv/bin/python scripts/run.py .venv/bin/pytest -q
```

PostgreSQL集成测试使用唯一临时schema并自动清理；模拟测试不写生产信号。实际模型验证脚本 `scripts/verify_ai.py` 使用明确标注的测试数据且不发布信号。

## 交付与验收边界

代码及模拟集成测试已实现；真实模型两阶段请求与Telegram接口曾验证通过。真实 Binance 完整周期、Demo下单和保护触发尚未完成验收；实际账户权限和网络条件需在部署环境验证。付费源尚未进行真实套餐验收。

仓库不包含密钥、账户认证文件、运行数据库、浏览器登录会话或本机网络配置。

参考交易规则来源为 `413hy/codex-single/auto-trader-longtime`；风险计算和Telegram轮询适配的来源见代码与 [实现说明](docs/IMPLEMENTATION_NOTES.md)。完整需求见 [开发规格](ai_crypto_signal_system_development_spec.md)，备份恢复见 [运维说明](docs/OPERATIONS.md)。
