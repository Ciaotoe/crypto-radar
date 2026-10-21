import unittest
import unittest.mock
from datetime import datetime, timedelta

import tracker as t
from tracker import BJ_TZ


def base_signal(**extra):
    """标准在持多单: 入场 100, 止损 97, TP1 103, TP2 106"""
    now = datetime.now(BJ_TZ)
    sig = {
        "id": "BTC/USDT-long-test", "symbol": "BTC/USDT", "direction": "long",
        "signal_type": "顺势做多", "entry_price": 100.0,
        "strategy": {"stop": 97.0, "tp1": 103.0, "tp2": 106.0,
                     "hold_hours": 12, "timeout_action": "平仓观望"},
        "triggered_at": (now - timedelta(hours=1)).isoformat(),
        "last_checked_at": (now - timedelta(minutes=20)).isoformat(),
        "status": "active", "exit_reason": None,
        "pnl_now": 0.0, "mfe": 0.0, "mae": 0.0, "tp1_hit": False,
        "last_price": 100.0, "alerts_fired": [],
        "last_ai_review": None, "seen_headlines": [], "last_ai_verdict": None,
    }
    sig.update(extra)
    return sig


def market_data(price=100.0, adx=30.0, ema20=100.0, ema50=98.0):
    return {"price": price, "4h_adx": adx, "4h_ema20": ema20, "4h_ema50": ema50,
            "funding_rate_pct": 0.01}


class TrackerTests(unittest.TestCase):
    def update(self, signals, data, window=None):
        """window(symbol, interval, start_ms) -> 15m K 线 DataFrame"""
        store = {"signals": signals}
        fetch = window if window is not None else (lambda *a: None)
        changed, events = t.update_open_signals(store, lambda s: data, fetch)
        return store["signals"][0], events

    def test_tp1_then_breakeven_exit_settles_half_tp1(self):
        # 先触 TP1(记档不结案)
        sig, events = self.update([base_signal()], market_data(price=103.5))
        self.assertTrue(sig["tp1_hit"])
        self.assertEqual(sig["status"], "active")
        self.assertEqual(len(events), 0)
        # 再回撤打保本: 按 半仓TP1(+3%) + 半仓保本(0%) 结案
        sig, events = self.update([base_signal(tp1_hit=True)], market_data(price=99.0))
        self.assertEqual(sig["status"], "exited")
        self.assertEqual(sig["exit_reason"], t.BREAKEVEN_AFTER_TP1)
        self.assertAlmostEqual(sig["pnl_now"], 0.5 * 3.0, places=3)
        self.assertEqual(events[0]["reason"], t.BREAKEVEN_AFTER_TP1)

    def test_full_stop_without_tp1_still_records_initial_stop(self):
        sig, events = self.update([base_signal()], market_data(price=96.0))
        self.assertEqual(sig["exit_reason"], t.STOPPED_OUT)
        self.assertAlmostEqual(sig["pnl_now"], -3.0, places=3)

    def test_tp2_exit_settles_blended(self):
        # 已 TP1 后触 TP2: 半仓TP1(+3%) + 半仓TP2(+6%) = +4.5%
        sig, events = self.update([base_signal(tp1_hit=True)], market_data(price=107.0))
        self.assertEqual(sig["exit_reason"], t.TOOK_PROFIT)
        self.assertAlmostEqual(sig["pnl_now"], 0.5 * 3.0 + 0.5 * 6.0, places=3)

    def test_stop_and_tp2_same_window_conserves_stop_first(self):
        # 同窗口双触保守按止损先触发
        sig, _ = self.update([base_signal()], market_data(price=101.0))
        self.assertEqual(sig["exit_reason"], None)  # 现价单点不触发
        import pandas as pd
        def window(sym, interval, start_ms):
            return pd.DataFrame({"high": [107.0], "low": [96.0]})
        sig, events = self.update([base_signal()], market_data(price=101.0), window)
        self.assertEqual(sig["exit_reason"], t.STOPPED_OUT)

    def test_intraperiod_ema50_pierce_no_longer_reverses(self):
        # 盘中价跌破 EMA50 但已收盘排列仍多头 -> 不结案(2026-10-21 移植口径)
        sig, events = self.update([base_signal()], market_data(price=97.5, ema50=99.0))
        self.assertEqual(sig["status"], "active")
        self.assertEqual(events, [])
        # 已收盘 EMA20/50 排列真正翻转 -> 结案且紧急
        sig, events = self.update([base_signal()], market_data(price=100.0,
                                                               ema20=97.0, ema50=99.0))
        self.assertEqual(sig["exit_reason"], t.TREND_REVERSED)
        self.assertTrue(events[0]["urgent"])

    def test_scan_window_excludes_pre_entry_pins(self):
        import pandas as pd
        sig = base_signal(triggered_at=(datetime.now(BJ_TZ) - timedelta(minutes=10)).isoformat(),
                          last_checked_at=(datetime.now(BJ_TZ) - timedelta(hours=2)).isoformat())
        seen = {}
        def window(sym, interval, start_ms):
            seen["start"] = start_ms
            return pd.DataFrame({"high": [100.5], "low": [99.5]})
        self.update([sig], market_data(price=100.0), window)
        # 窗口起点 = max(last_checked, triggered): 开仓前的插针不进扫描
        trig_ms = int(datetime.fromisoformat(sig["triggered_at"]).timestamp() * 1000)
        self.assertGreaterEqual(seen["start"], trig_ms)
        self.assertAlmostEqual(sig["mae"], -0.5, places=3)  # 只含开仓后窗口

    def test_old_archive_fields_migrate(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "signals.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"signals": [{
                    "id": "old-1", "symbol": "ETH/USDT", "direction": "short",
                    "entry_price": 100.0, "status": "closed",
                    "triggered_at": "2026-10-01T00:00:00+08:00",
                    "last_checked_at": "2026-10-01T00:00:00+08:00",
                    "pnl_now": -1.0, "mfe": 0.5, "mae": -1.0, "last_price": 99.0,
                }]}, f, ensure_ascii=False)
            with unittest.mock.patch.object(t, "DATA_PATH", path):
                loaded = t.load_signals()
        sig = loaded["signals"][0]
        self.assertEqual(sig["status"], "exited")          # invalid/closed -> exited
        self.assertEqual(sig["exit_reason"], t.TREND_REVERSED)
        self.assertEqual(sig["seen_headlines"], [])        # 新字段补齐
        self.assertIsNone(sig["last_ai_verdict"])
        self.assertFalse(sig["tp1_hit"])


if __name__ == "__main__":
    unittest.main()
