# 项目协作说明

这是一个双服务系统：分析服务产生可审计的方向信号，交易服务只消费信号并在 **Binance USDⓈ-M Demo** 执行。不要将两者混成一个进程，也不要把交易密钥交给模型。

开始修改前阅读：

1. `README.md`：安装、运行与当前交付边界。
2. `ai_crypto_signal_system_development_spec.md`：原始分析需求。
3. `docs/REQUIREMENTS_BASELINE.md`：用户后续确认后生效的范围和约束。
4. 交易改动还须阅读 `docs/OPERATIONS.md` 与相关测试。

## 关键边界

- 交易接口只能使用 `https://demo-fapi.binance.com`；不得改为 Binance 正式盘或其它交易所。
- 分析 Universe 仍来自 Binance 正式 USDⓈ-M `TRADING`、`PERPETUAL`、`USDT` 合约；Demo 仅用于执行测试。
- 正常分析周期恰好两次主要模型调用：选 Top10、批量方向分析。不得逐币调用或强行凑信号。
- 模型没有工具、浏览器、交易或密钥权限。程序负责精度、资金、杠杆、订单和 TP/SL。
- 新开仓仅消费发布后 60 秒内的信号；预测展示窗口为 30–60 分钟。未知订单响应只按原 client ID 核对，不能盲目重发。
- 持仓保护单仅 reduce-only，SL 优先。暂停停止新仓，不停止已有仓位核对。
- 金额、价格和数量使用 `Decimal`；不把模拟结果说成真实订单验收。
- 密钥只放本地 `deployment/.env`。不提交 `.env`、runtime 数据库、Telegram session、浏览器 profile、Codex 认证或本机网络配置。

## 结构

- `signal-system/src/signals`：来源采集、预排名、行情、AI、REST、分析 Bot。
- `trading-system/src/trader`：Demo 适配、风险、账本、执行、交易 Bot。
- `shared/src/common`：配置、PostgreSQL、Bot 公共逻辑、SQLite 工具。
- `deployment`：容器和 bridge 服务模板。
- `tests`：故障恢复和集成测试。PostgreSQL 集成测试会使用独立 schema。

## 验证

运行变更相关测试；提交前至少运行：

```bash
.venv/bin/ruff check shared signal-system trading-system scripts tests migrations
.venv/bin/python scripts/run.py .venv/bin/pytest -q
```

真实网络、模型和交易验证是额外步骤。遇到 Binance 网络不可达时记录清楚，不要改用其他交易所伪装成 Binance 数据，也不要自动修改账户或发测试单。
