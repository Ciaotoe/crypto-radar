# PLAN: 策略判定层修复 + 可测量性回路（第四轮，2026-10-24）

分支 `feature/signal-edge-v3`。以下历史轮次记录保留不动。

## 背景（2026-10-24 实测复盘）

用 OKX 真实历史数据把生产判定栈重放了 6 个币种 × 170 天（20,268 根已收盘 1H bar），
结论与生产档案完全一致：

- **生产配置产出 0 个信号**。funnel 逐级淘汰：ADX 6551 / 4H 排列 4095 / RSI 2906 /
  1H 反转 1085 / 偏离 226 / 均线触及 116，**最后 109 个通过全部前序门槛的 setup 100%
  死在 `P3_boundary_room`**。
- 生产档案同样为空：`data/signals.json` 中 `stats_epoch=2026-10-22`，带真实
  `strategy_version` 的信号 **0 条**。即新策略上线以来从未推送过一条。

### 根因：`structure_boundary` 的定义错误，不是阈值过严

它把"3 根 K 线局部极值"当作确认支撑/阻力。实测这些枢轴的性质：

```
最近枢轴距入场:     median +0.19 ATR    （突出度 median 0.19 ATR）
TP2 所需距离:       median +6.01 ATR
门槛通过率(boundary > tp2 + 0.25 ATR):  0/153 = 0.0%
boundary 位于 tp2 之后:                 100% 的 setup
```

0.19 ATR 的"枢轴"是 1H 噪声，不是结构。这道门要求的几何关系在数学上不可能成立，
等于把策略整体关闭。**"固定 2R/3R 目标"与"前方结构必须留出空间"两条约束互相矛盾**：
24h 窗口内必然存在微观枢轴，而 3R 目标必然在它们之后。

### 测试为什么全绿却掩盖了这件事

`tests/strategy_fixtures.py` 把阻力人工设在 110/112（价格 101、ATR=1，即 9–11 ATR 外）。
真实数据里前方 0.19 ATR 就有枢轴。**fixture 用手工植入的"开阔空间"让 boundary 门永远通过**，
因此 74 个测试全过而生产零信号。这是本轮必须一并修掉的结构性问题。

### 更深的结论：松绑后仍无正期望（本轮不做，但要记录）

把 boundary 门拿掉后逐级累加其余门槛，每笔期望 R：

```
S0 无任何筛选     n=1865  winR 0.316  expR -0.203R  PF 0.672
S1 +ADX≥22        n=1307  winR 0.311  expR -0.206R  PF 0.665   边际 -0.003R
S3 +1H反转        n=519   winR 0.314  expR -0.171R  PF 0.721   边际 +0.036R
S4 +RSI带         n=443   winR 0.318  expR -0.157R  PF 0.747   边际 +0.014R
S5 +偏离限制      n=155   winR 0.381  expR -0.028R  PF 0.951   边际 +0.129R  ← 最大
S6 +止损上限      n=153   winR 0.386  expR -0.018R  PF 0.968   边际 +0.010R
```

各门槛**方向正确**（PF 0.67→0.97，亏损收窄 99%），但累加到最紧仍是 -0.018R/笔 ≈ 打平偏负。
即：现有筛选器把"明显亏钱的机会"过滤掉了，但没有筛出"有正优势的机会"。
**本轮不宣称找到边际**，只把系统修到能产出信号、且阈值可用真实数据衡量。

### 已验证的修复方向（预实验）

对"显著结构 + 目标上限"语义做 16 组参数扫描（6 币种 × 170 天）：
3 组出现正期望，最佳档 min_prom=1.0 / cap=0.85 / hold=24 → n=13、winR 0.46、
expR +0.117R、PF 1.22。样本极小（n=13），仅证明该语义**能让系统重新产出非零信号且
方向不立即变坏**，不构成边际结论。

## 目标

1. 修复 `structure_boundary`：只认显著结构（突出度过滤），并从"否决门"改为"目标上限"。
2. 把重放回测能力做进仓库，使任何阈值改动都能用真实数据衡量，而不是只靠植入式 fixture。
3. 修掉掩盖此类问题的测试盲区（fixture 开阔空间假设 + 缺少"漏斗非空"断言）。
4. 阈值可配置，便于扫描；不在本轮调参定值。

