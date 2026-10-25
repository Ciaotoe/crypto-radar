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

1. [ ] **语料下载**（进行中，后台）（`research/fetch_corpus.py`）
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
4. [ ] **首批筛选运行 + 如实报告**：baseline（无条件）+ `squeeze_breakout`
   + `vol_imbalance`，全语料，分段输出 n/winR/expR/t/PF。
   - 验收：结果写入 `research/results/`（gitignore）与 PLAN 完成记录；
     测试段期望与 Wilson 下界如实呈现，正/负都记录。
5. [ ] 文档同步（AGENTS.md 研究回路一节、README 提及）、提交。

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
- 2026-10-25: 步骤 1 语料下载进行中（后台，BTC 快照已落地）。
