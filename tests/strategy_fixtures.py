import os
import tempfile
from unittest.mock import patch

import strategy as s
from test_strategy_market import MarketTests, NOW

s.KLINE_CACHE_PATH = os.path.join(
    tempfile.mkdtemp(prefix="crypto-radar-test-"), "klines_4h.json")


def market(direction="long"):
    data = MarketTests().market()
    data.update(price=101., bid=100.99, ask=101.01, mark_price=101.,
                **{"4h_ema20": 100., "4h_ema50": 98., "4h_ema200": 95.,
                   "4h_ema20_prev": 99., "4h_close": 101., "4h_adx": 30.,
                   "4h_di_plus": 30., "4h_di_minus": 15., "1h_rsi": 52.,
                   "1h_atr": 1., "1h_ema20": 100.})
    bars = data["bars_1h"]
    bars.loc[:, ["open", "high", "low", "close", "ema20", "rsi"]] = [99.8, 100.4, 99.5, 100., 100., 46.]
    bars.loc[len(bars)-2, ["open", "high", "low", "close", "ema20", "rsi"]] = [100.2, 100.3, 99.5, 99.9, 100., 46.]
    bars.loc[len(bars)-1, ["open", "high", "low", "close", "ema20", "rsi"]] = [99.9, 101.2, 99.7, 101., 100., 52.]
    # Historical resistance, confirmed before the entry setup.
    # 注意: 本 fixture 只覆盖"结构远离入场"(9-11 ATR)这一档 —— 该档下 TP2
    # 保持净 3R, 是点位数学单测需要的稳定基线。它**不代表真实枢轴密度**
    # (实测显著结构中位仅 1.6 ATR 外), 因此不能用它判断门槛是否可触发。
    # "门槛在真实数据上非空"由 tests/test_backtest.py 用录制快照保证。
    bars.loc[170, ["high", "open", "close"]] = [112., 100., 100.]
    bars.loc[200, ["high", "open", "close"]] = [110., 100., 100.]
    data["bars_4h"].loc[:, ["open", "high", "low", "close"]] = [100., 100.4, 99.5, 100.]
    data["bars_4h"].loc[650, "high"] = 112.
    data["bars_4h"].loc[700, "high"] = 110.
    bars15 = data["bars_15m"]
    bars15.loc[:, ["open", "high", "low", "close", "ema20"]] = [100., 100.4, 99.5, 100., 100.]
    bars15.loc[len(bars15)-1, ["open", "high", "low", "close", "ema20"]] = [99.9, 101.2, 99.7, 101., 100.]
    data["15m_ema20"] = 100.
    data["15m_open"] = 99.9
    data["15m_close"] = 101.
    if direction == "short":
        for key in ["price", "bid", "ask", "mark_price", "4h_ema20", "4h_ema50", "4h_ema200",
                    "4h_ema20_prev", "4h_close", "1h_ema20", "15m_ema20", "15m_open", "15m_close"]:
            data[key] = 200 - data[key]
        data["bid"], data["ask"] = data["ask"], data["bid"]
        data["4h_di_plus"], data["4h_di_minus"] = data["4h_di_minus"], data["4h_di_plus"]
        data["1h_rsi"] = 48.
        for frame in [data["bars_1h"], data["bars_4h"], data["bars_15m"]]:
            oldhigh = frame.high.copy()
            frame["high"], frame["low"] = 200-frame.low, 200-oldhigh
            frame["open"], frame["close"] = 200-frame.open, 200-frame.close
        bars["ema20"], bars["rsi"] = 200-bars.ema20, 100-bars.rsi
        data["bars_15m"]["ema20"] = 200 - data["bars_15m"]["ema20"]
    return data


def clock():
    return patch.object(s.time, "time", return_value=NOW / 1000)
