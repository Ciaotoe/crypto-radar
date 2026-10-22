# 策略升级计划（初稿，2026-10-22，待确认）

> 本文是评审后的改造初稿。原始 7 条清单的处理方式见文末对照表。
> 原则不变：单一 OKX 数据源、失败不回退；点位由代码生成，AI 只可否决；
> 不以固定阈值宣称收益已验证。

## 0. 结论摘要

- 第 7 条（VPS）是第 1/3/5 条能真正落地的前置，排第一。
- 第 6 条（统计）应紧随其后，先固定衡量口径，再谈策略优劣。
- 第 2、5 条必须合并成一个决策：**即时入场还是条件挂单**。只有条件挂单，
  “入场确认/失效单”才有实际意义；即时入场模型下第 5 条近乎空转。
- 第 2 条的“压缩止损提高 TP 命中率”不成立：止损更窄会放大仓位与噪声止损
  频率。本轮 15m 只做触发与失效检测，**不收紧止损**。
- 第 1、4 条方向合理，但需要滞后池、批量行情、聚合风险上限，否则会引入
  选币抖动、请求放大和相关性和仓。
- 持仓消息面复核由固定 2 小时改为“事件触发 + 24h 兜底”，减少无效 Gemini
  调用；机械巡检仍按 15 分钟运行，两者互不影响。

## 1. 动工前必须确认的决策

### D1. 入场模型

- **A. 即时入场（现状）**：推送时现价必须在入场带内，用户立即入场。
  改动最小，但“拉飞没上车/未触达失效”只在推送后的极短窗口有意义。
- **B. 条件挂单（推荐）**：15m 收盘同向确认后推送“入场带 + 60 分钟有效期”。
  推送时价格可以在带外（等待回踩），60 分钟内触达入场带才算成交；未触达记
  `not_entered`，释放并发槽。与第 2、5 条语义一致，但推送文案、档案契约、
  MFE/MAE 起点都要改。

选 B。若想先小步，选 A，同时把第 5 条降级为事后审计，不引入 `not_entered`。

选 B 时必须同步改 `ensure_pushable`：推送时不再要求现价在带内（否则挂单
退化成即时入场），只要求 1H 信号仍成立且价格未越过失效边界；现价在带内
属于“可能立即成交”，在带外属于“等待回踩”。

### D2. 并发上限

推荐 `MAX_OPEN=3`、同方向 ≤2、总计划风险 ≤1.5% 权益；先跑满 30 个平仓样本，
再评估是否放宽到 5。理由：山寨同涨同跌，5 单并发在统计上接近 1 个 beta 大单，
而 5 × 0.5% = 2.5% 权益风险，与“快速积累独立样本”的目标并不一致。

### D3. 统计起点

推荐 `stats_epoch=上线日`：旧档案只作历史参考，不混入新口径；`manual_reset`
永久排除；未成交单不计入胜率分母，但单独报告“未成交率”。

### D4. VPS 形态

需确认：厂商/区域（需实测 OKX、Telegram、Gemini 可达）、是否保留
Actions `workflow_dispatch` 作为应急（保留则必须移除 schedule，防双进程双推送）。

## 2. 分阶段计划

### P0. VPS 常驻 + 15 分钟对齐（原第 3、7 条）

**目标**：摆脱 Actions 丢拍，巡检贴合 15m 收盘。

**改动**
1. systemd 常驻循环：睡到下一个 15m 边界 +15 秒再跑（等 OKX `confirm`
   落定），单轮异常不退出进程。
2. 环境：`EnvironmentFile` 权限 600；chrony/NTP 校时；日志轮转；
   连续 3 轮失败告警。
3. 每日 08:00（北京）心跳：标的池大小、在持数量、最近扫描时间。
4. 数据模式：`signals.json` 留在 VPS 本地，取消每轮 commit；每日备份；
   已结案记录超过 90 天移入归档文件，防止档案无限增长。
