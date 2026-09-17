# 新设备部署指南

本指南只描述可公开复用的系统配置；不要把当前机器的密钥、代理、浏览器 profile、Telegram 会话或账本复制到 Git。

## 前置条件

- Linux 主机、Docker Engine 与 Docker Compose。
- Python 3.11+ 用于本机 bridge、脚本和测试；镜像内为 Python 3.12。
- 已安装并登录 Codex CLI，且账户支持配置的模型。
- 可访问 Binance USDⓈ-M 正式公开数据和 Demo 交易接口的网络。无法访问时系统会保留失败记录，不会虚构信号或订单。

## 初始化

```bash
git clone https://github.com/413hy/tradingview-analysis.git
cd tradingview-analysis
bash scripts/bootstrap.sh
.venv/bin/python scripts/configure.py
.venv/bin/python scripts/manage.py start
.venv/bin/python scripts/manage.py check
```

`configure.py` 以隐藏输入方式写入 `deployment/.env`，权限为 600。至少配置：

- Binance Demo API Key 和 Secret。
- 分析 Bot 与交易 Bot 的两个不同 Token。
- 被授权管理者的 Telegram chat_id 与 user_id。

可选项：Telegram MTProto API_ID/API_HASH、可选数据源密钥、Nansen token 地址映射。

确认只读检查、账户模式和 Bot 可达后，才运行：

```bash
.venv/bin/python scripts/manage.py enable-demo
```

该命令不主动发送测试订单，只开启后续有效信号的消费。紧急停止新仓使用交易 Bot 的暂停按钮或 `scripts/manage.py disable-entries`；已有仓位的保护仍会继续。

## 外部登录

首次 TradingView 登录使用：

```bash
bash scripts/tradingview_login.sh
```

Telegram 信息采集使用用户 MTProto 会话，不是 Bot Token。填入 API_ID/API_HASH 后暂停 worker，再运行：

```bash
.venv/bin/python scripts/run.py .venv/bin/python scripts/telegram_login.py
```

完成后重启 worker。会话失效时重复此流程。

## 更新与排查

```bash
git pull --ff-only
.venv/bin/python scripts/manage.py start --build
.venv/bin/python scripts/manage.py status
docker compose --env-file deployment/.env -f deployment/compose.yml logs --tail 100 worker trader
```

运行前先备份：`.venv/bin/python scripts/backup.py`。恢复、订单异常和重启的细节见 `OPERATIONS.md`。
