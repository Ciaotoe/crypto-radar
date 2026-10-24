# Crypto radar

动态跟踪 OKX USDT 永续流动性前 30 的顺势回踩机会（6 小时刷新，带滞后替换）。只生成提醒，不连接账户或下单。2026-10-21 起新策略与追踪层同源，均使用 OKX v5 公开接口，单一数据源不做跨市场回退。

## 策略数据

4H/1H/15m 指标仅使用已收盘、连续完整的 K 线（OKX confirm 字段过滤，未收盘 bar 永不进指标）；4H 至少 750 根用于 EMA200 预热。永续最新价、买卖报价、标记价、资金费率独立获取；同一轮 `quote()` 内只取一次资金费率。结算周期按接口时间戳推导并以 8 小时为预算上限——OKX 费率极低时会跳过某次结算，观测间隔可达 16 小时，此时按 8 小时保守预算成本。数据缺失、过期或接口异常时拒绝新策略，不降级到现货行情或其他交易所数据。

OKX 公开接口本机直连可用；本地模拟测试不代表真实信号或盈利表现。追踪层指标同样只取已收盘 K 线，实时价取自 ticker。标的池一轮内批量取 `tickers` / `mark-price` 各一次，`funding-rate` 仅对入池币逐币取且本轮复用。4H K 线按已收盘时间缓存到 `data/klines_4h.json`（不提交），同一 4H bar 内不重复拉取；candles 请求按 40 次/2 秒限频并自动重试，单个币失败只跳过该币。

## 入场筛选

4H EMA20/50/200 排列、EMA20 最近三个收盘间隔斜率、ADX≥22、DI 方向及收盘价需一致。1H 最近四根 K 线要有均线回踩/反弹证据，最新收盘需重新站回均线、突破上一根高/低点并有同向 RSI 改善。多头 RSI 40–65，空头 35–60。

只接受最新 1H 收盘后 45 分钟内的确认。除 1H 反转外，最新已收盘 15m 还必须同向收盘并站上/跌破 15m EMA20；15m 缺失或指标不完整不推送。现价距离 1H EMA20 最多 1.5 ATR、4H EMA20 最多 2 ATR、确认收盘最多 0.5 ATR；重新失守或价格跑远即放弃。阈值是待验证的明确筛选口径，不代表胜率；更严格筛选可能显著减少机会。

## 点位、成本与风险

入场区间为确认时刻买卖价 ±0.1 ATR；推送后 60 分钟内只认已收盘 15m 触带成交，未成交记 `not_entered` 并释放槽位，不追价。成交价统计统一按最不利入场价（做多 `entry_max`、做空 `entry_min`），以保守口径计算。价差或标记价偏离过大即放弃。止损取近端结构外沿（结构 ±0.25 ATR）与 1.5 ATR 的更远者，上限 3 ATR 且不超过价格的 3%；TP1/TP2 按最不利入场扣除双边 taker 费率、滑点与不利资金费后的净 2R/3R 求解。仓位按单笔风险不超过账户权益 0.5%、名义价值不超过权益计算；OKX 合约为张数模式（1 张 = ctVal 个币），数量按整数张向下取整，凑不足最小张数即放弃，不加仓凑单。

### 结构边界（2026-10-24 修正）

`structure_boundary` 只认**显著**结构：3 根 K 线局部极值须比其邻居突出至少 `RADAR_MIN_PROMINENCE_ATR`×ATR（默认 1.0）才计入。前方无显著结构是开阔空间，按净 2R/3R 放行，`structure_boundary` 记为 `null`。

显著结构作为**目标上限**而非否决门：TP2 取 `min(净 3R, 结构前可达空间 × RADAR_BOUNDARY_CAP_FRAC)`（默认 0.85）；仅当可达空间连净 2R 的 TP1 都容不下时才拒绝。

旧口径把任意 3 根极值当阻力，并要求它落在 TP2 之后。实测（6 币种 × 170 天、24,612 根已收盘 1H bar）前方最近枢轴中位仅 +0.19 ATR、突出度同为 0.19 ATR，而 TP2 中位在 +6.01 ATR，通过率 0/153 —— 该门在数学上不可能满足，等于关闭整个策略，且生产档案自 `stats_epoch` 起带真实 `strategy_version` 的信号为 0 条。修正后同一数据回放 `PASSED=24`。

