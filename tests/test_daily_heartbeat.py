import unittest
from unittest.mock import patch

import daily_heartbeat as h


class DailyHeartbeatTests(unittest.TestCase):
    def test_message_contains_pool_positions_and_last_scan(self):
        store = {"signals": [
            {"status": "active", "direction": "long",
             "last_checked_at": "2026-10-22T07:45:00+08:00"},
            {"status": "active", "direction": "short",
             "last_checked_at": "2026-10-22T07:50:00+08:00"},
            {"status": "exited", "direction": "long",
             "last_checked_at": "2026-10-22T07:55:00+08:00"},
        ]}
        text = h.build_message(store, {"symbols": ["BTC/USDT", "ETH/USDT"]})
        self.assertIn("标的池: 2", text)
        self.assertIn("在持: 2（多 1 / 空 1）", text)
        self.assertIn("最近扫描: 2026-10-22T07:55:00+08:00", text)

    def test_main_sends_once_with_configured_chat(self):
        store = {"signals": []}
        with patch.object(h, "TG_BOT_TOKEN", "token"), \
             patch.object(h, "TG_CHAT_ID", "chat"), \
             patch.object(h.tracker, "load_signals", return_value=store), \
             patch.object(h.universe, "load_cached", return_value={"symbols": ["BTC/USDT"]}), \
             patch.object(h.requests, "post") as post:
            h.main()
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "chat")
        self.assertIn("标的池: 1", post.call_args.kwargs["json"]["text"])

    def test_main_can_be_disabled(self):
        with patch.object(h, "DAILY_ENABLED", False), \
             patch.object(h.requests, "post") as post:
            h.main()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
