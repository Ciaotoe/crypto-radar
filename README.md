# Crypto radar

监控 BTC、ETH、SOL 的 Binance USDT 永续顺势回踩机会。只生成提醒，不连接账户或下单。新策略使用 Binance 永续公开接口，现有追踪保持原有口径。

## 策略数据

4H/1H 指标仅使用已收盘、连续完整的 K 线；4H 至少 750 根用于 EMA200 预热。永续最新价、买卖报价、标记价、资金费率和结算周期独立获取。数据缺失、过期或接口受地域限制时拒绝新策略，不降级到现货行情或其他交易所费率。

当前机器访问 `https://fapi.binance.com` 返回 HTTP 451。真实永续策略的在线验证受此限制；本地模拟测试不代表真实信号或盈利表现。

## 验证

`uv run --no-sync python -m unittest discover -s tests -p 'test_strategy*.py' -v`

不需要新增依赖或环境变量；密钥仍按现有运行方式注入，不应提交到仓库。