5. 并发安全：主循环启动即获取排他锁，覆盖“读取 → 巡检 → 落盘”完整生命周期，
   不能只锁写入瞬间；所有档案写入走“临时文件 + fsync + `os.replace`”原子替换。
   锁实现按平台分支（Linux `fcntl.flock`，Windows 测试用锁文件或跳过），
   `universe.json` 同理。读方（report/备份）因原子替换可无锁读完整快照。

**验收**
- 连续 24h 扫描 ≥92 次；`kill -9` 后自动恢复；时钟偏差 <1s。
- 心跳可收；无双进程、无重复推送。
- 并发写测试：两进程同时启动只有一个拿到锁；写入中途 kill 不产生损坏 JSON。
- 硬前提：`strategy.quote` 的 ±120 秒新鲜度校验会因时钟漂移拒绝全部信号。

### P1. 净口径统计与报表（原第 6 条）

**目标**：胜率、盈亏比、单笔期望可复核，且不被成本与幸存者偏差污染。

**口径**
- 主队列：`triggered_at >= stats_epoch` 且已结案且 `stats_eligible`。
- 版本切片：按 `strategy_version` / `universe_version` 分组；`stats_epoch` 只做
  总起点，规则变更不重置历史，避免每个版本样本都被切碎。
- 胜：`took_profit` / `breakeven_after_tp1`（TP1 已触达）；
  负：`stopped_out`；
  中性：`timeout` / `trend_reversed`（按市价单列，不强行二分类）；
  未成交：`not_entered`（单独分母）。
- 净收益：`pnl_now` 扣除双边 `TAKER_FEE + SLIPPAGE` 与资金费估算，落盘为
  `pnl_net_pct`；旧档案的毛收益不冒充净收益。
- R 倍数：`pnl_net_pct / risk_pct`，`risk_pct` 按最不利入场到止损计算。
- 报表输出：N、胜负/中性/未成交数、胜率 + Wilson 95% 区间、平均盈利 R、
  平均亏损 R、单笔期望 R、盈亏比、最大连续不利 R、平均持仓时长、MFE/MAE；
  按方向、按月分组。N < 30 标注“观察中”。

**改动**
- 新增 `report.py`，只读 `data/signals.json`，输出 Markdown/JSON，不发网络请求。
- 可选出周报（默认写文件，不推 TG，避免刷屏）。

**验收**
- `tests/test_report.py` 用合成档案覆盖胜负/中性/未成交/旧数据隔离；
- 报表运行不修改 `signals.json`；无新增依赖。

### P2. 入场生命周期与失效单（原第 5 条，依赖 D1）

**目标**：区分“计划有效”和“实际有机会成交”，避免把没上车的单算进胜负。

**改动（D1=B）**
- 有效期锚定 `triggered_at + 60min`（推送后 60 分钟），不用
  `signal_close_ms` 锚定，避免推送晚时窗口被压缩。
- 只扫“已收盘”15m：`close_time > triggered_at`；若首根 K 线开盘早于触发时刻
  超过 60 秒（手动触发/调度漂移），跳过该不完整根，不用整根高低点判断成交。
  P0 对齐后首根只有 ≤15 秒重叠，允许使用并记录 `first_bar_overlap_sec`。
- 到期判定前先补扫 deadline 前所有已收盘 15m；进程重启/断网跨窗时必须先补扫
  再判 `not_entered`，禁止直接按墙上时间失效。
- 同一根 15m 同时覆盖入场带与止损：按“先成交、后止损”记为 `stopped_out`，
  计入 -1R 与成本，并落 `entry_ambiguous=true` 供审计；不按 `not_entered` 逃单。
- 成交价口径：推送时记 `push_price`；成交后把 `entry_price` 设为最不利入场价
  （long 用 `entry_max`，short 用 `entry_min`），MFE/MAE/PnL 与 R 都从该价起算；
  不做事后“更优成交”还原。旧信号 `push_price=entry_price`。
