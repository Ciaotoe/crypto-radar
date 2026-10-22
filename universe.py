"""Dynamic OKX USDT perpetual universe with turnover/OI/liquidity filters."""
import json
import math
import os
import time
from datetime import datetime, timezone

import strategy
from runtime import atomic_write_json

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'universe.json')
TOP_N = int(os.getenv('RADAR_UNIVERSE_TOP_N', '30'))
KEEP_N = int(os.getenv('RADAR_UNIVERSE_KEEP_N', '45'))
BUCKET_CAP = int(os.getenv('RADAR_UNIVERSE_BUCKET_CAP', '5'))
REFRESH_MS = 6 * strategy.HOUR_MS
LIST_AGE_DAYS = 125
MIN_TURNOVER_USD = float(os.getenv('RADAR_MIN_TURNOVER_USD', '5000000'))
MIN_OI_USD = float(os.getenv('RADAR_MIN_OI_USD', '1000000'))
MAX_SPREAD_BPS = float(os.getenv('RADAR_MAX_SPREAD_BPS', '15'))


def _number(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise strategy.StrategyRejected(f"{name} 不是数字") from exc
    if not math.isfinite(number) or number < 0:
        raise strategy.StrategyRejected(f"{name} 不是有限非负数")
    return number


def _bulk(path, fields):
    rows = strategy.public_get(path, {"instType": "SWAP"})
    result = {}
    for row in rows:
        inst_id = row.get("instId")
        if not inst_id or any(field not in row for field in fields):
            continue
        result[inst_id] = row
    return result


def _symbol(inst_id):
    return inst_id.replace('-USDT-SWAP', '/USDT')


def _bucket(symbol):
    return symbol.replace('/USDT', '')[:1]


def fetch_ranking(now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    instruments = strategy.public_get('/api/v5/public/instruments', {'instType': 'SWAP'})
    tickers = _bulk('/api/v5/market/tickers', ['last', 'vol24h', 'bidPx', 'askPx'])
    opens = _bulk('/api/v5/public/open-interest', ['oi'])
    marks = _bulk('/api/v5/public/mark-price', ['markPx'])
    ranked = []
    cutoff = now_ms - LIST_AGE_DAYS * 24 * strategy.HOUR_MS
    for inst in instruments:
        inst_id = inst.get('instId', '')
        if not inst_id.endswith('-USDT-SWAP') or inst.get('state') != 'live' \
           or inst.get('ctType') != 'linear' or inst.get('settleCcy') != 'USDT':
            continue
        if not inst.get('listTime'):
            continue
        listed = _number(inst.get('listTime'), '上市时间')
        if listed > cutoff:
            continue
        ticker = tickers.get(inst_id)
        opening = opens.get(inst_id)
        mark = marks.get(inst_id)
        if not ticker or not opening or not mark:
            continue
        try:
            price = _number(mark.get('markPx') or ticker.get('last'), '标记价')
            ct_val = _number(inst.get('ctVal'), '合约面值')
            turnover = _number(ticker.get('vol24h'), '24h 成交量') * ct_val * price
            try:
                oi_usd = _number(opening.get('oiCcy'), '持仓量') * price
            except strategy.StrategyRejected:
                oi_usd = 0
            if oi_usd <= 0:
                oi_usd = _number(opening.get('oi'), '持仓张数') * ct_val * price
            bid = _number(ticker.get('bidPx'), '买一')
            ask = _number(ticker.get('askPx'), '卖一')
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            spread_bps = (ask - bid) / ((ask + bid) / 2) * 10000
        except strategy.StrategyRejected:
            continue
        if turnover < MIN_TURNOVER_USD or oi_usd < MIN_OI_USD or spread_bps > MAX_SPREAD_BPS:
            continue
        ranked.append({'symbol': _symbol(inst_id), 'inst_id': inst_id,
                       'turnover': turnover, 'oi_usd': oi_usd,
                       'spread_bps': spread_bps, 'bucket': _bucket(_symbol(inst_id))})
    ranked.sort(key=lambda item: (-item['turnover'], item['symbol']))
    for rank, item in enumerate(ranked, 1):
        item['rank'] = rank
    return ranked[:KEEP_N]


def apply_hysteresis(old_symbols, ranked):
    by_symbol = {item['symbol']: item for item in ranked}
    selected, buckets = [], {}

    def add(symbol):
        bucket = _bucket(symbol)
        if len(selected) >= TOP_N or buckets.get(bucket, 0) >= BUCKET_CAP:
            return
        selected.append(symbol)
        buckets[bucket] = buckets.get(bucket, 0) + 1

    for symbol in old_symbols:
        if symbol in by_symbol and by_symbol[symbol]['rank'] <= KEEP_N:
            add(symbol)
    for item in ranked:
        if len(selected) >= TOP_N:
            break
        if item['rank'] <= TOP_N and item['symbol'] not in selected:
            add(item['symbol'])
    return selected


def _load_file():
    if not os.path.exists(DATA_PATH):
        return None
    try:
        with open(DATA_PATH, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def load_cached():
    return _load_file()


def refresh(now_ms=None, old=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    old = old or _load_file() or {}
    ranked = fetch_ranking(now_ms)
    selected = apply_hysteresis(old.get('symbols', []), ranked)
    if not selected:
        raise strategy.StrategyRejected('动态标的池为空')
    by_symbol = {item['symbol']: item for item in ranked}
    version = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime('u-%Y%m%dT%H%M')
    payload = {'version': version, 'generated_at_ms': now_ms,
               'symbols': selected,
               'members': {symbol: by_symbol[symbol]['rank'] for symbol in selected},
               'ranking': ranked}
    atomic_write_json(DATA_PATH, payload)
    return payload


def load_or_refresh(now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cached = _load_file()
    if cached and cached.get('symbols') \
       and 0 <= now_ms - int(cached.get('generated_at_ms', 0)) < REFRESH_MS:
        return cached
    try:
        return refresh(now_ms, cached)
    except Exception as exc:
        if cached and cached.get('symbols'):
            print(f"[universe] 刷新失败, 沿用旧池: {exc}")
            return cached
        raise
