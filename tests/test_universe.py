import json
import os
import tempfile
import unittest
from unittest.mock import patch

import universe


NOW = 1_800_000_000_000
DAY = 24 * universe.strategy.HOUR_MS


def instrument(symbol, state='live', list_age_days=300):
    return {'instId': f'{symbol}-USDT-SWAP', 'instType': 'SWAP', 'state': state,
            'ctType': 'linear', 'settleCcy': 'USDT', 'ctVal': '1',
            'listTime': str(NOW - list_age_days * DAY),
            'tickSz': '0.01', 'lotSz': '1', 'minSz': '1', 'maxMktSz': '100000'}


def ticker(symbol, turnover, bid=100.0, ask=100.02):
    return {'instId': f'{symbol}-USDT-SWAP', 'last': '100',
            'vol24h': str(turnover), 'volCcy24h': str(turnover),
            'bidPx': str(bid), 'askPx': str(ask)}


def fake_api(instruments, tickers, oi=None):
    oi = oi or {}
    def get(path, params=None):
        if path.endswith('instruments'):
            return instruments
        if path.endswith('tickers'):
            return tickers
        if path.endswith('open-interest'):
            return [{'instId': f'{s}-USDT-SWAP', 'oi': '1',
                     'oiCcy': str(oi.get(s, 10_000_000))} for s in oi]
        if path.endswith('mark-price'):
            return [{'instId': f'{s}-USDT-SWAP', 'markPx': '100'} for s in oi]
        raise AssertionError(path)
    return get


class UniverseTests(unittest.TestCase):
    def test_filters_and_ranks_by_turnover(self):
        instruments = [instrument('BTC'), instrument('ETH'), instrument('SOL'),
                       instrument('DOGE'), instrument('NEW', list_age_days=10),
                       instrument('OFF', state='suspend')]
        tickers = [ticker('BTC', 100_000_000), ticker('ETH', 80_000_000),
                   ticker('SOL', 60_000_000), ticker('DOGE', 40_000),
                   ticker('NEW', 90_000_000), ticker('OFF', 90_000_000)]
        oi = {s: 10_000_000 for s in ('BTC', 'ETH', 'SOL', 'DOGE', 'NEW', 'OFF')}
        with patch.object(universe.strategy, 'public_get', side_effect=fake_api(instruments, tickers, oi)):
            ranked = universe.fetch_ranking(NOW)
        self.assertEqual([item['symbol'] for item in ranked],
                         ['BTC/USDT', 'ETH/USDT', 'SOL/USDT'])
        self.assertEqual(ranked[0]['rank'], 1)

    def test_hysteresis_keeps_old_until_out_of_top45(self):
        ranked = [{'symbol': 'AAA/USDT', 'rank': 1}, {'symbol': 'BBB/USDT', 'rank': 2},
                  {'symbol': 'CCC/USDT', 'rank': 40}, {'symbol': 'DDD/USDT', 'rank': 50}]
        selected = universe.apply_hysteresis(['CCC/USDT', 'DDD/USDT'], ranked)
        self.assertIn('CCC/USDT', selected)
        self.assertNotIn('DDD/USDT', selected)
        self.assertIn('AAA/USDT', selected)

    def test_refresh_writes_and_load_or_refresh_reuses_fresh_cache(self):
        instruments = [instrument('BTC'), instrument('ETH')]
        tickers = [ticker('BTC', 100_000_000), ticker('ETH', 80_000_000)]
        oi = {'BTC': 10_000_000, 'ETH': 10_000_000}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(universe, 'DATA_PATH', os.path.join(tmp, 'universe.json')), \
             patch.object(universe.strategy, 'public_get',
                          side_effect=fake_api(instruments, tickers, oi)):
            payload = universe.refresh(NOW)
            self.assertEqual(payload['symbols'], ['BTC/USDT', 'ETH/USDT'])
            with open(universe.DATA_PATH, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['version'], payload['version'])
            with patch.object(universe.strategy, 'public_get',
                              side_effect=AssertionError('network must not be called')):
                cached = universe.load_or_refresh(NOW + 1000)
            self.assertEqual(cached['version'], payload['version'])

    def test_refresh_failure_keeps_old_pool(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(universe, 'DATA_PATH', os.path.join(tmp, 'universe.json')):
            old = {'version': 'u-old', 'generated_at_ms': NOW - 10 * universe.strategy.HOUR_MS,
                   'symbols': ['BTC/USDT'], 'members': {'BTC/USDT': 1}}
            with open(universe.DATA_PATH, 'w', encoding='utf-8') as handle:
                json.dump(old, handle)
            with patch.object(universe.strategy, 'public_get',
                              side_effect=universe.strategy.StrategyRejected('down')):
                self.assertEqual(universe.load_or_refresh(NOW)['version'], 'u-old')


if __name__ == '__main__':
    unittest.main()
