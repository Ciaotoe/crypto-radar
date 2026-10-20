import unittest

import strategy as s
from strategy_fixtures import market, clock
from test_strategy_market import NOW


class SignalTests(unittest.TestCase):
    def test_confirmed_long_and_short(self):
        for side in ("long", "short"):
            with self.subTest(side=side), clock():
                self.assertEqual(s.signal_direction(market(side)), side)

    def test_no_reversal_or_extreme_rsi_rejected(self):
        for side in ("long", "short"):
            for case in ("body", "break", "rsi"):
                data = market(side)
                bars = data["bars_1h"]
                if case == "body": bars.loc[len(bars)-1, "open"] = bars.close.iloc[-1]
                if case == "break": bars.loc[len(bars)-1, "close"] = bars.close.iloc[-2]
                if case == "rsi": data["1h_rsi"] = 12 if side == "long" else 88
                with self.subTest(side=side, case=case), clock(), self.assertRaises(s.StrategyRejected):
                    s.signal_direction(data)

    def test_proximity_without_retest_rejected(self):
        data = market()
        bars = data["bars_1h"]
        bars.loc[bars.index[-4:], "low"] = 100.5
        with clock(), self.assertRaisesRegex(s.StrategyRejected, "回踩/反弹"):
            s.signal_direction(data)

    def test_trend_disagreement_rejected(self):
        for key, value in [("4h_ema200", 103), ("4h_ema20_prev", 101), ("4h_di_plus", 10), ("4h_adx", 21)]:
            data = market(); data[key] = value
            with self.subTest(key=key), clock(), self.assertRaises(s.StrategyRejected): s.signal_direction(data)

    def test_stale_signal_and_price_rejected(self):
        for key, value in [("quoted_at_ms", NOW-180_000), ("signal_close_ms", NOW-46*60_000), ("price", 103), ("price", 99.8)]:
            data = market(); data[key] = value
            with self.subTest(key=key, value=value), clock(), self.assertRaises(s.StrategyRejected): s.signal_direction(data)

    def test_distance_scales_with_volatility_and_price(self):
        data = market()
        for key in ["price", "bid", "ask", "mark_price", "4h_ema20", "4h_ema50", "4h_ema200", "4h_ema20_prev", "4h_close", "1h_ema20", "1h_atr"]:
            data[key] *= 600
        bars = data["bars_1h"]
        bars.loc[:, ["open", "high", "low", "close", "ema20"]] *= 600
        with clock(): self.assertEqual(s.signal_direction(data), "long")


if __name__ == "__main__":
    unittest.main()
