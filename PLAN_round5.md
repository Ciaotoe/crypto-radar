# PLAN: 边际研究回路（第五轮，2026-10-25）

第四轮已把系统修到"能产出信号且阈值可衡量"，但 6 币种 × 170 天实测
胜率 33.3%、单笔期望 -0.153R —— 现有趋势回调规则无边际。本轮不改生产
阈值（样本密度已低，继续收紧只会退化为个位数），转向建立**新信号特征
的可测量研究回路**。历史轮次记录保留不动。

## 背景与实测数据边界（2026-10-25 探测）

- OKX `history-candles` 1H 深度远超之前认知：BTC 最早 2019-12，ETH 2019-12，
  SOL 2021-01，DOGE 2020-07；XRP/ADA/LINK/LTC/DOT/TRX/UNI/FIL/NEAR/AVAX
  均有 2021-06 数据；APT/ARB/OP/SUI 更晚上市。**可用样本 ≈ 18 币种 ×
  4–6 年，而非 6 × 170 天**。
- 15m 深度实测 ~15 天（1,500 根），15m 触发门依旧无法历史重放，所有
  重放数字仍是上界。
- 资金费率历史仅 ~1 个月深度，重放 funding 仍按 0 计；但研究回路改为
  **扣双边 taker+滑点成本 0.14%**，比第四轮口径更接近实盘。
- 语料 14 币种（BTC/ETH/SOL/DOGE/XRP/ADA/AVAX/LINK/LTC/DOT/TRX/UNI/FIL/NEAR），按今日流动性名单选币 → **存在幸存者偏差**，结论按此口径标注。

## 目标

1. 建立离线的多币种长历史 1H 语料（`research/data/`，gitignore，可复现下载）。
2. 建立特征筛选回路 `research/screen.py`：特征 → 事件 → 统一离场模型 →
   训练/测试分段统计（Wilson 区间 + t 值），不经过生产判定栈。
3. 用该回路首批验证两个候选信号族（波动率压缩突破、量价失衡），如实报告，
   不宣称找到边际除非测试段数据支持。

## 步骤与验收标准

1. [x] **语料下载**（`research/fetch_corpus.py`）
   - 14 币种全历史 1H（~2500 天；APT/ARB/OP/SUI 上市晚于 2021-06，排除保持语料矩形）录制为与 `backtest.py --record` 相同的
     快照格式，存 `research/data/`（gitignore）。
   - 验收：每币种快照可被 `backtest.load_snapshot` 加载；BTC 语料起点
     ≤ 2020-01；总下载日志无静默失败（单币失败显式报错并继续）。
2. [x] **`backtest.simulate` 改为数组内核**（语义不变，消除逐 bar `iloc`）
   - 验收：`tests/test_backtest.py` 全过；doge 快照重放统计与改造前
     逐项一致（PASSED/胜率/expR/totalR）。
3. [x] **`research/screen.py`**：特征族注册表 + 向量化事件检测 +
   统一离场模拟（复用 `backtest.simulate`，扣 0.14% 成本）+
   训练段（<2025-01-01）/测试段（≥2025-01-01）分段统计。
   - 验收：`tests/test_research_screen.py` 覆盖事件检测、分段归属、
     成本扣减、Wilson 区间数学；对同一批事件，screen 的离场模拟与直接
     调 `backtest.simulate` 结果一致（防两套口径漂移——第四轮教训）。
4. [x] **首批筛选运行 + 如实报告**：baseline（无条件）+ `squeeze_breakout`
   + `vol_imbalance`，全语料，分段输出 n/winR/expR/t/PF。
   - 验收：结果写入 `research/results/`（gitignore）与 PLAN 完成记录；
     测试段期望与 Wilson 下界如实呈现，正/负都记录。
5. [x] 文档同步（AGENTS.md 研究回路一节、README 提及）、提交。

## 已知约束

- 研究回路**不碰** `strategy.py` 生产阈值与 `signals.json` 契约；筛出的
  特征若要进生产，另立一轮并过 `backtest.py` 漏斗验证。
- 特征参数写在各 family 定义内并注明含义，不进环境变量（未定型）。
- 统计纪律：训练段调参、测试段只读一次；测试段 t<2 或期望变号 = 不提升，
  不得回头改测试段口径。
- 15m 无法重放、funding=0、幸存者偏差 —— 所有结论标注为"1H 收盘口径、
  成本 0.14%、上界"。
- 不新增依赖（pandas/numpy/requests 已有）。

## 进度记录

- 2026-10-25: 探测确认 OKX 1H 历史深度（BTC 2019-12 起）；15m ~15 天；
  funding-rate-history ~1 个月。
- 2026-10-25: 步骤 2 完成 —— `simulate` 拆为数组内核 `simulate_walk` +
  DataFrame 适配器；doge 快照重放 JSON 与改造前逐项一致（DIFF: none），
  全量 85 测试通过。
- 2026-10-25: 步骤 3 完成 —— `research/screen.py`（3 个特征族 + 分段统计）
  与 `tests/test_research_screen.py`（8 项，含内核同一性断言）全过。
  入场口径修正：事件 i 根收盘成立 → i+1 根开盘入场（无前视、fill 无歧义）。