## 步骤与验收标准

1. [ ] **修复 boundary 语义**（`strategy.py`）
   - `structure_boundary` 增加突出度过滤：枢轴相对其邻居的突出幅度 <
     `min_prominence_atr`（默认 1.0，可配）则视为噪声丢弃。移除"24h 极值"兜底项
     （它只会引入 0.19 ATR 噪声）。
   - 前方无显著结构 = 开阔空间，**放行**（当前是拒绝）；有显著结构则作为目标上限：
     要求 `tp2 ≤ boundary × cap_frac`（默认 0.85），不满足即拒。
   - `validate_plan` 中 `values["structure_boundary"] != actual_boundary` 的浮点精确相等
     校验改为一致性校验（本就脆弱）。
   - 点位契约变更：`structure_boundary` 语义由"最近枢轴"变为"最近显著枢轴，可为 null"。
     同步 README / AGENTS.md。旧档案无需迁移（该字段仅存档展示，不参与结算）。
   - 验收：`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy_levels.py' -v`
     全过；新增用例覆盖"噪声枢轴不构成边界"、"无显著结构放行"、"tp2 超出 cap 被拒"。
2. [ ] **重放回测工具进仓库**（新增 `backtest.py`，只读，不碰 `data/`）
   - 尽量复用生产 `strategy.py` 的判定逻辑（避免两套口径漂移）；从 OKX
     `history-candles` 拉 1H 历史，按 4H 重采样算指标，逐 bar 重放判定栈。
   - 输出 funnel 逐级淘汰计数 + 前向模拟的 n / winR / expR / PF / MFE / MAE。
   - 已知近似必须在输出里显式标注：1H bar 判成交与离场（非逐笔）、历史资金费率
     不可得（成本按 0 计）、15m 触发门无法历史重放（OKX `history-candles` 15m 深度
     仅约 99 根）。
   - 验收：`uv run --no-sync python backtest.py --symbols BTC/USDT --days 120 --json`
     返回 funnel 计数且 `PASSED > 0`；不发 TG、不写 `data/`。
3. [ ] **修掉测试盲区**（`tests/`）
   - fixture 不再把阻力植入 9–11 ATR 外；按真实枢轴密度构造数据，使 boundary 门
     在测试中经历真实的"前方 0.2 ATR 就有枢轴"场景。
   - 新增"漏斗非空"断言：对录制数据跑判定栈，断言 `PASSED > 0`，防止再出现
     "测试全绿但生产零信号"。
   - 验收：全量测试通过；人为把 `min_prominence_atr` 调成 0（复现旧 bug）时
     新断言必须失败。
4. [ ] **阈值可配置**（`strategy.py`）
   - ADX 下限、RSI 带、偏离倍数、止损 ATR 上限/百分比上限、净 RR 下限、hold 小时、
     cap_frac、min_prominence_atr 统一走环境变量（保留现值为默认）。
   - 目的仅是让步骤 2 的工具能扫描，不在本轮改默认值。
   - 验收：`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy*.py' -v`
     全过；默认值下行为与步骤 1 完成时一致。
5. [ ] **全量验证 + 独立只读复核 + 文档同步**
   - 验收：全量测试通过；`backtest.py` 在 6 币种 × 170 天上给出 funnel 与统计；
     `git diff --check` 无输出；plan-reviewer 只读复核无缺口；README/AGENTS.md
     反映新语义与"当前无已验证边际"的明确表述。

## 已知约束

- 点位契约变更（`structure_boundary` 语义）已列明，待用户确认后执行。
- 追踪层（`tracker.py` 状态机口径）、`report.py`、`universe.py`、`runtime.py`、
  `deploy/` 本轮不动 —— 复核结论是这些资产扎实，不是问题来源。
- 单一 OKX 数据源、失败不回退；点位由代码生成、AI 只可否决 —— 原则不变。
- 不新增第三方依赖；回测工具用现有 requests/pandas。
- **不宣称策略已盈利**。步骤 2 的工具是衡量手段，任何 expR 数字受样本量（最佳档
  n=13）、1H 近似、无资金费率、并发相关性限制。
