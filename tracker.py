"""信号档案模块: 持久化历史信号, 盯策略点位追踪离场。

数据文件 data/signals.json 结构:
{
  "signals": [
    {
      "id": "BTC/USDT-short-2026-10-19T07:41",   # 币种+方向+触发时刻(北京)
      "symbol": "BTC/USDT",
      "direction": "short",                  # short / long
      "signal_type": "顺势做空 (...)",
      "entry_price": 81894.23,                # 信号触发时实时价(参考入场价)
      "strategy": {"stop": 83000, "tp1": 80500, "tp2": 79000, "text": "..."},
                                            # 无点位存档时为 null(旧信号/解析失败)
      "triggered_at": "2026-10-19T07:41:01+08:00",
      "last_checked_at": "2026-10-19T08:25:00+08:00",
      "status": "active",                     # active / exited
      "exit_reason": null,                    # exited 时: stopped_out / took_profit
                                            #   / trend_reversed / timeout
      "pnl_now": 0.12,                        # 当前(或结案时)浮盈 % (有利为正)
      "mfe": 1.2,                             # 期间最大有利波动 %
      "mae": -0.5,                            # 期间最大不利波动 %
      "tp1_hit": false,                       # 仅记档, 不推送不结案
      "last_price": 81790.0,
      "alerts_fired": []                      # 已推过的预警类型(防重复刷屏)
    }
  ]
}

推送口径(2026-10-19 与用户确认):
- 价格触及预设点位(止损/止盈)是交易所自动执行的旧闻, 只结案+简短通知, 不预警;
- 唯一紧急事件: 4H 排列翻转但止损尚未被触及 —— 需要人立刻决策离场;
- 预警(不结案): 浮盈大幅回吐 / ADX 动能消散, 同信号同原因只推一次;
- 所有触及判定用 15m K 线扫描上次检查以来的区间, 插针不漏(20 分钟粒度巡检
  本身不实时, 扫描窗口让"衡量口径"尽量准)。
"""
import json
import os
from datetime import datetime, timedelta, timezone

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'signals.json')
BJ_TZ = timezone(timedelta(hours=8))  # 统一用北京时间, 与用户感知一致
TRACK_HOURS = 48  # 信号追踪时长, 超时机械关闭
TIMEOUT = 'timeout'
STOPPED_OUT = 'stopped_out'
TOOK_PROFIT = 'took_profit'
TREND_REVERSED = 'trend_reversed'
# 预警类型与默认阈值(环境变量可覆盖; 值是初版经验值, 按长期统计调整后可收紧)
ALERT_DRAWDOWN = 'drawdown'  # 浮盈大幅回吐
ALERT_ADX = 'adx'            # 趋势动能消散
DRAWDOWN_MIN_MFE = float(os.getenv('RADAR_DRAWDOWN_MIN_MFE', '1.0'))  # 峰值低于此不预警回吐
DRAWDOWN_KEEP_RATIO = float(os.getenv('RADAR_DRAWDOWN_KEEP_RATIO', '0.33'))  # 现浮盈/峰值低于此则预警
ADX_FLOOR = float(os.getenv('RADAR_ADX_FLOOR', '22'))  # 与 check_signal 初筛下限一致


def _now():
    return datetime.now(BJ_TZ)


def _parse_dt(s):
    return datetime.fromisoformat(s)


def load_signals():
    if not os.path.exists(DATA_PATH):
        return {"signals": []}
    with open(DATA_PATH, encoding='utf-8') as f:
        store = json.load(f)
    # 旧数据迁移: invalid/closed -> exited, 状态机收敛为 active/exited 两态;
    # 一并补齐旧记录缺失的新字段。迁移只改内存副本, 由调用方 save 时落盘
    for sig in store.get('signals', []):
        if sig.get('status') in ('invalid', 'closed'):
            sig['status'] = 'exited'
            if not sig.get('exit_reason'):
                reason = sig.get('invalid_reason') or ''
                sig['exit_reason'] = TIMEOUT if '48' in reason else TREND_REVERSED
        # 2026-10-19 新增字段: 旧信号无策略点位(strategy=None), 走机械追踪
        sig.setdefault('strategy', None)
        sig.setdefault('exit_reason', None)
        sig.setdefault('tp1_hit', False)
        sig.setdefault('alerts_fired', [])
    return store


