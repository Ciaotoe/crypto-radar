import html
import unittest
from unittest.mock import Mock, patch

import strategy as s
from strategy_fixtures import market, clock
from test_strategy_market import NOW

import crypto_radar as radar


class FlowTests(unittest.TestCase):
    def setUp(self):
        # run_scan 的密钥守卫在模块导入时已取值, patch 模块属性而非环境变量;
        # 测试只验证流程, 不真连 TG/Gemini
        for key in ("TG_BOT_TOKEN", "TG_CHAT_ID", "GEMINI_API_KEY"):
            item = patch.object(radar, key, "test")
            item.start()
            self.addCleanup(item.stop)
        # 冻结 strategy 的时钟: 候选/推送前复核都校验报价新鲜度,
        # 真实时钟会让 fixture 行情全部"报价过期"
        frozen = patch.object(s.time, "time", return_value=NOW / 1000)
        frozen.start()
        self.addCleanup(frozen.stop)

    def make(self, side="long"):
        data = market(side)
        data["symbol"] = "BTC/USDT"
        with clock():
            plan = s.build_plan(data)
        return data, plan

    def run_scan_with(self, store, market_data, veto=None, events=None):
        """跑一遍 run_scan: 隔离档案读写、事件更新与真实网络调用。
        market_data: get_strategy_market_data 的 side_effect 序列
        (3 币种扫描 + 推送前复核), veto: ai_veto_check 返回值。"""
        events = events or []
        universe = {"version": "u-test",
                    "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
                    "members": {"BTC/USDT": 1, "ETH/USDT": 2, "SOL/USDT": 3}}
        with patch.object(radar, "get_macro_sentiment", return_value="70 (Greed)"), \
             patch.object(radar, "send_telegram_msg") as send, \
             patch.object(radar, "ai_veto_check", return_value=veto) as check, \
             patch.object(radar.universe, "load_or_refresh", return_value=universe), \
             patch.object(radar.tracker, "load_signals", return_value=store), \
             patch.object(radar.tracker, "save_signals") as save, \
             patch.object(radar, "review_signals_with_ai"), \
             patch.object(radar.tracker, "update_open_signals",
                          return_value=(False, events)), \
             patch.object(radar.strategy, "load_quote_snapshot", return_value=None), \
             patch.object(radar.strategy, "get_strategy_market_data",
                          side_effect=market_data):
            radar.run_scan()
        return send, check, save

    def test_candidates_ranked_by_comparable_metrics(self):
        base = market()
        # 高价币贴近 EMA20 的绝对差更大, 但按 ATR 归一后并列 ADX 时应胜出
        cheap_far = {**base, "symbol": "SOL/USDT", "4h_adx": 30.}
        dear_near = {**base, "symbol": "BTC/USDT", "4h_adx": 30., "price": 100.5,
                     "bid": 100.49, "ask": 100.51, "mark_price": 100.5}
        strong = {**base, "symbol": "ETH/USDT", "4h_adx": 40., "price": 110.,
                  "bid": 109.99, "ask": 110.01, "mark_price": 110.}
        # 可比指标打分: ADX 优先, 并列时 ATR 归一化贴近度决胜(绝对差不可比)
        self.assertGreater(s.candidate_score(strong), s.candidate_score(dear_near))
        self.assertGreater(s.candidate_score(dear_near), s.candidate_score(cheap_far))
        with clock():
            _, data, _ = s.build_from_candidates([cheap_far, dear_near])
            self.assertEqual(data["symbol"], "BTC/USDT")  # 按分排序后首个合格者胜出

    def test_first_choice_failure_does_not_block_others(self):
        data, plan = self.make()
        # 第一候选(ADX 更高)止损过宽被拒, 第二候选合格照常出计划
        bad = dict(data)
        bad["4h_adx"] = data["4h_adx"] + 10
        bars = bad["bars_1h"].copy()
        bars.loc[len(bars) - 4, "low"] = 90
        bad["bars_1h"] = bars
        with clock():
            plan2, data2, attempts = s.build_from_candidates([bad, data])
        self.assertIsNotNone(plan2)
        self.assertEqual(data2["symbol"], data["symbol"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][0], bad["symbol"])
        self.assertIn("止损", attempts[0][1])

    def test_push_rejected_when_signal_or_range_invalid(self):
        data, plan = self.make()
        # 方向已改变 -> 拒绝
        flipped = dict(data)
        flipped["4h_ema20"], flipped["4h_ema50"] = 98., 100.
        flipped["4h_close"] = flipped["4h_close"] - 3
        with self.assertRaisesRegex(s.StrategyRejected, "方向"):
            s.ensure_pushable(plan, flipped)
        # 现价已越过止损 -> 拒绝(不现场改点位)
        beyond_stop = {**plan, "stop": data["price"] + .5}
        with self.assertRaisesRegex(s.StrategyRejected, "止损"):
            s.ensure_pushable(beyond_stop, data)
        # 现价远离入场带超过 1 ATR -> 拒绝追价; 带外但未越止损仍允许等回踩
        far = {**plan, "entry_min": plan["entry_min"] - 20,
               "entry_max": plan["entry_max"] - 20}
        with self.assertRaisesRegex(s.StrategyRejected, "远离"):
            s.ensure_pushable(far, data)
        # 正常: 新鲜行情仍成立且价格在区间内 -> 放行
        self.assertEqual(s.ensure_pushable(plan, data), data)

    def test_no_valid_candidate_no_push_no_archive(self):
        store = {"signals": []}
        send, check, save = self.run_scan_with(
            store, [s.StrategyRejected("暂无合格机会")] * 3)
        self.assertFalse(send.called)
        self.assertFalse(check.called)
        save.assert_called_once()
        self.assertEqual(store["signals"], [])  # 无有效策略不建档

    def test_full_pipeline_push_and_archive(self):
        data, plan = self.make()
        fresh = {**data, "price": plan["entry"]}
        store = {"signals": []}
        send, check, save = self.run_scan_with(
            store, [data, s.StrategyRejected("x"), s.StrategyRejected("x"), fresh],
            veto=None)
        self.assertTrue(send.called)
        self.assertTrue(check.called)
        save.assert_called_once()
        sig = store["signals"][0]
        self.assertEqual(sig["symbol"], "BTC/USDT")
        self.assertEqual(sig["direction"], plan["direction"])
        self.assertEqual(sig["strategy"]["stop"], plan["stop"])
        self.assertEqual(sig["strategy"]["tp2"], plan["tp2"])
        self.assertIn("entry_min", sig["strategy"])
        self.assertEqual(sig["entry_model"], "pending")
        self.assertIsNone(sig["entry_filled_at"])
        self.assertEqual(sig["push_price"], fresh["price"])
        self.assertEqual(sig["universe_version"], "u-test")
        self.assertTrue(sig["stats_eligible"])
        # 推送文案由规则点位渲染, 与档案 text 一致, 不经过模型
        text = send.call_args.args[2]
        self.assertIn(f"TP2 {plan['tp2']:.2f}", text)
        self.assertEqual(text, sig["strategy"]["text"])

    def test_veto_blocks_push_and_archive(self):
        data, plan = self.make()
        fresh = {**data, "price": plan["entry"]}
        store = {"signals": []}
        send, check, save = self.run_scan_with(
            store, [data, s.StrategyRejected("x"), s.StrategyRejected("x"), fresh],
            veto="监管突发利空")
        self.assertFalse(send.called)
        save.assert_called_once()
        self.assertEqual(store["signals"], [])  # 否决即不建档

    def test_full_slots_block_new_push(self):
        data, plan = self.make()
        held = [{"symbol": symbol, "direction": "short", "status": "active",
                 "strategy": {"stop": 1}, "alerts_fired": [], "risk_pct": .5}
                for symbol in ("ETH/USDT", "SOL/USDT", "XRP/USDT")]
        store = {"signals": held}
        send, check, save = self.run_scan_with(
            store, [data, s.StrategyRejected("x"), s.StrategyRejected("x")])
        self.assertFalse(send.called)
        self.assertEqual(len(store["signals"]), 3)  # 不建新档案

    def test_slot_gate_same_direction_and_total_risk(self):
        def held(direction, risk):
            return {"symbol": f"{direction}-{risk}", "direction": direction,
                    "status": "active", "strategy": {}, "alerts_fired": [],
                    "account_risk_pct": risk}
        store = {"signals": [held("long", .5), held("long", .5)]}
        ok, why = radar.slot_available(store, "long", .5)
        self.assertFalse(ok)
        self.assertIn("方向", why)
        self.assertTrue(radar.slot_available(store, "short", .5)[0])
        store["signals"].append(held("short", .5))
        self.assertFalse(radar.slot_available(store, "short", .5)[0])

    def test_veto_failure_allows_push(self):
        data, plan = self.make()
        fresh = {**data, "price": plan["entry"]}
        store = {"signals": []}
        # 复核调用失败时放行推送(不依赖单点模型服务)
        with patch.object(radar, "ai_veto_check", side_effect=Exception("API down")):
            send = self.run_scan_with(
                store, [data, s.StrategyRejected("x"), s.StrategyRejected("x"),
                        fresh])[0]
        self.assertTrue(send.called)