- 测试沿用既有教训：冻结 `strategy.time.time`，patch `crypto_radar` 模块属性。
- 本轮改动不涉及 `.env.example` 之外的环境变量注入路径；新增变量
  `RADAR_MIN_PROMINENCE_ATR` / `RADAR_BOUNDARY_CAP_FRAC` 需确认（全局规则：敏感文件
  不读写，`.env.example` 属需确认范围——步骤 4 落地时先向用户确认）。

## 进度记录

- [ ] 步骤 1：boundary 语义修复
- [ ] 步骤 2：回测工具进仓库
- [ ] 步骤 3：测试盲区
- [ ] 步骤 4：阈值可配置
- [ ] 步骤 5：验证与文档

---


# PLAN: 信号追踪改造 —— 从"趋势机械追踪"到"盯策略离场 + 事件驱动推送"

## 背景(2026-10-19 用户反馈)
- 现追踪口径不感知策略点位(TP/止损): 旧信号早已止盈离场, 摘要仍报"趋势成立";
- 信号 ID 含日期, 日期翻篇即当作新信号, 次日重复推送完整策略;
- 每小时摘要罗列峰值/最差等数字, 信息量低且刷屏。
- 核心认知: 巡检有 20 分钟粒度延迟, 价格触及预设点位(止损/止盈)时
  交易所已自动执行, 无需即时提醒 —— 这些只是**衡量策略好坏的口径**;
  真正值得推送的是策略没预设、需要人重新决策的"不对劲"。

## 目标
1. 策略点位(止损/TP1/TP2)随信号存档进 signals.json。
2. 追踪 = 盯策略离场: 触及止损/TP2、趋势排列翻转、48h 超时 → 结案退出追踪。
3. 推送改事件驱动, 平时完全静默; 新策略推送保留。

## 步骤
1. **Gemini 结构化输出** `crypto_radar.py`
   - 要求 Gemini 返回严格 JSON(方向/入场/止损/TP1/TP2/时间预期/核心逻辑),
     推送文案由代码格式化(观感与现在一致)。
   - 解析或点位校验失败 → 退化为旧纯文本模式, 信号无点位档案走机械追踪。
   - 验收: mock 两种 Gemini 响应, 断言解析成功/失败两条路径文案正确。
2. **tracker.py 状态机改造**
   - status: active → exited(reason ∈ stopped_out / took_profit /
     trend_reversed / timeout); 旧 invalid/closed 加载时迁移为 exited。
   - 离场判定用 15m K 线扫描上次检查以来的区间高低(插针不漏);
     同一根 bar 内止损与止盈双触, 保守按止损先触发。
   - TP1 命中仅记档(tp1_hit), 不推送不结案; MFE/MAE 用扫描窗口高低点累积。
   - 去重修复: 同币种同方向存在未结案信号即不推新策略,
     结案后再次触发才算新一轮(新 ID 含触发时刻)。
   - 验收(临时断言脚本, 跑过即删): 触止损结案 / 触TP2结案 / TP1不结案 /
     趋势翻转且止损未触 = ⚠️紧急结案 / 回吐与ADX预警各只推一次 / 旧数据迁移。
3. **推送改造** `crypto_radar.py`
   - 删除每小时摘要(last_summary_hour 字段随之清理);
     update 返回事件列表, 有事件时合并推一条 TG。
   - 验收: 本地跑通(网络失败可 mock), signals.json 新字段完整。
4. **端到端**
   - 手动触发 Actions, 确认 TG 收到事件推送(或正常静默), data commit 正常。

## 预警与结案口径(默认值, 可调)
- ⚠️ 紧急(结案同时推): 4H 排列翻转但止损尚未被触及 —— 唯一需要人立刻
  决策的事件("别等止损, 尽快离场")。
- ⚠️ 预警(信号保持追踪, 同信号同原因只推一次):
  - 浮盈大幅回吐: mfe ≥ 1.0% 且现浮盈 ≤ 峰值的 1/3;
  - 4H ADX < 22(趋势动能消散)。
- 📋 结案通知(简短一条, 之后退出追踪): 止损触及 / TP2 触及 / 排列翻转 /
  48h 超时; 附最终浮盈与峰值供长期衡量。
- 无点位存档的旧信号: 仅排列翻转/超时可结案, 结案时标注"机械口径"。

