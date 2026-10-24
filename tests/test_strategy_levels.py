import copy
import unittest

import strategy as s
from strategy_fixtures import market, clock


class LevelTests(unittest.TestCase):
    def make(self, side="long"):
        data = market(side)
        with clock(): plan = s.build_plan(data)
        return data, plan

    def test_long_and_short_net_rr_across_entry_range(self):
        for side in ("long", "short"):
            data, plan = self.make(side)
            self.assertGreaterEqual(plan["net_rr"], 2)
            for entry in [plan["entry_min"], plan["entry"], plan["entry_max"]]:
                loss = abs(entry-plan["stop"])+s.cost_per_unit(data,entry,plan["stop"],side)
                gain = abs(plan["tp1"]-entry)-s.cost_per_unit(data,entry,plan["tp1"],side)
                self.assertGreaterEqual(gain/loss,2)

    def test_open_space_allowed_nearby_structure_rejected(self):
        """2026-10-24 语义修正: 前方无显著结构是开阔空间(放行), 不再是拒绝理由。
        旧断言要求两者都拒绝, 正是把策略整体关闭的那条 bug 的测试侧镜像。"""
        data = market()
        for frame in (data["bars_1h"], data["bars_4h"]):
            frame.loc[frame.high > 105, "high"] = 100.4
        with clock():
            plan = s.build_plan(data)
        self.assertIsNone(plan["structure_boundary"])
        self.assertIn("暂无显著结构", s.format_strategy_text(plan))
        # 显著结构就在 TP2 之前: 目标不可越过它
        data = market()
        for frame in (data["bars_1h"], data["bars_4h"]):
            frame.loc[frame.high > 105, "high"] = 100.4
        data["bars_1h"].loc[len(data["bars_1h"])-10, "high"] = 103
        with clock(), self.assertRaisesRegex(s.StrategyRejected, "不足以容纳净 2R"):
            s.build_plan(data)

    def test_noise_pivots_do_not_form_boundary(self):
        """3 根 K 线局部极值本身匹配噪声; 突出度不足 MIN_PROMINENCE_ATR*ATR
        的枢轴不得构成边界(否则旧 bug 复现: 边界总在 TP2 之前)。"""
        data = market()
        for frame in (data["bars_1h"], data["bars_4h"]):
            frame.loc[frame.high > 105, "high"] = 100.4
        # 前方 0.3 ATR 处的微小凸起: 不是结构
        data["bars_1h"].loc[len(data["bars_1h"])-30, "high"] = 100.7
        self.assertIsNone(s.structure_boundary(data, "long", 101.1))
        with clock():
            self.assertIsNone(s.build_plan(data)["structure_boundary"])
        # 同样的凸起若突出 3 ATR 就是真结构
        data["bars_1h"].loc[len(data["bars_1h"])-30, "high"] = 103.4
        self.assertAlmostEqual(
            s.structure_boundary(data, "long", 101.1), 103.4)

    def test_boundary_as_target_cap_not_veto(self):
        """结构在 TP2 之后且空间足够 = 放行并记录边界; 结构把空间压到不足
        3R 之内 = 拒绝。boundary 是目标上限, 不是一票否决。"""
        for offset, expect_ok in ((12.0, True), (4.0, False)):
            data = market()
            for frame in (data["bars_1h"], data["bars_4h"]):
                frame.loc[frame.high > 105, "high"] = 100.4
            data["bars_1h"].loc[len(data["bars_1h"])-30, "high"] = 101 + offset
            with self.subTest(offset=offset), clock():
                if expect_ok:
                    plan = s.build_plan(data)
                    self.assertAlmostEqual(plan["structure_boundary"], 101 + offset)
                    self.assertLess(plan["tp2"], plan["structure_boundary"])
                else:
                    with self.assertRaises(s.StrategyRejected):
                        s.build_plan(data)

    def test_prominence_threshold_is_configurable(self):
        """枢轴突出度 = 与邻居(100.4)之差; 1.6 ATR 的凸起在阈值 2.0 下被当噪声
        丢弃, 在阈值 1.0 下算结构。"""
        data = market()
        for frame in (data["bars_1h"], data["bars_4h"]):
            frame.loc[frame.high > 105, "high"] = 100.4
        data["bars_1h"].loc[len(data["bars_1h"])-30, "high"] = 102.0  # 突出 1.6 ATR
        original = s.MIN_PROMINENCE_ATR
        try:
            s.MIN_PROMINENCE_ATR = 2.0
            self.assertIsNone(s.structure_boundary(data, "long", 101.1))
            s.MIN_PROMINENCE_ATR = 1.0
            self.assertAlmostEqual(s.structure_boundary(data, "long", 101.1), 102.0)
        finally:
            s.MIN_PROMINENCE_ATR = original

    def test_short_side_boundary_uses_lows(self):
        data = market("short")
        for frame in (data["bars_1h"], data["bars_4h"]):
            frame.loc[frame.low < 95, "low"] = 99.5
        data["bars_1h"].loc[len(data["bars_1h"])-30, "low"] = 96.5
        self.assertAlmostEqual(s.structure_boundary(data, "short", 98.9), 96.5)

    def test_bad_numbers_direction_and_hours_rejected(self):
        data, plan = self.make()
        for key,value in [("stop",float("nan")),("tp1",-1),("tp2",float("inf")),("entry",True),("direction","flat"),("hold_hours",-1),("hold_hours",1000000)]:
            bad = {**plan,key:value}
            with self.subTest(key=key,value=value),self.assertRaises(s.StrategyRejected): s.validate_plan(bad,data)

    def test_low_rr_wrong_order_and_narrow_stop_rejected(self):
        data, plan = self.make()
        for change in [{"tp1":plan["entry_max"]+.1},{"stop":plan["entry_min"]-.1},{"tp2":plan["structure_boundary"]+1},{"entry":90}]:
            with self.subTest(change=change),self.assertRaises(s.StrategyRejected):s.validate_plan({**plan,**change},data)

    def test_too_wide_structure_stop_rejected(self):
        data = market(); data["bars_1h"].loc[len(data["bars_1h"])-4,"low"] = 90
        with clock(), self.assertRaisesRegex(s.StrategyRejected,"止损须"):s.build_plan(data)

    def test_rounding_and_size_remain_within_budget(self):
        for side in ("long","short"):
            data,plan=self.make(side)
            result=s.risk_size(plan,1000)
            self.assertLessEqual(result["estimated_loss"],5)
            self.assertLessEqual(result["notional_max"],1000)
            for key in ("stop","tp1","tp2","entry_min","entry_max"):
                self.assertAlmostEqual(plan[key],s.round_tick(plan[key],data["tick_size"]))

    def test_lots_semantics_and_min_order(self):
        # OKX 张数语义: qty 是整数张, base_qty = 张数 * ctVal 个币
        data,plan=self.make()
        result=s.risk_size(plan,1000)
        self.assertEqual(result["qty"],int(result["qty"]))
        self.assertAlmostEqual(result["base_qty"],result["qty"]*data["ct_val"])
        self.assertAlmostEqual(result["notional_max"],result["base_qty"]*plan["entry_max"])
        # 最小张数 minSz 与最小名义都不满足时拒绝
        tiny={"ct_val":data["ct_val"],"qty_step":data["qty_step"],
              "min_qty":1e9,"max_qty":data["max_qty"],"min_notional":1e9,
              "loss_per_unit":plan["loss_per_unit"],"entry_max":plan["entry_max"],
              "entry_min":plan["entry_min"]}
        with self.assertRaises(s.StrategyRejected): s.risk_size(tiny,1000)

    def test_tiny_account_not_forced_to_minimum_order(self):
        _,plan=self.make()
        with self.assertRaisesRegex(s.StrategyRejected,"不可加仓凑单"):s.risk_size(plan,1)

    def test_adverse_funding_included_never_credit(self):
        for side in ("long","short"):
            data,plan=self.make(side)
            data["funding_rate"] = .001 if side=="long" else -.001
            adverse=s.cost_per_unit(data,100,95,side)
            data["funding_rate"] *= -1
            favorable=s.cost_per_unit(data,100,95,side)
            self.assertAlmostEqual(adverse-favorable,.4)
            self.assertAlmostEqual(favorable,195*(s.TAKER_FEE+s.SLIPPAGE))

    def test_spread_mark_divergence_and_opposite_signal_rejected(self):
        for change in [{"ask":102},{"mark_price":102}]:
            data=market();data.update(change)
            with self.subTest(change=change),clock(),self.assertRaises(s.StrategyRejected):s.build_plan(data)
        with clock(),self.assertRaises(s.StrategyRejected):s.build_plan(market(),"short")


if __name__ == "__main__": unittest.main()