- 2026-10-25: 步骤 1 完成 —— 14/14 币种快照落地（BTC 2019-12-19 起，
  总计 78.8 万根 1H bar，跨度 ~6.8 年）；specs 均为真实合约规格（无
  approximated）；下载日志无失败。验收项"BTC 起点 ≤ 2020-01"满足
  （2019-12-19）。
- 2026-10-25: BTC 单币冒烟运行 screen.py 通过（baseline 全段 expR
  -0.154R t=-5.9，符合"无条件入场必亏成本"的预期方向）。
- 2026-10-25: 首轮全语料运行暴露数据缺陷 —— ADA 2020-03 上市初期死带
  bar（OHLC 全平、vol=0，tick 舍入后 ATR>0 但 ~1e-12 相对价格）使
  baseline expR 污染到 -1.3e8 R。新增 MIN_ATR_FRAC=1e-4 守卫 + 回归测试
  （含微振幅变体，防假通过——第四轮教训），提交 81918ef。
- 2026-10-25: 步骤 4/5 完成。全量 94 测试通过；结果与结论见下方完成记录。

## 完成记录（第五轮，2026-10-25）

### 全语料筛选结果（14 币种 × 2019-12–2026-10，78.8 万根 1H bar）

口径：事件收盘成立 → 次根开盘入场；离场走 backtest.simulate_walk 同一内核；
每笔扣 0.14% 双边成本；funding=0（上界）；幸存者偏差（按今日流动性选币）。

| family | 段 | n | winR [Wilson95] | expR | t | PF |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | train | 23,417 | 0.362 [.356,.368] | -0.126 | -14.7 | 0.81 |
| baseline | test | 9,268 | 0.356 [.346,.366] | -0.159 | -11.7 | 0.77 |
| squeeze_breakout | train | 5,321 | 0.378 [.365,.391] | -0.070 | -3.9 | 0.89 |
| squeeze_breakout | test | 2,139 | 0.350 [.330,.370] | -0.152 | -5.5 | 0.77 |
| vol_imbalance | train | 6,643 | 0.389 [.378,.401] | -0.057 | -3.5 | 0.91 |
| vol_imbalance | test | 2,912 | 0.385 [.368,.403] | -0.070 | -2.8 | 0.89 |

结果文件：`research/results/screen_*.json`（gitignore，可用 --json 复现）。

### 如实结论

- **两个候选信号族都没有正边际**。按预注册纪律（测试段 t<2 或期望变号 =
  无提升）：squeeze_breakout 测试段期望恶化（-0.070 → -0.152，train/test
  变号级退化），直接淘汰；vol_imbalance 测试段 -0.070R、t=-2.80，
  **显著为负**，同样不提升。
- 但两者都**显著优于 baseline**（vol_imbalance 全段 -0.061R vs baseline
  -0.136R，train/test 两段方向一致），说明特征本身携带信息——量价失衡
  选出的时段确实比随机时段的 2R/3R 几何亏损更少。亏损主体是 0.14% 成本
  加上"1H 收盘入场 + 固定 1.5 ATR 止损"几何本身的高止损率（~57%）。
- 推论（待下一轮验证，不在本轮宣称）：正边际的缺口不在"事件选择"而在
  "离场几何"——vol_imbalance 事件上换更宽止损/跟踪离场/更早 TP1 的
  消融是下一个最便宜的实验；生产判定栈的 S5 偏离门（消融中边际最大
  +0.129R）与本回路的 vol_imbalance 属同类信息（入场时机质量），方向
  互相印证。
- 本轮不碰生产阈值、不下"找到边际"结论。

### 独立只读复核（plan-reviewer）结果与处置

复核判定 **PASS**：语料完整性（14/14 可加载、bar 数与日志一致、specs 无
approximated）、内核唯一性（用重构前版本重跑 doge 对比逐项一致）、无前视
（rank/shift/rolling 逐表达式核查）、索引对齐（经验证与纯位置掩码逐位相等）、
Wilson/t/PF 数学、PLAN 数字与 results JSON 逐格一致、红线（strategy.py /
signals.json / .env 零触碰、无新增依赖、gitignore 覆盖）均确认。

三项次要发现，均已修复：

1. **F1** `fetch_corpus.py` 的 `except Exception` 接不住 `record_snapshot`
   抛出的 `SystemExit`（BaseException），短历史币种会终止整个下载循环而非
   "显式报错并继续"。已改为 `except (Exception, SystemExit)`。本轮语料
   14/14 成功，属潜在缺口非错误结果。
2. **N1** 内核同一性测试原断言是"自己调自己"恒真。已改为端到端：从
   `run_family` 产出反推离场输入，直接调 `simulate_walk` 逐项比对
   outcome/净 pnl/risk_pct（若 screen 出现第二套离场数学该测试必失败）。
3. **N2** 文本模式 PF=0.0（段内零胜）会误渲染为 "inf"。改为
   `is not None` 判断。JSON 输出与本轮报告数字不受影响。

### 提交

- b5be84b simulate 数组内核化（doge 快照逐项一致）
- 2855624 研究回路（fetch_corpus + screen + 9 项测试 + 文档）
- 81918ef 死带守卫 + 回归测试
- ff368b8 第五轮完成记录
- （待提交）复核三项处置：F1 SystemExit 捕获 / N1 端到端内核一致性测试 /
  N2 PF 渲染