def save_signals(store):
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def make_signal_id(symbol, direction, now=None):
    """币种+方向+触发时刻(分钟)。旧 ID 按日期会在翻篇时把同一轮信号误判为
    新信号重复推送, 改按时刻后: 同一轮追踪内唯一, 结案后再次触发自然生成新 ID"""
    return f"{symbol}-{direction}-{(now or _now()).strftime('%Y-%m-%dT%H:%M')}"


def open_signal_same_side(store, symbol, direction):
    """同币种同方向是否已有未结案信号在追踪(有则不推新策略)"""
    for s in store['signals']:
        if s['symbol'] == symbol and s['direction'] == direction and s['status'] == 'active':
            return s
    return None


def record_new_signal(store, data, signal_type, strategy=None):
    """构造新信号(不追加不落盘, 由调用方在推送成功后 append)。
    同币种同方向已有未结案信号时返回 None, 防止重复推送策略。"""
    direction = 'long' if '做多' in signal_type else 'short'
    if open_signal_same_side(store, data['symbol'], direction):
        return None
    now = _now()
    return {
        "id": make_signal_id(data['symbol'], direction, now),
        "symbol": data['symbol'],
        "direction": direction,
        "signal_type": signal_type,
        "entry_price": data['price'],
        "strategy": strategy,  # {"stop","tp1","tp2","text"} 或 None(机械追踪)
        "triggered_at": now.isoformat(),
        "last_checked_at": now.isoformat(),
        "status": "active",
        "exit_reason": None,
        "pnl_now": 0.0,
        "mfe": 0.0,
        "mae": 0.0,
        "tp1_hit": False,
        "last_price": data['price'],
        "alerts_fired": [],
    }


def _pnl_pct(direction, entry, price):
    """有利为正: 做空=入场-现价, 做多=现价-入场"""
    if entry <= 0:
        return 0.0
    diff = (entry - price) if direction == 'short' else (price - entry)
    return round(diff / entry * 100, 3)


def _trend_ok(direction, d):
    """4H EMA20/50 排列方向与信号方向是否仍一致"""
    if direction == 'short':
        return d['price'] < d['4h_ema50'] and d['4h_ema20'] < d['4h_ema50']
    return d['price'] > d['4h_ema50'] and d['4h_ema20'] > d['4h_ema50']


def _check_exit(strategy, direction, hi, lo):
    """用扫描窗口高低点判定策略离场。同一窗口止损与止盈双触时保守按
    止损先触发(bar 内先后不可知)。返回 exit_reason 或 None"""
    if not strategy:
        return None
    stop, tp2 = strategy.get('stop'), strategy.get('tp2')
    hit_stop = stop is not None and (hi >= stop if direction == 'short' else lo <= stop)
    hit_tp2 = tp2 is not None and (lo <= tp2 if direction == 'short' else hi >= tp2)
    if hit_stop:
        return STOPPED_OUT
    if hit_tp2:
        return TOOK_PROFIT
    return None


def _tp1_hit(strategy, direction, hi, lo):
    if not strategy or strategy.get('tp1') is None:
        return False
    tp1 = strategy['tp1']
    return lo <= tp1 if direction == 'short' else hi >= tp1


def _check_alerts(sig, d):
    """预警判定(不结案)。返回本次新触发的预警类型列表; 同类型只推一次"""
    fired = []
    # 浮盈大幅回吐: 峰值够高且现浮盈已回落到峰值 1/3 以下
    if ALERT_DRAWDOWN not in sig['alerts_fired'] \
       and sig['mfe'] >= DRAWDOWN_MIN_MFE \
       and sig['pnl_now'] < sig['mfe'] * DRAWDOWN_KEEP_RATIO:
        fired.append(ALERT_DRAWDOWN)
    # 趋势动能消散
    if ALERT_ADX not in sig['alerts_fired'] and d['4h_adx'] < ADX_FLOOR:
        fired.append(ALERT_ADX)
    return fired