if __name__ == "__main__":
    unittest.main()


class EscapeAndDedupTests(unittest.TestCase):
    def test_event_notice_escapes_html(self):
        """AI 复核 detail 含 <script> 时必须被转义, 否则 TG 400"""
        import tracker as t
        sig = {"symbol": "BTC/USDT", "direction": "long", "entry_price": 100.0,
               "last_price": 101.0, "pnl_now": 1.0, "mfe": 1.5,
               "strategy": {"stop": 97.0}, "alerts_fired": []}
        events = [{"sig": sig, "type": "alert", "reason": "ai_review",
                   "detail": "<script>alert(1)</script> 利空突袭"}]
        with patch.object(radar, "TG_BOT_TOKEN", "t"), \
             patch.object(radar, "TG_CHAT_ID", "c"), \
             patch.object(radar.requests, "post") as post:
            radar.send_event_notice(events)
        sent = post.call_args.kwargs["json"]["text"]
        self.assertNotIn("<script>", sent)                     # 原文标签不可见
        self.assertIn(html.escape("<script>alert(1)</script>"), sent)
        self.assertIn("利空突袭", sent)

    def test_ai_review_dedups_headlines_and_repeated_verdicts(self):
        """同一批旧头条不再重复复核报警; 结论与上次相同不重复推"""
        store = {"signals": [{"symbol": "BTC/USDT", "direction": "long", "status": "active",
                              "entry_price": 100.0, "pnl_now": 0.5, "mfe": 1.2,
                              "last_ai_review": None, "alerts_fired": [],
                              "seen_headlines": [], "last_ai_verdict": None,
                              "strategy": None}]}
        events = []
        data = {"price": 100.5, "4h_adx": 30., "funding_rate_pct": 0.01}
        with patch.object(radar, "fetch_news_headlines",
                          side_effect=[["旧闻A", "旧闻B"], ["旧闻A", "旧闻B"],
                                       ["旧闻A", "旧闻B", "突发C"]]), \
             patch.object(radar, "get_macro_sentiment", return_value="50"), \
             patch.object(radar, "get_market_data", return_value=data), \
             patch.object(radar, "ask_ai_review",
                          side_effect=["监管利空", "新风险"]) as ask:
            radar.review_signals_with_ai(store, events)
            self.assertEqual(len(events), 1)          # 首次结论推一次
            self.assertEqual(sorted(store["signals"][0]["seen_headlines"]), ["旧闻A", "旧闻B"])
            # 第二轮: 头条没变、无机械事件、未到 24h 兜底 -> 不调用
            radar.review_signals_with_ai(store, events)
            self.assertEqual(len(events), 1)
            self.assertEqual(ask.call_count, 1)
            # 第三轮: 出现新头条 -> 复核, 结论变化 -> 推
            radar.review_signals_with_ai(store, events)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[-1]["detail"], "新风险")

    def test_heartbeat_is_opt_in_and_never_raises(self):
        """心跳默认关闭; 开启后推送失败也不能影响巡检"""
        with patch.object(radar, "HEARTBEAT_ENABLED", False), \
             patch.object(radar.requests, "post") as post:
            radar.send_heartbeat(True, "ok")
        post.assert_not_called()

        failing = Mock()
        failing.raise_for_status.side_effect = RuntimeError("tg down")
        with patch.object(radar, "HEARTBEAT_ENABLED", True), \
             patch.object(radar, "TG_BOT_TOKEN", "t"), \
             patch.object(radar, "TG_CHAT_ID", "c"), \
             patch.object(radar.requests, "post", return_value=failing):
            radar.send_heartbeat(False, "boom")