- `pending` 状态占一个并发槽（保守），失效后立即释放。
- MFE/MAE 从 `entry_filled_at` 起算；pending 期间不累计浮盈浮亏。
- 去重键增加 `(symbol, direction, signal_close_ms)` + 1 小时冷却，防止同一根
  1H 信号失效后在 45 分钟有效期内被重新推送。
- 推送文案改为“挂单区间 / 60 分钟有效 / 未成交失效”，同步 AGENTS.md 点位契约。

**兼容**
- 旧信号 `entry_model="immediate"`、`entry_filled_at=triggered_at`，行为不变。

**验收**
- 60 分钟内触达 → filled；未触达 → not_entered；触达后触损 → 正常记负；
- 重启跨过 60 分钟窗口：补扫后正确判定，不误记 not_entered；
- 同根 15m 触带 + 触损：记 stopped_out 且 `entry_ambiguous=true`；
- 手动触发晚于 15m 边界：不扫信号前的半根 K 线；
- 同一 `signal_close_ms` 不重复推送；旧档案加载不报错。

### P2b. 持仓复核改为事件触发（新增）

**目标**：不再固定每 2 小时调用 Gemini，只在有信息增量或机械预警时复核。

**触发条件（任一满足）**
1. 出现上次复核未见过的新闻头条（`seen_headlines` 增量非空）。
2. 本轮机械层为该信号产生预警事件：`drawdown` / `adx` / `stall` / `funding`。
3. 兜底：距上次复核满 `RADAR_AI_REVIEW_FLOOR_HOURS`（默认 24h）。
   不额外做 FNG 变化触发，避免再引入一个需要持久化的状态。

**节流与预算**
- 每轮最多 `RADAR_AI_MAX_CALLS_PER_SCAN`（默认 2）次调用；优先级：机械预警 >
  新头条 > 兜底；被跳过的信号保持 due，下一轮再处理。
- 无在持信号不抓新闻、不调 AI；新闻每轮只抓一次。
- 成功调用后才更新 `last_ai_review` / `seen_headlines`（按时间序保留最近 20 条）；
  调用失败不更新，下一轮重试。
- 新信号建档时把 `last_ai_review` 初始化为触发时刻，并用建档当轮的新闻快照
  初始化 `seen_headlines`；此后只有成功调用才更新，避免刚推送就重复复核。
- 结论去重保留：`last_ai_verdict` 相同不推；新结论才推一条预警。

**验收**
- 无新头条、无预警、未到 24h：0 次 Gemini 调用；
- 新头条 / 机械预警 / 24h 兜底：各触发 1 次；
- 每轮上限 2 次，超出的信号下轮重试；调用失败不丢状态；
- 无在持信号时 0 次新闻请求。

### P3. 并发持仓（原第 4 条）

**目标**：在严格质检下允许多个独立币种并行，同时限制组合风险。

**改动**
- 门禁从“有任何在持就全停”改为槽位计数；候选按 `candidate_score` 依次
  构造计划，直到槽位占满。
- 上限：`MAX_OPEN`（默认 3，见 D2）、同方向 ≤2、总计划风险 ≤1.5% 权益；
  单币种同方向去重保留。
- 每单仍走推送前复核与 AI 否决；记录组合快照（总风险、方向暴露）到日志。
- 报表按日/周聚类，注明并发样本相关性。

**验收**
- 模拟 3 个候选：占满即停；同向超限拒绝；同币种不重复；
- AI 否决不额外占用槽位；报表能识别并发批次。

### P4. 动态标的池（原第 1 条）

**目标**：跟踪全市场流动性前 30，自动轮动，且不引入抖动与请求风暴。

**改动**
- 批量取数：`tickers` / `instruments` / `mark-price` / `open-interest` 各一次；
  tickers / mark-price / open-interest 已实测按 `instType=SWAP` 返回全市场，
  instruments 一次返回全量（联调时确认）。`funding-rate` **不支持批量**
  （实测返回 50014），只对入池的 30 个币逐币取，并在单轮内去重
  （`quote()` 与策略入口共用一次），不跨轮缓存，避免 `nextFundingTime` 过期误拒。
