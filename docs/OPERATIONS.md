# 运维与恢复

所有命令在 `/root/tradingview-analysis` 执行。初始配置用 `.venv/bin/python scripts/configure.py`，应用用 `.venv/bin/python scripts/manage.py start`。版本更新用 `start --build`，业务检查用 `check`，容器与最新周期状态用 `status`。

## 启停与配置

`enable-demo` 在只读连通性/账户模式检查通过后打开新仓权限；`disable-entries` 立即持久化暂停新仓，已有仓位管理继续。两个 Bot `/pause` 和 `/resume` 均先预览再保存。分析暂停不会撤销已发布信号的剩余有效时间；紧急停止新仓请暂停交易侧。

模型 CLI 已登录，模型固定 `gpt-5.6-terra / medium`。模型超时、额度失败、结构不合规或行情不足时周期记 FAILED，不发布新信号。网页/频道结构变化在 `/sources` 和来源健康表记录。

配置变化后 `manage.py start` 更新容器环境并重启宿主机 bridge。数据库设置由 Bot/API 实时读取，不需要重启。不要运行第二个同 Token Bot 轮询器或同一交易账本的第二个 worker；服务使用进程锁，分析另有 PostgreSQL advisory lock。

## 订单异常

查看交易 Bot `/errors`、`/positions`、`/trades`。`/retry 交易ID` 为持久化的单次手动请求，由交易 worker 核对原订单后执行；不能自动给 UNKNOWN、SUBMITTING 或查询不到的 ACK 再发一张订单。确认已取消的保护单可以新身份补挂原价，历史身份留账。

交易所明确拒绝的保护单最多两次自动提交。仍失败时保留 OPEN 并告警；手动操作账户可能让自动账本与持仓不一致，需要先核对。取消/修改 Demo 账户中的系统保护单不会改变系统保存的原 TP/SL 目标。所有账户写操作固定 Demo 域名。

## 备份与恢复

`.venv/bin/python scripts/backup.py` 使用 pg_dump 与 SQLite backup API 生成快照，权限700，包含本地密钥。备份文件不要提交 Git 或通过公开链接分享。源代码可独立备份，运行目录和秘密文件被 Git/Docker build 忽略。

恢复到同一部署路径的顺序：

1. 先停止 api、worker、trader 和两个 Bot，保持 postgres 运行；停止宿主机 bridge。
2. 先备份当前状态，再用 `pg_restore -U signals -d signals --clean --if-exists` 恢复 `postgres.dump`。该步骤会替换当前分析库，须明确选对备份。
3. 保留当前 SQLite 目录副本，把备份中 analysis/trading 的数据库放回对应 `runtime` 目录。关闭进程后处理旧 WAL/SHM；不能把旧 WAL 和恢复的 DB 混用。
4. 必要时恢复 `deployment.env` 到 `deployment/.env`，权限600。Telegram Session 放回 analysis/telegram，权限700；TradingView 重新人工登录。
5. 运行 `.venv/bin/python scripts/manage.py disable-entries`，再 `start`。先保持暂停，核对 Demo 真实账户、账本和 UNKNOWN 意图；完成核对后由 Bot 恢复新仓。

不要把备份回滚当作交易回滚：交易所订单不会因本地恢复消失。丢失本地订单身份时需人工核账，不能直接恢复开仓。备份为各数据库分别一致，不提供跨库原子恢复或交易所历史回滚。

