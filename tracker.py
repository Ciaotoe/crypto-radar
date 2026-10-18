"""信号档案模块: 持久化历史信号并按机械口径追踪表现。

数据文件 data/signals.json 结构:
{
  "signals": [
    {
      "id": "BTC/USDT-short-2026-10-18",   # 币种+方向+北京时间日期
      "symbol": "BTC/USDT",
      "direction": "short",                  # short / long
      "signal_type": "顺势做空 (...)",
      "entry_price": 81676.42,                # 信号触发时实时价(参考入场价)
      "triggered_at": "2026-10-18T10:33:00+08:00",
      "last_checked_at": "2026-10-18T11:05:00+08:00",
      "status": "active",                     # active / invalid / closed
      "pnl_now": 0.12,                        # 当前浮盈 % (有利为正)
      "mfe": 1.2,                             # 期间最大有利波动 %
      "mae": -0.5,                            # 期间最大不利波动 %
      "last_price": 81790.0,
      "invalid_reason": null                  # 非 active 时的原因说明
    }
  ]
}
"""
import json
import os
from datetime import datetime, timedelta, timezone, date

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'signals.json')
BJ_TZ = timezone(timedelta(hours=8))  # 统一用北京时间, 与用户感知一致
TRACK_HOURS = 48  # 信号追踪时长, 超时机械关闭


def _now():
    return datetime.now(BJ_TZ)


def _bj_date_str():
    return _now().strftime('%Y-%m-%d')


def load_signals():
    if not os.path.exists(DATA_PATH):
        return {"signals": []}
    with open(DATA_PATH, encoding='utf-8') as f:
        return json.load(f)


def save_signals(store):
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def make_signal_id(symbol, direction):
    """同币种同方向同日(北京)视为同一信号, 防止持续成立时重复推送"""
    return f"{symbol}-{direction}-{_bj_date_str()}"


def find_signal(store, signal_id):
    for s in store['signals']:
        if s['id'] == signal_id:
            return s
    return None


def record_new_signal(data, signal_type):
    """新信号建档。若同 ID 信号当日已存在(含已 closed), 返回 None 表示不重复推。"""
    store = load_signals()
    direction = 'long' if '做多' in signal_type else 'short'
    sid = make_signal_id(data['symbol'], direction)
    if find_signal(store, sid):
        return None
    sig = {
        "id": sid,
        "symbol": data['symbol'],
        "direction": direction,
        "signal_type": signal_type,
        "entry_price": data['price'],
        "triggered_at": _now().isoformat(),
        "last_checked_at": _now().isoformat(),
        "status": "active",
        "pnl_now": 0.0,
        "mfe": 0.0,
        "mae": 0.0,
        "last_price": data['price'],
        "invalid_reason": None,
    }
    store['signals'].append(sig)
    save_signals(store)
    return sig


def _pnl_pct(direction, entry, price):
    """有利为正: 做空=入场-现价, 做多=现价-入场"""
    if entry <= 0:
        return 0.0
    diff = (entry - price) if direction == 'short' else (price - entry)
    return round(diff / entry * 100, 3)


def update_open_signals(store, get_market_data):
    """更新所有未关闭信号的表现与有效性, 返回是否有状态变化。

    get_market_data: crypto_radar.get_market_data, 注入便于测试。
    有效性口径(机械): 4H EMA20/50 排列方向与信号方向不再一致 => invalid。
    """
    changed = False
    now = _now()
    for sig in store['signals']:
        if sig['status'] == 'closed':
            continue
        # 48h 机械关闭
        age = now - datetime.fromisoformat(sig['triggered_at'])
        if age >= timedelta(hours=TRACK_HOURS):
            sig['status'] = 'closed'
            sig['invalid_reason'] = f"追踪满{TRACK_HOURS}h, 机械关闭"
            sig['last_checked_at'] = now.isoformat()
            changed = True
            continue
        try:
            d = get_market_data(sig['symbol'])
        except Exception:
            continue  # 单币种取数失败不阻断其他信号更新
        price = d['price']
        pnl = _pnl_pct(sig['direction'], sig['entry_price'], price)
        sig['pnl_now'] = pnl
        sig['mfe'] = max(sig['mfe'], pnl)
        sig['mae'] = min(sig['mae'], pnl)
        sig['last_price'] = price
        sig['last_checked_at'] = now.isoformat()
        # 有效性: 趋势排列方向是否仍与信号方向一致
        if sig['direction'] == 'short':
            trend_ok = price < d['4h_ema50'] and d['4h_ema20'] < d['4h_ema50']
            reason = "4H 空头排列已破坏"
        else:
            trend_ok = price > d['4h_ema50'] and d['4h_ema20'] > d['4h_ema50']
            reason = "4H 多头排列已破坏"
        if not trend_ok and sig['status'] == 'active':
            sig['status'] = 'invalid'
            sig['invalid_reason'] = reason
            changed = True
        changed = True
    return changed