- 排序：`vol24h × ctVal × price`（24h USDT 名义额），不用语义含糊的
  `volCcy24h`；叠加 OI 下限与绝对点差上限。
- 硬过滤：`linear + USDT 结算 + state=live`；用 `listTime` 排除上市不足
  125 天的合约（4H EMA200 需 750 根，短线热点币本来也过不了预热）。
- 池刷新每 6 小时；滞后带：现成员跌出前 45 名才移出，新成员进前 30 才纳入，
  上限 30。池与排名落盘 `data/universe.json`，供审计。
- 记录 `pool_rank` 到信号；在持/追踪中的币即使掉出池也继续追踪。
- 行情失败时保留上一版池子，不把池子清空；4H K 线按“最后收盘时间”缓存，
  冷启动约 190 请求/轮，热态约 100 请求/轮；candles 按 40 次/2 秒限频设置
  并发上限与重试，单币失败只剔除该币。

**验收**
- mock 批量响应，断言排序、滞后、预热/流动性过滤；
- 单币取数失败只剔除该币；池刷新失败不改变现有池；
- 30 币扫描请求数与耗时在预算内。

### P5. 15m 触发增强（原第 2 条，与 P2 同版本上线）

**目标**：用 15m 细化入场时机与失效检测，不改 1H 风险模型。

**改动**
- `strategy.closed_klines` 扩展 15m（仍只认 `confirm=="1"`，连续性/新鲜度
  校验同现有口径）。
- 触发规则（D1=B）：1H 确认有效 + 最新已收盘 15m 同向收盘并收回 15m EMA20，
  才允许推送；价格是否在带内不阻断推送，只决定是否立即成交。15m 数据不足或
  指标缺失则不推送。
- 失效：60 分钟到期统一记 `not_entered`；期间不因 15m 反向波动撤单，避免与
  触达/止损判定打架。同一根 K 线同时触带与触损时，以 P2 的“先成交、后止损”
  规则为准。
- **明确不做**：不用 15m ATR 收紧止损；止损仍锚定 1H 结构与 1.5–3 ATR；
  若未来要试窄止损，另立实验并先做统计验证。

**验收**
- 未收盘 15m 永不参与；15m 缺失或指标不完整时不推送，日志给出拒绝原因；
- 现有点位/成本/仓位测试全部通过；新增 15m fixture 覆盖触发与缺失两条路径。

## 3. signals.json 契约迁移（P1/P2/P4 一次冻结）

新增字段：

| 字段 | 类型 | 写入时机 | 旧档案迁移 |
| --- | --- | --- | --- |
| `signal_close_ms` | int | 建信号 | 缺失为 null，去重退回仅 active |
| `entry_model` | `immediate`/`pending` | 建信号 | 默认 `immediate` |
| `entry_filled_at` | ISO/null | P2 触达 | 旧信号填 `triggered_at` |
| `push_price` | float | 建信号 | 旧信号取 `entry_price` |
| `stats_eligible` | bool | 建信号/结案 | `manual_reset` 置 false，其余 true |
| `risk_pct` | float | 建信号 | 由 `strategy.stop` 与入场价推导，缺失为 null |
| `cost_pct_est` | float | 建信号 | 旧信号为 null，报表不冒充净收益 |
| `pnl_net_pct` | float/null | 结案 | 旧信号为 null |
| `pool_rank` | int/null | 建信号（P4） | 旧信号为 null |
| `strategy_version` | str | 建信号 | 旧信号为 `legacy` |
| `universe_version` | str/null | 建信号（P4） | 旧信号为 null |
| `entry_ambiguous` | bool | 成交/结案判定 | 默认 false |

