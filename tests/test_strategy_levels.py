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

    def test_unknown_or_nearby_structure_rejected(self):
        for case in ("unknown", "nearby"):
            data = market()
            for frame in (data["bars_1h"], data["bars_4h"]):
                frame.loc[frame.high > 105, "high"] = 100.4
            if case == "nearby": data["bars_1h"].loc[len(data["bars_1h"])-10,"high"] = 103
            with self.subTest(case=case), clock(), self.assertRaises(s.StrategyRejected): s.build_plan(data)

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