## 已知约束
- 所有"触及"都是 15m K 线区间扫描(非实时), 结案的浮盈按触及价位计算,
  与实际分批止盈的成交均价会有偏差 —— 口径是衡量, 不是记账。
- 排列翻转与止损同窗触发时: 若止损位已被扫过按止损结案(事更早),
  否则按紧急翻转处理。

## 进度记录(改造轮)
- [x] 步骤1+2+3 (2026-10-19): 8 组断言全过 + 本地 dry-run 真实行情跑通
  (旧信号触发 2 结案 2 预警, 去重生效, 文案/档案字段正确)
- [ ] 步骤4 端到端: 手动触发 Actions 验证

## 关键记录(改造轮)
- 初跑暴露: 旧档案信号无 strategy 字段直接 KeyError, load_signals 迁移时
  统一 setdefault 补齐(strategy/exit_reason/tp1_hit/alerts_fired)
- dry-run 删掉一次 15m 扫描的 span 截断防御: startTime 由档案时间戳推导,
  无真实触发场景

## 第二轮: 一次只推一个策略 + 巡检节奏修复(2026-10-19)
- [x] 多候选择优(4H ADX, 并列取贴近 EMA20) + 有在持不推新策略
- [x] 旧单全清(manual_reset), 重新开始找机会
- [x] Actions cron 丢拍确认: scheduled 实测中位间隔 144 分钟(免费仓库
      高频 cron 被调度器延迟/丢弃), 改 job 内 3 轮循环 + sleep 1200,
      每小时 :03 拉起接力
- 遗留: Gemini 返回未过点位校验时 strategy 为 null(退化机械追踪),
  已加诊断日志待下条新策略触发时定位原因

## 策略完善计划（2026-10-20，草案，待用户确认）

### 目标
- 完善新策略的行情依据、顺势回踩确认、交易点位、成本和风险约束；不合格机会不推送。
- 明确实际交易市场及代码/AI 的点位决策职责，使建议可复核；不把规则通过表述为盈利保证。

### 步骤与验收标准
1. [x] 明确策略口径并修正行情输入。
   - 验收：`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy_market.py' -v`；全部通过，覆盖未收盘 K 线不改变趋势指标、现价独立更新、行情缺失/过期拒绝发策略、费率缺失不当作零费率、EMA200 预热充足，以及所选交易市场的数据一致性。
2. [x] 完善趋势方向、回踩/反弹确认与追价限制。
   - 验收：`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy_signal.py' -v`；全部通过，覆盖多空确认、下跌未止/上涨未止拒绝、未触及支撑/阻力拒绝、偏离按 ATR 归一化、超时或价格跑远拒绝。
3. [x] 完善点位、交易成本、结构空间和仓位风险约束。
   - 验收：`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy_levels.py' -v`；全部通过，覆盖正数/有限数/方向校验、数字入场与建议一致、净 TP1 盈亏比至少 2:1、止损在结构外且不过宽/过窄、目标不越过已知反向结构、交易成本和账户风险上限计算正确、不合格 AI 输出拒绝推送。
4. [x] 接入候选择优、推送前复核并更新策略说明。
   - 验收：`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy_flow.py' -v`；全部通过，覆盖不同币价候选使用可比较指标、首选失败不阻断其余合格候选、推送前失效拒绝、无有效策略不推送/不建新档案；`git diff --check` 无输出。
5. [x] 完整验证与独立只读复核，记录实际限制并归档本轮计划。
   - 验收：`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy*.py' -v` 全部通过；公开行情只读检查能给出策略或明确拒绝原因，不调用 Telegram/实际订单接口、不改信号档案；按本轮计划逐项只读复核无遗漏。保存验证记录与必要项目规则后，每个完成步骤单独提交，不推送。

