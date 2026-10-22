import unittest

import report


EPOCH = '2026-01-01T00:00:00+08:00'


def signal(symbol, reason, net, risk=1.0, when='2026-02-01T10:00:00+08:00',
           version='v2', direction='long', eligible=True):
    return {
        'symbol': symbol, 'direction': direction, 'status': 'exited',
        'exit_reason': reason, 'pnl_now': net or 0, 'pnl_net_pct': net,
        'risk_pct': risk, 'stats_eligible': eligible,
        'strategy_version': version,
        'triggered_at': when, 'entry_filled_at': when,
        'last_checked_at': when, 'mfe': 1.0, 'mae': -1.0,
    }


class ReportTests(unittest.TestCase):
    def records(self):
        return [
            signal('BTC/USDT', 'took_profit', 2.0, when='2026-02-01T10:00:00+08:00'),
            signal('ETH/USDT', 'stopped_out', -1.05, when='2026-02-02T10:00:00+08:00'),
            signal('SOL/USDT', 'timeout', 0.1, when='2026-02-03T10:00:00+08:00'),
            signal('XRP/USDT', 'not_entered', None, when='2026-02-04T10:00:00+08:00'),
            signal('DOGE/USDT', 'manual_reset', 3.0, when='2026-02-05T10:00:00+08:00',
                   eligible=False),
            signal('ADA/USDT', 'took_profit', 4.0, when='2025-12-01T10:00:00+08:00',
                   version='legacy'),
        ]

    def test_net_outcomes_wilson_and_not_entered(self):
        result = report.build_report(self.records(), epoch=EPOCH)
        main = result['main']
        self.assertEqual(main['n'], 3)
        self.assertEqual((main['wins'], main['losses'], main['neutral']), (1, 1, 1))
        self.assertAlmostEqual(main['expectancy_r'], (2.0 - 1.05 + 0.1) / 3, places=6)
        self.assertEqual(main['max_consecutive_losses'], 1)
        self.assertAlmostEqual(result['not_entered_rate'], 1 / 4)
        low, high = main['wilson95']
        self.assertLess(low, main['win_rate'])
        self.assertGreater(high, main['win_rate'])
        self.assertEqual(result['excluded']['manual_reset'], 1)
        self.assertEqual(result['excluded']['before_epoch'], 1)
        self.assertEqual(result['excluded']['not_entered'], 1)
        self.assertEqual(set(result['versions']), {'v2'})

    def test_report_is_read_only_and_markdown_renders(self):
        records = self.records()
        before = [dict(item) for item in records]
        result = report.build_report(records, epoch=EPOCH)
        text = report.render_markdown(result)
        self.assertIn('Crypto Radar 净口径报表', text)
        self.assertIn('观察中', text)
        self.assertEqual(records, before)

    def test_concurrent_batch_is_grouped(self):
        records = [
            signal('BTC/USDT', 'took_profit', 2.0, when='2026-02-01T10:01:00+08:00'),
            signal('ETH/USDT', 'stopped_out', -1.05, when='2026-02-01T10:09:00+08:00'),
            signal('SOL/USDT', 'timeout', 0.1, when='2026-02-01T10:31:00+08:00'),
        ]
        result = report.build_report(records, epoch=EPOCH)
        self.assertEqual(len(result['batches']), 1)
        self.assertEqual(result['batches'][0]['n'], 2)
        self.assertEqual(result['batches'][0]['symbols'], ['BTC/USDT', 'ETH/USDT'])

    def test_legacy_without_net_is_excluded(self):
        old = signal('BTC/USDT', 'took_profit', None, when='2026-02-01T10:00:00+08:00')
        result = report.build_report([old], epoch=EPOCH)
        self.assertEqual(result['main']['n'], 0)
        self.assertEqual(result['excluded']['legacy_no_net'], 1)


if __name__ == '__main__':
    unittest.main()