- `exit_reason` 新增 `not_entered`。
- `entry_model=pending` 时，`entry_filled_at` 缺失且窗口未到期 = pending；
  成交时把 `entry_price` 更新为最不利入场价，`push_price` 保留推送时价格。
  旧信号一律视为 immediate，不参与 pending 逻辑。
- 迁移只做 `setdefault` 与派生，不改历史价格、点位、MFE/MAE。
- `strategy.simplify_plan` 增加 `risk_pct`、`cost_pct_est`、`signal_close_ms`。
- 主统计用 `stats_epoch` 隔离旧档案，并按版本切片；另设“历史参考”小节。
- 事件触发复核只改 `last_ai_review` / `seen_headlines` 的更新时机，不新增字段。

## 4. 原始清单对照

| 原项 | 判定 | 落点 | 口径修正 |
| --- | --- | --- | --- |
| 1 动态 30 币池 | 合理 | P4 | 滞后带、硬过滤、热币预热边界、批量取数、`pool_rank` 审计 |
| 2 4H+1H+15m | 部分合理 | P5 | 4H/1H 协同与阻力测算已存在；15m 只做触发，不收紧止损 |
| 3 巡检 15 分钟 | 合理但收益被夸大 | P0 | 依赖 VPS；对齐边界 +15s；新信号仍由 1H 收盘产生 |
| 4 并发 5 单 | 方向合理 | P3 | 默认 3 单、同向 ≤2、总风险 ≤1.5%；并发≠独立样本 |
| 5 入场确认/失效单 | 合理，需先定入场模型 | P2 | 60 分钟窗口；未成交率单列；同 1H 信号冷却 |
| 6 胜负与胜率报表 | 合理且优先 | P1 | 净成本 + R 口径 + 置信区间 + 旧数据隔离 |
| 7 迁移 VPS | 合理，前置 | P0 | 校时、心跳、本地数据、与 Actions 互斥 |

## 5. 非目标与已知限制

- 不调 ADX 22、RSI 区间、净 2R/3R、止损 1.5–3 ATR 等既有阈值；
  本轮只改生命周期、标的池、调度与度量。
- 不做回测、不宣称策略已盈利；所有报表结论受样本量与相关性限制。
- 不接订单执行；点位仍是信号，实际成交与滑点由用户承担。
- 15m 触发会减少推送数量，不直接改变成交率；`not_entered` 取决于推送后是否
  回踩入场带，由报表衡量。若未成交率长期 >50%，再评估回退即时入场。
- 7×24 连续撮合下按“挂单真实存在且价格穿过即成交”记账，成交价统一取最不利
  入场价；若用户未实际挂单，统计仍是“计划口径”，不是账户口径。
- 成交判定用 15m 最新价 K 线，止损计划写的是标记价；极端行情两者可能不同，
  追踪层仍按现有口径近似，不宣称逐笔精确。
- P0 对齐时首根 15m 与触发时刻有 ≤15 秒重叠；记录但不追求 tick 级还原。
- 不新增第三方依赖；报表与标的池用现有 requests/pandas + 标准库。

## 6. 验证

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_strategy*.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_tracker*.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_report*.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_universe*.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_ai_review*.py' -v
```

测试注意：行情类用例冻结 `strategy.time.time`；`crypto_radar` 模块级密钥
属性用 patch，不碰真实环境变量。

## 7. 顺序与依赖

- 顺序：P0 → P1 → P2/P5（同一版本上线）→ P2b → P3 → P4。
  P2 与 P5 合并上线，避免先用“仅触达”攒一批样本、再被 15m 触发推翻。
- P1 先建报表但暂不下结论；正式记账从 P2/P5 版本起，P4 换池时 bump
  `universe_version`，按版本切片，不重置历史。
- P0 未完成前，不在 Actions 上强上 15 分钟节奏。
- P2 改推送语义，需先确认 D1；P4/P5 依赖 P0 的稳定调度与批量取数预算。
- P2b 不改变机械巡检节奏，只减少 Gemini 固定调用。