### 已知约束
- 用户已确认计划、Binance USDT 永续及代码生成点位；AI 复核允许否决，不能强行解释成立。
- 本轮不修改追踪状态机、旧信号、巡检节奏、AI 持仓复核或 GitHub Actions 部署。
- 优先使用现有依赖和 Python 标准库，不增加依赖、不改变环境变量/包管理器/部署布局。
- 若最终选择涉及数据/API/档案契约变化，先列明迁移与兼容范围并确认；敏感文件包括 `.env.example` 按全局规则不读写，如与文档同步要求冲突须先解决。
- 不把固定阈值或净盈亏比当作经过回测的优势；本轮验证规则与边界正确性，不宣称收益已验证。
- 保留上方已有追踪计划；本轮完成时归档仅处理新增策略计划，不删除或改名已有文件。
- 步骤1：新策略使用独立永续数据入口，保留旧追踪接口与档案结构。Binance 永续 HTTP 451，公开行情验证以明确拒绝原因为验收，不使用跨市场替代。API 官方连接器说明可读，官方网页抓取失败。
- 步骤2：EMA200、DI 与斜率用于方向一致性；1H 收盘反转及均线触及才构成候选，45 分钟有效、ATR 限制追价。所有阈值仅为待验证规则，不表述“确定性机会”。

## 完成记录(策略完善轮, 2026-10-20 归档)

- 步骤1-2 前期完成(提交 d043b39/2c1e4f7); 步骤3 提交 8d2c2e6(9 测试), 步骤4 提交
  8b9186d(8 测试); 步骤5 收尾: 全量 31/31 通过, `git diff --check` 无输出。
- 公开行情只读检查(本机 451): BTC/ETH/SOL 均输出明确拒绝 `HTTP 451`, 未调
  TG/订单接口、未改档案 —— 按计划约束以此口径验收, 不用跨市场替代。
- 独立只读复核(plan-reviewer): 判定 PASS —— 步骤1-4 验收逐项达成, 约束零违反
  (追踪层/部署/依赖/环境变量/敏感文件均无 diff), 无 stub; data/signals.json
  的字段变化为巡检自然产物。
- 关键决策: AI 职责收敛为"只可否决"(用户已确认点位由代码生成); 推送文案与
  档案点位同源于一份规则计划, 不再存在模型文本与存档两个口径; 候选择优按
  ATR 归一贴近度(绝对差跨币价不可比, 旧口径会让高价币永远输)。
- 教训: 测 `run_scan` 需冻结 `strategy.time.time`(真实时钟使 fixture 行情全部
  "报价过期"), 并 patch `crypto_radar` 模块级密钥属性; 已沉淀到项目 AGENTS.md。
- 归档: 本轮计划完成, 保留上方追踪计划不动; 项目 AGENTS.md 已建, 记录架构
  边界、451 约束与测试要点。不推送, 由用户决定合并。

## 第三轮: 迁移 OKX + 移植追踪改进(2026-10-21, 已完成)

### 背景
- 用户确认: 整个系统(行情源与实际交易市场)迁移到 OKX USDT 永续;
  并采纳 external `new/` 版中 4 项追踪层真改进, 但**不采纳**其
  crypto_radar 架构(回退 AI 生成点位/放弃成本校验等, 已评审为倒退)。
- 用户确认(2026-10-21): 单一 OKX 数据源, 不做失败回退。
- 本机实测 OKX v5 公开接口全部可用: candles/ticker/mark-price/
  funding-rate/funding-rate-history/instruments, 无 451 限制。
- 关键差异: OKX 合约为 ctVal 张数模式(BTC 1张=0.01 BTC), 数量单位
  与 Binance 币本位不同; 费率每 8h 结算(ticker 实测 28800000ms 间隔)。

### 目标
- strategy.py 数据层整体换为 OKX v5, 单一行情源无回退(回退会重新引入
  跨市场基差错位); 策略规则/点位生成/成本校验逻辑不变, 仅适配数据。
- tracker.py 移植 4 项改进: TP1 后保本闭环(breakeven_after_tp1)、
  _trend_ok 仅看已收盘 EMA20/50 排列、扫描窗口防开仓前插针、
  AI 复核增量头条去重。
- TG 推送全量 html.escape 转义(当前分支缺口)。
- 契约同步: README/AGENTS.md/signals.json 字段兼容(旧信号无新增
  字段走 setdefault 迁移)。

### 步骤与验收标准
1. [x] strategy.py 数据层迁移 OKX( candles 已收盘过滤/连续性校验/预热量,
     ticker 报价新鲜度, funding 8h 周期, instruments 规格: tickSz/lotSz/
     minSz/ctVal; 数量单位换算为张)。无 Binance 回退。
   - 验收: `uv run --no-sync python -m unittest discover -s tests -p
     'test_strategy_market.py' -v` 全过; mock 全 OKX 端点, 覆盖
     未收盘 K 线不进指标、行情过期拒绝、费率缺失拒绝、张数/步长校验、
     HTTP 失败无跨市场回退。
