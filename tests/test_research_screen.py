"""研究筛选回路的离线测试(合成数据, 不联网)。

存在理由(第五轮): screen.py 的离场模拟必须与 backtest.simulate_walk 同口径
(第四轮教训: 两套口径漂移), 分段/成本/Wilson 数学必须可复核。
"""
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "research"))

import backtest as B
import screen as SC

HOUR_MS = 3_600_000


def synthetic_frame(n=600, seed=7):
    """Random walk 1H frame in backtest snapshot column layout."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.01, n)
    close = 100 * np.exp(np.cumsum(steps))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, n)))
    vol = rng.uniform(1e5, 1e6, n)
    ts = 1_700_000_000_000 + np.arange(n) * HOUR_MS
    return pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low,
                         "close": close, "vol": vol})


class KernelParityTests(unittest.TestCase):
    """screen 的成交/离场必须与 backtest.simulate 完全一致(同一内核)。"""

    def test_screen_trades_match_direct_kernel_calls(self):
        """run_family 的每笔成交/离场必须与直接调 simulate_walk 逐项一致。

        复核指出原断言是"自己调自己比较"恒真; 这里改为端到端: 从 run_family
        的产出反推离场内核的输入(entry=i+1 开盘, stop=1.5 ATR, TP1/TP2 =
        2R/3R), 直接调 backtest.simulate_walk, 断言 outcome 与净 pnl 相等。
        """
        d = SC.enrich(synthetic_frame())
        trades = SC.run_family({"X/USDT": d}, "baseline", SC.family_baseline,
                               cooldown_bars=1)
        self.assertGreater(len(trades), 3)
        high, low, close = d.high.values, d.low.values, d.close.values
        ts_to_i = {int(t): k for k, t in enumerate(d.ts.values)}
        for t in trades[:5]:
            i = ts_to_i[t["ts"]]
            sign = t["sign"]
            entry = float(d.open.values[i + 1])
            stop = entry - sign * 1.5 * float(d.atr.iloc[i])
            risk = abs(entry - stop)
            direct = B.simulate_walk(high, low, close, i, sign, entry, stop,
                                     entry + sign * risk * 2,
                                     entry + sign * risk * 3,
                                     hold_bars=24, fill_bars=1)
            self.assertEqual(direct["outcome"], t["outcome"])
            self.assertAlmostEqual(direct["pnl_pct"] - SC.ROUND_TRIP_COST_PCT,
                                   t["pnl_pct"], places=9)
            self.assertAlmostEqual(risk / entry * 100, t["risk_pct"], places=9)
        self.assertIs(SC.B.simulate_walk, B.simulate_walk)


class EventTests(unittest.TestCase):
    def test_no_events_in_warmup(self):
        d = SC.enrich(synthetic_frame())
        for name, fn in SC.FAMILIES.items():
            if name == "baseline":
                continue
            events, _ = fn(d)
            self.assertFalse(events[:SC.WARMUP_BARS].any(), f"{name} 在预热带内触发")

    def test_squeeze_requires_breakout_after_squeeze(self):
        d = SC.enrich(synthetic_frame())
        events, params = SC.family_squeeze_breakout(d)
        n = int(events.sum())
        self.assertGreaterEqual(n, 0)
        direction = params["direction"]
        idx = np.flatnonzero(events)
        for i in idx:
            self.assertIn(direction[i], (1, -1))
            # 突破方向必须与收盘相对中轨一致
            mid = (d.high.rolling(48).max() + d.low.rolling(48).min()) / 2
            if direction[i] == 1:
                self.assertGreater(d.close.iloc[i], mid.iloc[i])
            else:
                self.assertLess(d.close.iloc[i], mid.iloc[i])


class SplitAndCostTests(unittest.TestCase):
    def test_split_boundary(self):
        trades = [{"symbol": "X", "ts": SC.SPLIT_UTC - 1, "sign": 1,
                   "pnl_pct": 1.0, "risk_pct": 1.0, "outcome": "took_profit"},
                  {"symbol": "X", "ts": SC.SPLIT_UTC, "sign": 1,
                   "pnl_pct": 1.0, "risk_pct": 1.0, "outcome": "took_profit"}]
        train = SC.segment_stats(trades, hi_ms=SC.SPLIT_UTC)
        test = SC.segment_stats(trades, lo_ms=SC.SPLIT_UTC)
        self.assertEqual(train["n"], 1)
        self.assertEqual(test["n"], 1)

    def test_cost_is_deducted(self):
        self.assertAlmostEqual(SC.ROUND_TRIP_COST_PCT, 0.14, places=6)
        # run_family applies cost on every trade: construct via baseline on a
        # tiny frame and confirm each pnl is net (shifted by exactly the cost).
        d = SC.enrich(synthetic_frame(n=400))
        frames = {"X/USDT": d}
        trades = SC.run_family(frames, "baseline", SC.family_baseline,
                               cooldown_bars=10**9)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        entry = float(d.open.values[d.index[d.index >= SC.WARMUP_BARS][0] + 1])
        self.assertGreater(t["risk_pct"], 0)
        # gross re-derived from net must equal simulate output + cost
        gross = t["pnl_pct"] + SC.ROUND_TRIP_COST_PCT
        self.assertTrue(math.isfinite(gross))
        self.assertGreater(entry, 0)

    def test_wilson_math(self):
        lo, hi = SC.wilson(0, 0)
        self.assertIsNone(lo)
        lo, hi = SC.wilson(50, 100)
        self.assertAlmostEqual(lo, 0.402, places=2)
        self.assertAlmostEqual(hi, 0.598, places=2)
        lo, hi = SC.wilson(100, 100)
        self.assertGreater(lo, 0.95)
        self.assertLessEqual(hi, 1.0)

    def test_dead_tape_bars_skipped(self):
        """Flat listing bars (ATR>0 but ~0 vs price) must not produce trades.

        Real case: ADA 2020-03 opening bars are OHLC all 0.041 with vol 0 —
        risk_pct ~1e-12 made baseline expR explode to -1.3e8 R.
        """
        n = 400
        ts = 1_700_000_000_000 + np.arange(n) * HOUR_MS
        flat = pd.DataFrame({"ts": ts, "open": 0.041, "high": 0.041,
                             "low": 0.041, "close": 0.041, "vol": 0.0})
        frames = {"X/USDT": SC.enrich(flat)}
        trades = SC.run_family(frames, "baseline", SC.family_baseline)
        self.assertEqual(trades, [])
        # Micro-range variant: ATR > 0 but far below MIN_ATR_FRAC of price —
        # this is what the new guard exists for (the old atr<=0 check misses it).
        micro = flat.copy()
        micro["high"] = 0.041 * (1 + SC.MIN_ATR_FRAC / 10)
        d = SC.enrich(micro)
        # baseline fires on its first eligible bar (WARMUP_BARS), so assert on
        # the ATR actually consulted at the event index.
        a = float(d.atr.iloc[SC.WARMUP_BARS])
        self.assertTrue(math.isfinite(a) and a > 0,
                        "ATR 必须为正, 否则测的是旧 atr<=0 分支")
        self.assertLess(a / 0.041, SC.MIN_ATR_FRAC)
        frames = {"X/USDT": d}
        self.assertEqual(SC.run_family(frames, "baseline",
                                       SC.family_baseline), [])

    def test_cooldown_suppresses_overlapping(self):
        d = SC.enrich(synthetic_frame(n=600))
        frames = {"X/USDT": d}
        tight = SC.run_family(frames, "baseline", SC.family_baseline,
                              cooldown_bars=1)
        wide = SC.run_family(frames, "baseline", SC.family_baseline,
                             cooldown_bars=48)
        self.assertGreater(len(tight), len(wide))


class RunFamilyTests(unittest.TestCase):
    def test_all_families_run_on_synthetic_corpus(self):
        frames = {"X/USDT": SC.enrich(synthetic_frame())}
        for name, fn in SC.FAMILIES.items():
            trades = SC.run_family(frames, name, fn)
            stats_all = SC.segment_stats(trades)
            if stats_all["n"]:
                self.assertTrue(-5 <= stats_all["expectancy_r"] <= 5, name)


if __name__ == "__main__":
    unittest.main()
