import copy
import os
import tempfile
import unittest
from unittest.mock import patch

import strategy as s

NOW = 1800000000000 // s.HOUR_MS * s.HOUR_MS + 25 * 60_000
# OKX K 线: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
# 最新一根 confirm="0" 未收盘, 永不进指标


def rows(interval, spike=110):
    duration, count = {"4h": (4 * s.HOUR_MS, 800),
                       "1h": (s.HOUR_MS, 300),
                       "15m": (s.HOUR_MS // 4, 300)}[interval]
    end = NOW // duration * duration  # 当前未收盘 bar 的开始
    result = []
    for i in range(count):
        # 已收盘 bar: 最旧在 i=0, 最新已收盘在 end-duration(收盘时刻 <= NOW)
        ts = end - (count - i) * duration
        price = 100 + i * .01
        result.append([ts, price, price + 1, price - 1, price, 10, 1, 100, "1"])
    # 最新一根是未收盘 live bar: 它的 OHLC 再离谱也不进指标(spike 挂这里)
    result.append([end, spike, spike + 1, spike - 1, spike, 10, 1, 100, "0"])
    return result


def okx_rows(interval):
    """OKX candles 倒序返回(最新在前), 300 根一页"""
    all_rows = rows(interval)
    pages = []
    for i in range(0, len(all_rows), 300):
        pages.append(all_rows[i:i + 300][::-1])
    return pages


def okx_pages(full, params):
    """把升序 K 线行按 OKX 语义返回: after=该时间戳之前的更旧数据, 倒序, limit 根"""
    after = params.get("after")
    pool = [r for r in full if not after or float(r[0]) < float(after)]
    return pool[::-1][:params["limit"]]


def api(path, params=None):
    if path.endswith("candles"):
        return okx_pages(rows(params["bar"].lower()), params)
    if path.endswith("tickers"):
        return [{"instId": "BTC-USDT-SWAP", "ts": str(NOW),
                 "last": "108", "bidPx": "107.99", "askPx": "108.01"}]
    if path.endswith("ticker"):
        return [{"instId": "BTC-USDT-SWAP", "ts": str(NOW),
                 "last": "108", "bidPx": "107.99", "askPx": "108.01"}]
    if path.endswith("mark-price"):
        return [{"instId": "BTC-USDT-SWAP", "instType": "SWAP",
                 "markPx": "108", "ts": str(NOW)}]
    if path.endswith("funding-rate"):
        return [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0001",
                 "nextFundingTime": str(NOW + s.HOUR_MS),
                 "prevFundingTime": str(NOW - 7 * s.HOUR_MS)}]
    return [{"instId": "BTC-USDT-SWAP", "instType": "SWAP", "state": "live",
             "ctType": "linear", "settleCcy": "USDT", "ctVal": "0.01",
             "tickSz": "0.01", "lotSz": "0.01", "minSz": "0.01", "maxMktSz": "100000"}]


def okx_body(path, params=None):
    return {"code": "0", "data": api(path, params), "msg": ""}


class MarketTests(unittest.TestCase):
    def market(self, custom=api):
        s.clear_specs_cache()
        s.clear_klines_cache(remove_file=True)
        s.clear_candle_rate_limit()
        with patch.object(s, "public_get", side_effect=custom), \
             patch.object(s.time, "time", return_value=NOW / 1000):
            return s.get_strategy_market_data("BTC/USDT")

    def test_live_bar_cannot_change_closed_indicators(self):
        before = self.market()
        def altered(path, params=None):
            if path.endswith("candles"):
                return okx_pages(rows(params["bar"].lower(), 100000), params)
            return api(path, params)
        after = self.market(altered)
        for key in ["4h_ema20", "4h_ema200", "4h_adx", "1h_rsi", "1h_atr"]:
            self.assertEqual(before[key], after[key])
        self.assertEqual(before["price"], 108)
        self.assertGreaterEqual(len(before["bars_4h"]), 750)
        # 未收盘 bar 不进任何指标样本
        self.assertTrue((before["bars_4h"].confirmed == 1).all())

    def test_current_price_updates_independently(self):
        def altered(path, params=None):
            body = api(path, params)
            if path.endswith("ticker"):
                body[0]["last"] = "109"
            return body
        self.assertEqual(self.market()["4h_adx"], self.market(altered)["4h_adx"])
        self.assertEqual(self.market(altered)["price"], 109)

    def test_missing_funding_rejected(self):
        def altered(path, params=None):
            body = copy.deepcopy(api(path, params))
            if path.endswith("funding-rate"):
                body[0].pop("fundingRate")
            return body
        with self.assertRaisesRegex(s.StrategyRejected, "费率"):
            self.market(altered)

    def test_bad_quotes_rejected(self):
        for field, value in [("ts", str(NOW - 180_000)), ("last", "NaN"),
                             ("instId", "ETH-USDT-SWAP"), ("bidPx", "108.5")]:
            def altered(path, params=None):
                body = copy.deepcopy(api(path, params))
                if path.endswith("ticker"):
                    body[0][field] = value
                return body
            with self.subTest(field=field), self.assertRaises(s.StrategyRejected):
                self.market(altered)

    def test_bad_candles_rejected(self):
        for case in ["few", "gap", "stale", "bad_ohlc", "unconfirmed"]:
            def altered(path, params=None):
                body = copy.deepcopy(api(path, params))
                if path.endswith("candles"):
                    full = rows(params["bar"].lower())
                    if case == "few":
                        full = full[-250:]
                    if case == "gap" and params["bar"] == "1H":
                        full.pop(-3)
                    if case == "stale":
                        full = full[:-4]
                    if case == "bad_ohlc":
                        full[-2][2] = 0
                    if case == "unconfirmed" and params["bar"] == "1H":
                        full[-2][8] = "0"
                    return full if not params.get("after") else \
                        [r for r in full if float(r[0]) < float(params["after"])][-params["limit"]:]
                return body
            with self.subTest(case=case), self.assertRaises(s.StrategyRejected):
                self.market(altered)

    def test_non_usdt_or_suspended_contract_rejected(self):
        for field, value in [("state", "suspend"), ("ctType", "inverse"),
                             ("settleCcy", "USDC")]:
            def altered(path, params=None):
                body = copy.deepcopy(api(path, params))
                if path.endswith("instruments"):
                    body[0][field] = value
                return body
            with self.subTest(field=field), \
                 self.assertRaisesRegex(s.StrategyRejected, "USDT 永续"):
                self.market(altered)

    def test_funding_interval_derived_from_api(self):
        # 实测推导: next-prev 间隔 4h 的品种, 周期必须是 4 而非写死 8
        def altered(path, params=None):
            body = copy.deepcopy(api(path, params))
            if path.endswith("funding-rate"):
                body[0]["prevFundingTime"] = str(NOW - 3 * s.HOUR_MS)
            return body
        self.assertEqual(self.market(altered)["funding_interval_hours"], 4)

    def test_funding_clock_mismatch_rejected(self):
        def altered(path, params=None):
            body = copy.deepcopy(api(path, params))
            if path.endswith("funding-rate"):
                # prev=NOW-3h, next=NOW+18h -> 观测间隔 21h, 连跳两次以上, 拒绝
                body[0]["prevFundingTime"] = str(NOW - 3 * s.HOUR_MS)
                body[0]["nextFundingTime"] = str(NOW + 18 * s.HOUR_MS)
            return body
        with self.assertRaisesRegex(s.StrategyRejected, "结算周期"):
            self.market(altered)

    def test_skipped_settlement_observed_interval_capped_to_8h(self):
        # OKX 费率极低时跳过一次结算, 观测间隔 16h: 周期按 8h 保守预算, 不拒绝
        def altered(path, params=None):
            body = copy.deepcopy(api(path, params))
            if path.endswith("funding-rate"):
                body[0]["prevFundingTime"] = str(NOW - 15 * s.HOUR_MS)
            return body
        data = self.market(altered)
        self.assertEqual(data["funding_interval_hours"], 8)

    def test_contract_specs_in_data(self):
        data = self.market()
        # OKX 张数语义: BTC 1 张 = 0.01 BTC, 最小名义按 minSz*ctVal*price 推导
        self.assertEqual(data["ct_val"], 0.01)
        self.assertEqual(data["tick_size"], 0.01)
        self.assertEqual(data["qty_step"], 0.01)
        self.assertEqual(data["min_qty"], 0.01)
        self.assertAlmostEqual(data["min_notional"], 0.01 * 0.01 * 108)
        self.assertEqual(data["funding_interval_hours"], 8)

    def test_http_failure_has_no_cross_market_fallback(self):
        with patch.object(s.requests, "get") as get:
            get.return_value.raise_for_status.side_effect = s.requests.HTTPError("503")
            with self.assertRaises(s.requests.HTTPError):
                s.public_get("/api/v5/market/candles")
            self.assertTrue(get.call_args.args[0].startswith(s.OKX_API))

    def test_okx_business_error_rejected(self):
        with patch.object(s.requests, "get") as get:
            get.return_value.json.return_value = {"code": "50011", "data": [], "msg": "rate limit"}
            get.return_value.raise_for_status.return_value = None
            with self.assertRaisesRegex(s.StrategyRejected, "50011"):
                s.public_get("/api/v5/market/ticker")

    def test_batch_quote_snapshot_reuses_ticker_mark_and_funding(self):
        calls = []

        def counted(path, params=None):
            calls.append((path, params))
            return api(path, params)

        with patch.object(s, "public_get", side_effect=counted):
            snapshot = s.load_quote_snapshot()
            first = s.quote("BTC/USDT", NOW, snapshot)
            second = s.quote("BTC/USDT", NOW, snapshot)
        self.assertEqual(sum(p.endswith("/tickers") for p, _ in calls), 1)
        self.assertEqual(sum(p.endswith("/mark-price") for p, _ in calls), 1)
        self.assertEqual(sum(p.endswith("/funding-rate") for p, _ in calls), 1)
        self.assertEqual(first, second)

    def test_4h_cache_skips_refetch_in_same_bar(self):
        old_path = s.KLINE_CACHE_PATH
        with tempfile.TemporaryDirectory() as directory:
            s.KLINE_CACHE_PATH = os.path.join(directory, "klines.json")
            s.clear_klines_cache(remove_file=True)
            s.clear_candle_rate_limit()
            try:
                calls = []

                def counted(path, params=None):
                    calls.append((path, params))
                    return api(path, params)

                with patch.object(s, "public_get", side_effect=counted), \
                     patch.object(s.time, "time", return_value=NOW / 1000):
                    s.get_strategy_market_data("BTC/USDT")
                self.assertEqual(sum(p.endswith("candles") and q.get("bar") == "4H"
                                     for p, q in calls), 3)
                s.flush_klines_cache()

                calls.clear()
                with patch.object(s, "public_get", side_effect=counted), \
                     patch.object(s.time, "time", return_value=NOW / 1000):
                    s.get_strategy_market_data("BTC/USDT")
                self.assertFalse(any(p.endswith("candles") and q.get("bar") == "4H"
                                     for p, q in calls))
            finally:
                s.clear_klines_cache(remove_file=True)
                s.KLINE_CACHE_PATH = old_path
                s.clear_klines_cache()

    def test_4h_cache_fetches_one_page_after_new_bar(self):
        import sys
        module = sys.modules[__name__]
        old_path = s.KLINE_CACHE_PATH
        with tempfile.TemporaryDirectory() as directory:
            s.KLINE_CACHE_PATH = os.path.join(directory, "klines.json")
            s.clear_klines_cache(remove_file=True)
            s.clear_candle_rate_limit()
            try:
                with patch.object(s, "public_get", side_effect=api), \
                     patch.object(s.time, "time", return_value=NOW / 1000):
                    s.get_strategy_market_data("BTC/USDT")
                s.flush_klines_cache()

                new_now = NOW + 4 * s.HOUR_MS
                calls = []

                def counted(path, params=None):
                    calls.append((path, params))
                    return api(path, params)

                with patch.object(module, "NOW", new_now), \
                     patch.object(s, "public_get", side_effect=counted), \
                     patch.object(s.time, "time", return_value=new_now / 1000):
                    s.get_strategy_market_data("BTC/USDT")
                self.assertEqual(sum(p.endswith("candles") and q.get("bar") == "4H"
                                     for p, q in calls), 1)
            finally:
                s.clear_klines_cache(remove_file=True)
                s.KLINE_CACHE_PATH = old_path
                s.clear_klines_cache()

    def test_candle_rate_limit_waits_at_40_calls(self):
        s.clear_candle_rate_limit()
        try:
            with patch.object(s.time, "monotonic", return_value=100.0), \
                 patch.object(s.time, "sleep") as sleep:
                for _ in range(s.CANDLES_RATE_LIMIT):
                    s._throttle_candle_call()
                self.assertFalse(sleep.called)
                s._throttle_candle_call()
                self.assertTrue(sleep.called)
        finally:
            s.clear_candle_rate_limit()

    def test_candles_request_retries_transient_failure(self):
        s.clear_candle_rate_limit()
        try:
            with patch.object(s, "public_get",
                              side_effect=[s.requests.HTTPError("503"), []]) as get, \
                 patch.object(s.time, "sleep") as sleep:
                self.assertEqual(s._candles_get({"instId": "BTC-USDT-SWAP",
                                                 "bar": "4H", "limit": 1}), [])
            self.assertEqual(get.call_count, 2)
            self.assertTrue(sleep.called)
        finally:
            s.clear_candle_rate_limit()


if __name__ == "__main__":
    unittest.main()