2. [x] 适配点位/成本/仓位到 OKX 合约语义( ctVal 换算、张数取整、
     最小张数 minSz、费率方向与结算次数按 8h)。
   - 验收: test_strategy_levels.py 全过(必要处适配张数断言); 净盈亏比
     计算含 ctVal 与费率周期。
3. [x] tracker.py 移植 4 项改进 + 契约字段迁移( breakeven_after_tp1/
     seen_headlines/last_ai_verdict setdefault; exit_reason 新枚举值)。
   - 验收: 新增 tests/test_tracker.py: 触 TP1 后回撤保本结案记 0.5*TP1、
     未 TP1 回撤仍记全额止损、开仓前插针不误判、旧档案字段迁移;
     update_open_signals 现有行为不回归。
4. [x] crypto_radar.py 追踪层数据源换 OKX( get_market_data/
     fetch_klines_since/ fetch_funding_rate 失败返回 None 并由预警层
     豁免) + send_event_notice/html 全量转义 + AI 复核增量去重。
   - 验收: tests/test_strategy_flow.py 全过(mock OKX 行情入口); 手工
     构造含 `<script>` 的 detail 断言转义。
5. [x] 全量验证 + 独立只读复核 + 文档同步( README 数据源/点位/成本
     章节改 OKX 口径; AGENTS.md 更新架构边界; PLAN 归档)。
   - 验收: `uv run --no-sync python -m unittest discover -s tests -p
     'test_strategy*.py' -v` 全过; 公开行情只读检查三币种给出策略或
     明确拒绝原因(OKX 直连, 预期可真实拉到行情); plan-reviewer 复核
     无缺口; `git diff --check` 无输出。

### 已知约束
- 环境: TG_BOT_TOKEN/TG_CHAT_ID/GEMINI_API_KEY 保持, 不新增环境变量;
  不新增依赖; GitHub Actions 部署不改(仍每小时拉起, job 内循环不动)。
- 兼容: data/signals.json 旧信号照常加载; 新增字段走 setdefault;
  不修改已结案信号的历史记录。
- `new/` 目录的 external 文件仅作参考, 不入库不入 git(加 .gitignore)。
- 阈值与点位规则同前: 净 2R/3R、结构外止损、代码生成点位、AI 仅否决;
  OKX 结算时间表(8h)按 fundingTime 实测推导, 不硬编码假设。
- 教训沿用: 测试冻结 strategy.time.time; crypto_radar 模块级密钥 patch。

### 完成记录(第三轮, 2026-10-21)
- 提交: e9ecbb9(步骤1 数据层) / fac3bf5(步骤2 张数) / 1e69d43(步骤3 追踪
  改进) / 84adadd(步骤4 追踪层OKX+转义+去重) / 997c29f(费率cap修复+文档) /
  2ed55b6(new/ 忽略)。测试 45/45 通过(新增 market 1 项、levels 1 项、
  tracker 7 项、flow 2 项), `git diff --check` 无输出。
- 公开行情只读检查(OKX 直连): BTC "4H ADX 低于趋势筛选阈值"、ETH/SOL
  "4H 均线、斜率或方向指标不一致" —— 均给出明确拒绝原因, 未调 TG/档案。
- 独立只读复核(plan-reviewer): PASS —— 分页拼接用真实 OKX 数据独立验证
  (799 根 4H 无重复无缺口), 张数换算/保本结算数学/转义覆盖/约束逐项确认。
- 端到端真 bug 修复: OKX 费率极低时跳过结算(BTC 实测 next-prev=16h),
  原校验把 16h 当常规周期会低估成本; 改为 min(观测, 8h) 保守预算,
  观测 >16h(连跳两次)才拒绝。
- 遗留标注(复核员, 不影响正确性): 追踪层 4H EMA200 用 250 根预热有偏差,
  但该值仅存档展示不参与判定; seen_headlines [-20:] 基于 set 序非严格
  "最近20条", verdict 去重兜底。
- 不推送, 由用户决定合并; Actions 部署未动(下一小时巡检将自动切换
  OKX 数据源)。
