"""回放测试: 判定栈必须在真实数据上非空。

存在理由(2026-10-24): strategy_fixtures.py 把阻力人工植入 9-11 ATR 之外, 使
structure_boundary 的门槛在测试中永远通过; 而真实数据里前方 0.19 ATR 就有枢轴,
门槛通过率 0/153 —— 74 个测试全绿, 生产上线后一条信号都没推送过。

这些用例用录制的真实 OKX 1H 快照(tests/fixtures/doge_1h.json.gz, 离线, 不联网)
跑生产判定栈本身, 断言漏斗非空。把 MIN_PROMINENCE_ATR 调回 0(复现旧 bug)时
断言必须失败。
"""
import os
import unittest

import strategy as S
import backtest as B

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "doge_1h.json.gz")


@unittest.skipUnless(os.path.exists(FIXTURE), "录制快照缺失")
class ReplayTests(unittest.TestCase):
    """离线重放: 快照已录制, 不联网, 单轮约 1.3 秒。"""

    @classmethod
    def setUpClass(cls):
        cls.frames, cls.specs = B.load_snapshot(FIXTURE)

    def replay(self):
        return B.run_frames({k: v for k, v in self.frames.items()},
                            dict(self.specs), 24)

    def test_funnel_is_not_empty(self):
        """核心回归: 生产判定栈在真实数据上必须能产出信号。"""
        result = self.replay()
        passed = result["funnel"].get("PASSED", 0)
        self.assertGreater(
            passed, 0,
            f"漏斗为空, 判定栈在真实数据上无法产出信号: {dict(result['funnel'])}")

    def test_prominence_filter_is_what_keeps_it_alive(self):
        """把突出度阈值调回 0(旧语义: 任意 3 根 K 线极值都算结构)必须回到零信号。
        这条断言证明上一条测试真的在守这个 bug, 而不是恰好通过。"""
        saved = S.MIN_PROMINENCE_ATR
        try:
            S.MIN_PROMINENCE_ATR = 0.0
            result = self.replay()
            self.assertEqual(result["funnel"].get("PASSED", 0), 0,
                             "突出度阈值归零后仍有信号, 说明噪声枢轴没被过滤")
            self.assertGreater(result["funnel"].get("P3_tp1_beyond_cap", 0), 0)
        finally:
            S.MIN_PROMINENCE_ATR = saved

    def test_gate_keys_are_stable(self):
        """漏斗 key 必须落在已知集合内 —— 出现 OTHER 说明生产改了错误文案而
        回放侧的映射没跟上, 漏斗会静默失真。"""
        result = self.replay()
        unexpected = [k for k in result["funnel"]
                      if k.startswith(("OTHER", "DATA_"))]
        self.assertEqual(unexpected, [], f"未映射的拒绝原因: {unexpected}")

    def test_replay_reads_production_thresholds_at_runtime(self):
        """回放走生产 signal_direction/build_plan, 不在本文件复制门槛字面量。
        把 strategy.ADX_FLOOR 抬到几乎全拒, 漏斗必须随之变化 —— 否则说明回放
        侧留了加载期别名快照, 会重新引入"两套口径漂移"。"""
        saved = S.ADX_FLOOR
        try:
            S.ADX_FLOOR = 999.0
            frames, specs = B.load_snapshot(FIXTURE)
            result = B.run_frames(frames, specs, 24)
            self.assertGreater(result["funnel"].get("G1_adx", 0), 1000)
            self.assertEqual(result["funnel"].get("PASSED", 0), 0)
        finally:
            S.ADX_FLOOR = saved

    def test_snapshot_replay_does_not_write_state(self):
        """回放必须只读: 不改 signals.json, 不发网络请求(快照模式)。"""
        import tracker
        path = tracker.DATA_PATH
        before = os.path.getmtime(path) if os.path.exists(path) else None
        before_size = os.path.getsize(path) if os.path.exists(path) else None
        self.replay()
        if os.path.exists(path):
            self.assertEqual(os.path.getmtime(path), before)
            self.assertEqual(os.path.getsize(path), before_size)

    def test_stats_shapes(self):
        result = self.replay()
        s = B.stats(result["events"])
        self.assertEqual(s["signals"], result["funnel"].get("PASSED", 0))
        self.assertEqual(s["filled"] + s["not_entered"], s["signals"])
        for key in ("win_rate", "expectancy_r", "profit_factor"):
            self.assertTrue(s[key] is None or isinstance(s[key], float))


if __name__ == "__main__":
    unittest.main()