def _scan_window(sig, fetch_klines, d):
    """15m K 线扫描上次检查以来的窗口, 覆盖巡检间隔内的插针。
    扫描失败退化为现价(hi=lo=现价), 行为不劣于旧口径。"""
    hi, lo = d['price'], d['price']
    if fetch_klines is None:
        return hi, lo
    try:
        start_ms = int(_parse_dt(sig['last_checked_at']).timestamp() * 1000)
        df15 = fetch_klines(sig['symbol'], '15m', start_ms)
        hi = max(hi, float(df15['high'].max()))
        lo = min(lo, float(df15['low'].min()))
    except Exception:
        pass
    return hi, lo


def update_open_signals(store, get_market_data, fetch_klines=None):
    """更新所有未结案信号, 返回 (changed, events)。

    get_market_data / fetch_klines: 注入便于测试。
    events: [{"sig","type","reason",...}], type ∈ exit / alert, 供调用方推送。
    排列翻转与止损的优先级: 先判点位离场(止损被扫=事已发生, 按止损结案);
    排列翻转但止损未触 => urgent=True(唯一需人立刻决策的事件)。
    """
    changed = False
    events = []
    now = _now()
    for sig in store['signals']:
        if sig['status'] != 'active':
            continue
        # 48h 机械关闭
        age = now - _parse_dt(sig['triggered_at'])
        if age >= timedelta(hours=TRACK_HOURS):
            sig['status'] = 'exited'
            sig['exit_reason'] = TIMEOUT
            sig['last_checked_at'] = now.isoformat()
            changed = True
            events.append({"sig": sig, "type": "exit", "reason": TIMEOUT})
            continue
        try:
            d = get_market_data(sig['symbol'])
        except Exception:
            continue  # 单币种取数失败不阻断其他信号更新

        hi, lo = _scan_window(sig, fetch_klines, d)

        # 用窗口高低点累积 MFE/MAE(衡量口径尽量准)
        hi_pnl = _pnl_pct(sig['direction'], sig['entry_price'], lo)
        lo_pnl = _pnl_pct(sig['direction'], sig['entry_price'], hi)
        sig['mfe'] = round(max(sig['mfe'], hi_pnl, lo_pnl), 3)
        sig['mae'] = round(min(sig['mae'], hi_pnl, lo_pnl), 3)
        sig['last_price'] = d['price']
        sig['pnl_now'] = _pnl_pct(sig['direction'], sig['entry_price'], d['price'])
        sig['last_checked_at'] = now.isoformat()
        changed = True

        # 1) 策略点位离场(止损被扫到 => 事已发生, 无需紧急预警)
        exit_reason = _check_exit(sig['strategy'], sig['direction'], hi, lo)
        if exit_reason:
            # 结案浮盈按触及价位算, 不用巡检现价(衡量口径)
            mark = {STOPPED_OUT: 'stop', TOOK_PROFIT: 'tp2'}[exit_reason]
            sig['pnl_now'] = _pnl_pct(sig['direction'], sig['entry_price'],
                                      sig['strategy'][mark])
            sig['status'] = 'exited'
            sig['exit_reason'] = exit_reason
            changed = True
            events.append({"sig": sig, "type": "exit", "reason": exit_reason})
            continue

        # 2) TP1 仅记档: 策略文本已交代减仓, 触及时交易所自动执行, 不推送
        if not sig['tp1_hit'] and _tp1_hit(sig['strategy'], sig['direction'], hi, lo):
            sig['tp1_hit'] = True
            changed = True

        # 3) 趋势排列翻转: 有策略点位且止损未触 => 紧急(别等止损, 尽快离场);
        #    无点位存档(旧信号) => 机械结案
        if not _trend_ok(sig['direction'], d):
            sig['status'] = 'exited'
            sig['exit_reason'] = TREND_REVERSED
            changed = True
            # 有点位说明止损还没被扫到(第 1 步已 return), 才算紧急
            urgent = bool(sig['strategy'])
            events.append({"sig": sig, "type": "exit", "reason": TREND_REVERSED,
                           "urgent": urgent})
            continue

        # 4) 预警(不结案)
        for alert in _check_alerts(sig, d):
            sig['alerts_fired'].append(alert)
            changed = True
            events.append({"sig": sig, "type": "alert", "reason": alert})
    return changed, events




