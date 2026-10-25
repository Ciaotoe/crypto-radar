# 项目规则（trade）

继承全局 AGENTS.md；Python 项目，一律 `uv run --no-sync`。密钥经环境变量注入（TG_BOT_TOKEN / TG_CHAT_ID / GEMINI_API_KEY），`RADAR_HEARTBEAT=1` 可选开启每轮心跳；`.env` 已被 .gitignore 覆盖，不提交。

## 架构边界（2026-10-20 策略完善轮确立）

- `strategy.py` 是新策略的独立数据入口与规则层：行情拉取、信号确认、点位/成本/仓位全部由代码生成，**AI（Gemini）只可否决，不生成点位、不解释成立**；复核调用失败放行，推送不得依赖单一外部模型。
- `crypto_radar.py` 中 `get_market_data` / `fetch_klines_since` / `review_signals_with_ai` / 巡检循环属于追踪层，与新策略解耦，改新策略时不得触碰追踪状态机口径。`universe.py` 只负责动态标的池；`report.py` 只读统计，不得写 `signals.json`。
- 点位契约（signals.json 档案的 strategy 字段：stop/tp1/tp2/hold_hours/timeout_action/entry_min/entry_max/net_rr/risk_pct/cost_pct_est/signal_close_ms/strategy_version/text）如需变更，先列迁移与兼容范围并征得确认。
- 结构边界口径（2026-10-24 第四轮确立）：`structure_boundary` 只认显著结构 —— 3 根 K 线局部极值须比邻居突出 `RADAR_MIN_PROMINENCE_ATR`×ATR（默认 1.0），否则视为 1H 噪声丢弃；已移除旧的"24h 极值"兜底项。**该字段可为 `null`（前方无显著结构 = 开阔空间），消费方不得假设是正数**；`format_strategy_text` 已分支渲染。显著结构是**目标上限而非否决门**：TP2 = `min(净 3R, 可达空间 × RADAR_BOUNDARY_CAP_FRAC)`（默认 0.85），仅当连净 2R 的 TP1 都容不下才拒绝。改这两条前先跑 `backtest.py` 看漏斗，不要凭直觉收紧。
- 入场契约（2026-10-22 起）：新信号 `entry_model=pending`，`triggered_at + entry_window_min(60)` 内只认已收盘 15m 触带成交；成交后 `entry_price` 取最不利入场价，`push_price` 保留推送时价格；未成交记 `exit_reason=not_entered`、`stats_eligible=false`。旧档案迁移为 `entry_model=immediate`、`entry_filled_at=triggered_at`，行为不变。

## 已知环境约束

- 2026-10-21 系统整体迁移 OKX v5 公开接口（本机直连可用）：策略层（`strategy.py`）与追踪层（`crypto_radar.py`）同源，单一 OKX 数据源，**失败不回退**其他交易所——回退会重新引入跨市场基差错位。
- OKX 张数语义：合约数量单位是"张"（1 张 = ctVal 个币），lotSz 是面值不是步长；仓位取整按整数张向下。资金结算周期按接口时间戳推导并封顶 8h 预算（OKX 费率极低时会跳过某次结算，观测间隔可达 16h）。
- 生产调度在 Debian 12 VPS 的 `crypto-radar.service` 常驻循环（每 15 分钟边界 +15 秒）；GitHub Actions 只保留 `workflow_dispatch`，不再定时，避免双写/重复推送。部署文件在 `deploy/`，环境变量放 `/etc/crypto-radar.env`（600）。
- 每轮心跳由 `RADAR_HEARTBEAT=1` 控制（默认关闭）；每日汇总由 `RADAR_DAILY_HEARTBEAT=1` 控制，08:00 汇报标的池/在持/最近扫描。发送失败只写日志，不得影响巡检。每日 08:00 本地备份到 `/var/backups/crypto-radar`，保留 30 天。
- 4H K 线缓存到 `data/klines_4h.json`（gitignore，按最后收盘时间失效）；每轮批量取 `tickers` / `mark-price` 各一次，`funding-rate` 逐币取且本轮复用；candles 请求 40 次/2 秒限频、最多重试 3 次，单币失败只剔除该币，缓存写失败不影响扫描。
- 主循环持有 `data/radar.lock` 单实例锁并覆盖读取→巡检→落盘；`signals.json` 只允许 `runtime.atomic_write_json` 原子替换，禁止裸写。`data/radar.lock` 已 gitignore。

## 验证

- 策略测试：`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy*.py' -v`
- 全量测试：`uv run --no-sync python -m unittest discover -s tests -p 'test_*.py' -v`
- 净口径报表：`uv run --no-sync python report.py --json`（只读，不发网络请求）
- 判定栈重放：`uv run --no-sync python backtest.py --snapshot tests/fixtures/doge_1h.json.gz`（离线只读，约 1.3s）；联网口径 `--symbols ... --days ...`。**改任何筛选阈值或点位规则后必须跑一次看漏斗**，`PASSED=0` 即门槛在真实数据上不可触发。
- 测试需要冻结 `strategy.time.time`（行情新鲜度校验依赖真实时钟，fixture 行情是固定时间戳）；`crypto_radar` 模块级读取密钥环境变量，测试 patch 模块属性而非 `os.environ`。
- **手工构造的行情 fixture 只能覆盖单点几何，不能用来判断门槛是否可触发**：`strategy_fixtures.market()` 把阻力植入 9–11 ATR 外（真实显著结构中位仅 1.6 ATR 外），曾使 74 个测试全绿而生产零信号。这类保证由 `tests/test_backtest.py` + 录制快照承担；新增"门槛能出信号"类断言一律走快照，不要往 `strategy_fixtures.py` 里加手工开阔空间。
- 筛选阈值（ADX 下限、RSI 带、反转收盘位、偏离倍数、止损 ATR/百分比上下限）是 `strategy.py` 的具名模块常量，`backtest.py` 运行时读取同一份，不得在回放侧重定义字面量。

## 研究回路边界（2026-10-25 第五轮确立）

- `research/` 是独立的边际研究区：`fetch_corpus.py` 下载多币种全历史 1H 语料到 `research/data/`（gitignore，可复现），`screen.py` 做特征族筛选（事件 → 离场 → 训练/测试分段统计）。**研究回路不碰 `strategy.py` 生产阈值与 `signals.json` 契约**；筛出的特征进生产须另立一轮并过 `backtest.py` 漏斗验证。
- 离场模拟唯一内核是 `backtest.simulate_walk`（数组口径），`backtest.simulate` 与 `research/screen.py` 都调它，禁止复制离场数学（第四轮两套口径漂移教训）。
- 筛选统计纪律：训练段（<2025-01-01）调参、测试段只读一次；测试段 t<2 或期望变号 = 无提升，不得回头改测试段口径。每笔扣 0.14% 双边成本；funding=0 与幸存者偏差须在结论中显式标注。
- OKX 数据深度实测（2026-10-25）：1H 自 2019-12（BTC）；15m 仅 ~15 天（15m 触发门不可重放，所有重放数字是上界）；funding-rate-history 仅 ~1 个月。