阈值与盈亏比是待验证的筛选口径，不代表回测过的收益优势：修正后重放单笔期望仍为 **-0.153R**（胜率 33.3%、PF 0.76），即信号流恢复非零，但当前规则族**尚无已验证的正边际**。

推送前用新行情复核方向与 15m 触发，现价已越过止损/TP1 或偏离入场带超过 1 ATR 即放弃本轮推送，不改点位硬推。AI（Gemini）仅作消息面否决：只能以重大风险拦下推送，不能修改点位或解释策略成立；复核调用失败时放行，不依赖单一外部模型。

## 推送流程

动态标的池扫描确认机会后按可比指标择优（4H ADX 优先，并列时取按 1H ATR 归一的 4H EMA20 贴近度），最多同时持有 3 单、同方向 ≤2、总计划风险 ≤1.5% 权益；首选候选构建失败不阻断其余候选。推送文案与存档点位由同一份规则计划生成，追踪档案只存点位、预案与文案。

阈值与盈亏比是待验证的筛选口径，不代表回测过的收益优势；本地模拟与公开行情检查不构成真实信号。

## VPS 部署（Debian 12）

生产调度已从 GitHub Actions 迁到 VPS：systemd 常驻循环睡到下一个 15 分钟边界 +15 秒再扫描，单实例锁防重入；进程异常退出由 systemd 自动拉起。Actions 保留 `workflow_dispatch` 仅供手动应急，不再定时运行。

```bash
# 仓库放到 /opt/crypto-radar 后，以 root 执行；REPO_URL 仅在目录不存在时需要
REPO_URL=git@github.com:you/crypto-radar.git bash deploy/install.sh
# 填入 /etc/crypto-radar.env 的 TG_BOT_TOKEN / TG_CHAT_ID / GEMINI_API_KEY
systemctl start crypto-radar.service
journalctl -u crypto-radar.service -f
```

- `/etc/crypto-radar.env`：权限 600；`RADAR_HEARTBEAT=1` 每轮成功/失败推一条简短 TG，默认关闭；`RADAR_DAILY_HEARTBEAT=1` 额外发送每日汇总。
- `crypto-radar.service`：常驻循环，扫描间隔对齐 15 分钟边界 +15 秒；异常退出 `Restart=always`。
- `crypto-radar-daily.timer`：每日 08:00:30（Asia/Shanghai）汇总标的池、在持数量和最近扫描时间。
- `crypto-radar-backup.timer`：每日 08:00（Asia/Shanghai）备份 `data/` 状态到 `/var/backups/crypto-radar`，保留 30 天。
- 日志用 `journalctl -u crypto-radar.service`；不要把密钥或 `data/` 提交回仓库。

## 验证

`uv run --no-sync python -m unittest discover -s tests -p 'test_*.py' -v`

净口径报表（只读，不发网络请求）：

`uv run --no-sync python report.py`

判定栈重放（只读，不写 `data/`、不发 TG）—— 用真实 OKX 历史数据回答"哪道门杀掉了多少 setup"与"幸存者期望多少"：

```bash
uv run --no-sync python backtest.py --symbols BTC/USDT,ETH/USDT --days 120
uv run --no-sync python backtest.py --snapshot tests/fixtures/doge_1h.json.gz   # 离线
uv run --no-sync python backtest.py --symbols DOGE/USDT --days 170 --record OUT.json.gz
```

重放直接调用 `strategy.signal_direction` / `strategy.build_plan`，不复制判定逻辑，避免"回放一套口径、生产另一套口径"再次掩盖问题。已知近似（结果偏乐观，输出中显式标注）：15m 触发门被 stub 放行（OKX `history-candles` 的 15m 深度仅约 22h），故信号数与期望是生产口径的**上界**；历史资金费率不可得，成本按 0 计；成交与离场按已收盘 1H bar 判定，同根先止损后止盈。
