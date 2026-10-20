import copy
import unittest
from unittest.mock import patch

import strategy as s

NOW = 1800000000000 // s.HOUR_MS * s.HOUR_MS + 25 * 60_000


def rows(interval, spike=110):
    hours, count = (4, 800) if interval == "4h" else (1, 300)
    duration = hours * s.HOUR_MS
    end = NOW // duration * duration
    result = []
    for i in range(count):
        ts = end - (count - 1 - i) * duration
        price = 100 + i * .01 if i < count - 1 else spike
        result.append([ts, price, price + 1, price - 1, price, 10, ts + duration - 1])
    return result


def api(path, params=None):
    if path.endswith("klines"):
        return rows(params["interval"])
    if path.endswith("premiumIndex"):
        return {"symbol": "BTCUSDT", "time": NOW, "markPrice": "108", "lastFundingRate": "0.0001", "nextFundingTime": NOW + s.HOUR_MS}
    if path.endswith("bookTicker"):
        return {"symbol": "BTCUSDT", "time": NOW, "bidPrice": "107.99", "askPrice": "108.01"}
    if path.endswith("price"):
        return {"symbol": "BTCUSDT", "time": NOW, "price": "108"}
    if path.endswith("fundingInfo"):
        return []
    return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}


class MarketTests(unittest.TestCase):
    def market(self, custom=api):
        with patch.object(s, "public_get", side_effect=custom), patch.object(s.time, "time", return_value=NOW / 1000):
            return s.get_strategy_market_data("BTC/USDT")

    def test_live_bar_cannot_change_closed_indicators(self):
        before = self.market()
        def altered(path, params=None):
            return rows(params["interval"], 10000) if path.endswith("klines") else api(path, params)
        after = self.market(altered)
        for key in ["4h_ema20", "4h_ema200", "4h_adx", "1h_rsi", "1h_atr"]:
            self.assertEqual(before[key], after[key])
        self.assertEqual(before["price"], 108)
        self.assertGreaterEqual(len(before["bars_4h"]), 750)

    def test_current_price_updates_independently(self):
        def altered(path, params=None):
            body = api(path, params)
            if path.endswith("price"):
                body["price"] = "109"
            return body
        self.assertEqual(self.market()["4h_adx"], self.market(altered)["4h_adx"])
        self.assertEqual(self.market(altered)["price"], 109)

    def test_missing_funding_rejected(self):
        def altered(path, params=None):
            body = api(path, params)
            if path.endswith("premiumIndex"):
                body.pop("lastFundingRate")
            return body
        with self.assertRaisesRegex(s.StrategyRejected, "费率缺失"):
            self.market(altered)

    def test_bad_quotes_rejected(self):
        for field, value in [("time", NOW - 180_000), ("price", "NaN"), ("symbol", "ETHUSDT")]:
            def altered(path, params=None):
                body = api(path, params)
                if path.endswith("price"):
                    body[field] = value
                return body
            with self.subTest(field=field), self.assertRaises(s.StrategyRejected):
                self.market(altered)

    def test_bad_candles_rejected(self):
        for case in ["few", "gap", "stale", "bad_ohlc"]:
            def altered(path, params=None):
                body = copy.deepcopy(api(path, params))
                if path.endswith("klines"):
                    if case == "few": body = body[-250:]
                    if case == "gap": body.pop(-3)
                    if case == "stale": body = body[:-3]
                    if case == "bad_ohlc": body[-2][2] = 0
                return body
            with self.subTest(case=case), self.assertRaises(s.StrategyRejected):
                self.market(altered)

    def test_spot_contract_rejected(self):
        def altered(path, params=None):
            body = api(path, params)
            if path.endswith("exchangeInfo"):
                body["symbols"][0]["contractType"] = "CURRENT_QUARTER"
            return body
        with self.assertRaisesRegex(s.StrategyRejected, "USDT 永续"):
            self.market(altered)

    def test_adjusted_funding_interval_is_used(self):
        def altered(path, params=None):
            if path.endswith("fundingInfo"):
                return [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}]
            return api(path, params)
        self.assertEqual(self.market(altered)["funding_interval_hours"], 4)

    def test_http_failure_has_no_cross_market_fallback(self):
        with patch.object(s.requests, "get") as get:
            get.return_value.raise_for_status.side_effect = s.requests.HTTPError("451")
            with self.assertRaises(s.requests.HTTPError): s.public_get("/fapi/v1/klines")
            self.assertTrue(get.call_args.args[0].startswith(s.FUTURES_API))


if __name__ == "__main__":
    unittest.main()
