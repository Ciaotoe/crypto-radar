# 项目规则（trade）

继承全局 AGENTS.md；Python 项目，一律 `uv run --no-sync`。密钥经环境变量注入（TG_BOT_TOKEN / TG_CHAT_ID / GEMINI_API_KEY），`.env` 已被 .gitignore 覆盖，不提交。

## 架构边界（2026-10-20 策略完善轮确立）

- `strategy.py` 是新策略的独立数据入口与规则层：行情拉取、信号确认、点位/成本/仓位全部由代码生成，**AI（Gemini）只可否决，不生成点位、不解释成立**；复核调用失败放行，推送不得依赖单一外部模型。
- `crypto_radar.py` 中 `get_market_data` / `fetch_klines_since` / `review_signals_with_ai` / 巡检循环属于追踪层，与新策略解耦，改新策略时不得触碰追踪状态机口径。
- 点位契约（signals.json 档案的 strategy 字段：stop/tp1/tp2/hold_hours/timeout_action/entry_min/entry_max/text）如需变更，先列迁移与兼容范围并征得确认。

## 已知环境约束

- 本机出口访问 `https://fapi.binance.com` 返回 HTTP 451：新策略在线验证受阻是已知常态，公开行情只读检查以**明确拒绝原因**为验收，不引入 OKX/镜像等跨市场替代（`crypto_radar.py` 的 OKX 镜像仅属追踪层旧口径）。
- Actions 免费仓库高频 cron 会丢拍，job 内 3 轮循环 + sleep 1200 是有意为之，勿"修复"。

## 验证

- 策略测试：`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy*.py' -v`
- 测试需要冻结 `strategy.time.time`（行情新鲜度校验依赖真实时钟，fixture 行情是固定时间戳）；`crypto_radar` 模块级读取密钥环境变量，测试 patch 模块属性而非 `os.environ`。
