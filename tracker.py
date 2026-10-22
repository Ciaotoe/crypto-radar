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
      "exit_reason": null,                    # exited 时: stopped_out /
                                            #   breakeven_after_tp1 / took_profit
                                            #   / trend_reversed / timeout
      "pnl_now": 0.12,                        # 当前(或结案时)浮盈 % (有利为正)
      "mfe": 1.2,                             # 期间最大有利波动 %
      "mae": -0.5,                            # 期间最大不利波动 %
      "tp1_hit": false,                       # 命中后止损上移至入场价(保本)
      "last_price": 81790.0,
      "alerts_fired": [],                     # 已推过的预警类型(防重复刷屏)
      "seen_headlines": [],                   # AI 复核已审阅的新闻头条(增量去重)
      "last_ai_verdict": null                 # 上次 AI 复核结论(同结论不重复推)
    }
  ]
}

推送口径(2026-10-19 与用户确认; 2026-10-21 迁移 OKX 时移植保本闭环):
- 价格触及预设点位(止损/止盈)是交易所自动执行的旧闻, 只结案+简短通知, 不预警;
- 唯一紧急事件: 4H 排列翻转但止损尚未被触及 —— 需要人立刻决策离场;
- 预警(不结案): 浮盈大幅回吐 / ADX 动能消散, 同信号同原因只推一次;
- 所有触及判定用 15m K 线扫描上次检查以来的区间, 插针不漏(20 分钟粒度巡检
  本身不实时, 扫描窗口让"衡量口径"尽量准);
- TP1 命中后有效止损上移至入场价: 回撤打保本按 半仓TP1+半仓保本 结案
  (breakeven_after_tp1), 与真实分批止盈的期望收益一致。
"""
import json
import os
from datetime import datetime, timedelta, timezone

from runtime import atomic_write_json

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'signals.json')
ARCHIVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'signals_archive.json')
BJ_TZ = timezone(timedelta(hours=8))  # 统一用北京时间, 与用户感知一致
TRACK_HOURS = 48  # 信号追踪时长, 超时机械关闭
ARCHIVE_AFTER_DAYS = 90
FIFTEEN_MIN_MS = 15 * 60_000
TIMEOUT = 'timeout'
STOPPED_OUT = 'stopped_out'
BREAKEVEN_AFTER_TP1 = 'breakeven_after_tp1'
TOOK_PROFIT = 'took_profit'
TREND_REVERSED = 'trend_reversed'
MANUAL_RESET = 'manual_reset'
NOT_ENTERED = 'not_entered'
# 预警类型与默认阈值(环境变量可覆盖; 值是初版经验值, 按长期统计调整后可收紧)
ALERT_DRAWDOWN = 'drawdown'  # 浮盈大幅回吐
ALERT_ADX = 'adx'            # 趋势动能消散
ALERT_STALL = 'stall'        # 超时横盘: 超过策略时间预期仍未脱离成本区(只预警不结案, 2026-10-19 用户确认)
ALERT_FUNDING = 'funding'    # 资金费率异动: 逆势费率深值(拥挤风险), 交易所自动结算但需提示轧空/踩踏可能
DRAWDOWN_MIN_MFE = float(os.getenv('RADAR_DRAWDOWN_MIN_MFE', '1.0'))  # 峰值低于此不预警回吐
DRAWDOWN_KEEP_RATIO = float(os.getenv('RADAR_DRAWDOWN_KEEP_RATIO', '0.33'))  # 现浮盈/峰值低于此则预警
ADX_FLOOR = float(os.getenv('RADAR_ADX_FLOOR', '22'))  # 与 check_signal 初筛下限一致
STALL_COST_BAND = float(os.getenv('RADAR_STALL_COST_BAND', '0.5'))  # "仍在成本区"的浮盈带宽 %
FUNDING_SPIKE = float(os.getenv('RADAR_FUNDING_SPIKE', '0.1'))  # 逆势费率深值阈值 %


def _now():
    return datetime.now(BJ_TZ)


def _parse_dt(s):
    return datetime.fromisoformat(s)


def load_signals():
    if not os.path.exists(DATA_PATH):
        return {"signals": []}
    with open(DATA_PATH, encoding='utf-8') as f:
        store = json.load(f)
    store.setdefault('stats_epoch', None)
    # 旧数据迁移: invalid/closed -> exited, 状态机收敛为 active/exited 两态;
    # 一并补齐旧记录缺失的新字段。迁移只改内存副本, 由调用方 save 时落盘
    for sig in store.get('signals', []):
        if sig.get('status') in ('invalid', 'closed'):
            sig['status'] = 'exited'
            if not sig.get('exit_reason'):
                sig['exit_reason'] = TREND_REVERSED  # 旧档案已全部清仓重置, 兜底而已
        # 2026-10-19 新增字段: 旧信号无策略点位(strategy=None), 走机械追踪
        sig.setdefault('strategy', None)
        sig.setdefault('exit_reason', None)
        sig.setdefault('tp1_hit', False)
        sig.setdefault('alerts_fired', [])
        sig.setdefault('last_ai_review', None)  # AI 消息面复核节流(2026-10-19)
        sig.setdefault('seen_headlines', [])    # 复核增量头条去重(2026-10-21)
        sig.setdefault('last_ai_verdict', None)  # 同结论不重复推(2026-10-21)
        sig.setdefault('entry_model', 'immediate')
        if sig['entry_model'] == 'immediate':
            sig.setdefault('entry_filled_at', sig.get('triggered_at'))
        else:
            sig.setdefault('entry_filled_at', None)
        sig.setdefault('entry_window_min', 60)
        sig.setdefault('push_price', sig.get('entry_price'))
        sig.setdefault('entry_ambiguous', False)
        sig.setdefault('stats_eligible', sig.get('exit_reason') != MANUAL_RESET)
        sig.setdefault('pnl_net_pct', None)
        sig.setdefault('risk_pct', None)
        sig.setdefault('account_risk_pct', None)
        sig.setdefault('cost_pct_est', None)
        sig.setdefault('signal_close_ms', None)
        sig.setdefault('strategy_version', 'legacy')
        sig.setdefault('universe_version', None)
        sig.setdefault('pool_rank', None)
        if sig.get('exit_reason') == MANUAL_RESET:
            sig['stats_eligible'] = False
    return store


def save_signals(store):
    atomic_write_json(DATA_PATH, store)


def archive_closed_signals(store, now=None, days=ARCHIVE_AFTER_DAYS):
    now = now or _now()
    keep, archived = [], []
    for sig in store.get('signals', []):
        if sig.get('status') != 'exited':
            keep.append(sig)
            continue
        try:
            # 归档按结案时刻, 不按触发时刻; 长持信号刚结案不应立即被归档
            age = now - _parse_dt(sig.get('last_checked_at') or sig['triggered_at'])
        except (KeyError, TypeError, ValueError):
            keep.append(sig)
            continue
        (archived if age >= timedelta(days=days) else keep).append(sig)
    if not archived:
        return 0
    if os.path.exists(ARCHIVE_PATH):
        with open(ARCHIVE_PATH, encoding='utf-8') as handle:
            archive = json.load(handle)
    else:
        archive = {"signals": []}
    target = archive.setdefault('signals', [])
    known = {s.get('id') for s in target}
    target.extend(s for s in archived if s.get('id') not in known)
    atomic_write_json(ARCHIVE_PATH, archive)
    store['signals'] = keep
    return len(archived)


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


def open_signal_for_symbol(store, symbol):
    for s in store['signals']:
        if s['symbol'] == symbol and s['status'] == 'active':
            return s
    return None


def record_new_signal(store, data, signal_type, strategy=None, seen_headlines=None):
    """构造新信号(不追加不落盘, 由调用方在推送成功后 append)。
    同币种已有未结案信号时返回 None, 防止重复推送策略。"""
    direction = 'long' if '做多' in signal_type else 'short'
    if open_signal_same_side(store, data['symbol'], direction) \
       or open_signal_for_symbol(store, data['symbol']):
        return None
    now = _now()
    strategy = strategy or None
    close_ms = (strategy or {}).get('signal_close_ms', data.get('signal_close_ms'))
    if close_ms is not None:
        for old in store['signals']:
            if old.get('symbol') == data['symbol'] and old.get('direction') == direction \
               and old.get('signal_close_ms') == close_ms:
                try:
                    if now - _parse_dt(old['triggered_at']) < timedelta(hours=1):
                        return None
                except (KeyError, TypeError, ValueError):
                    pass
    entry_model = 'pending' if strategy else 'immediate'
    return {
        "id": make_signal_id(data['symbol'], direction, now),
        "symbol": data['symbol'],
        "direction": direction,
        "signal_type": signal_type,
        "entry_price": data['price'],
        # {"stop","tp1","tp2","hold_hours","timeout_action","text"} 或 None(机械追踪)
        "strategy": strategy,
        "entry_model": entry_model,
        "entry_window_min": (strategy or {}).get('entry_window_min', 60),
        "entry_filled_at": None if entry_model == 'pending' else now.isoformat(),
        "push_price": data['price'],
        "entry_ambiguous": False,
        "stats_eligible": True,
        "pnl_net_pct": None,
        "risk_pct": (strategy or {}).get('risk_pct'),
        "account_risk_pct": (strategy or {}).get('account_risk_pct'),
        "cost_pct_est": (strategy or {}).get('cost_pct_est'),
        "signal_close_ms": (strategy or {}).get('signal_close_ms',
                                                 data.get('signal_close_ms')),
        "strategy_version": (strategy or {}).get('strategy_version', 'legacy'),
        "universe_version": data.get('universe_version'),
        "pool_rank": data.get('pool_rank'),
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
        "last_ai_review": now.isoformat(),
        "seen_headlines": list(seen_headlines or [])[-20:],
        "last_ai_verdict": None,
    }


def _pnl_pct(direction, entry, price):
    """有利为正: 做空=入场-现价, 做多=现价-入场"""
    if entry <= 0:
        return 0.0
    diff = (entry - price) if direction == 'short' else (price - entry)
    return round(diff / entry * 100, 3)


def _trend_ok(direction, d):
    """仅以 4H EMA20/50 排列是否维持判趋势存续(2026-10-21 移植:
    盘中实时价刺破 EMA50 属正常洗盘, 交由硬止损防守, 不再误判翻转)。
    d 的 4h_ema20/50 由调用方保证来自已收盘 K 线。"""
    if direction == 'short':
        return d['4h_ema20'] < d['4h_ema50']
    return d['4h_ema20'] > d['4h_ema50']


def _check_exit(strategy, direction, hi, lo, entry_price=None, tp1_already_hit=False):
    """用扫描窗口高低点判定策略离场。同一窗口止损与止盈双触时保守按
    止损先触发(bar 内先后不可知)。TP1 已命中后有效止损上移至入场价
    (保本止损)。返回 exit_reason 或 None"""
    if not strategy:
        return None
    stop, tp2 = strategy.get('stop'), strategy.get('tp2')
    eff_stop = entry_price if (tp1_already_hit and entry_price is not None) else stop
    hit_stop = eff_stop is not None and (hi >= eff_stop if direction == 'short' else lo <= eff_stop)
    hit_tp2 = tp2 is not None and (lo <= tp2 if direction == 'short' else hi >= tp2)
    if hit_stop:
        return BREAKEVEN_AFTER_TP1 if tp1_already_hit else STOPPED_OUT
    if hit_tp2:
        return TOOK_PROFIT
    return None


def _tp1_hit(strategy, direction, hi, lo):
    if not strategy or strategy.get('tp1') is None:
        return False
    tp1 = strategy['tp1']
    return lo <= tp1 if direction == 'short' else hi >= tp1


def _check_alerts(sig, d, now):
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
    # 超时横盘: 超过策略自己写的时间预期仍未脱离成本区。
    # 策略文本的"时间预期"此前从未被执行(2026-10-19 用户指出), 现落地为预警
    strat = sig['strategy']
    if ALERT_STALL not in sig['alerts_fired'] and strat \
       and strat.get('hold_hours') \
       and now - _parse_dt(sig['triggered_at']) >= timedelta(hours=strat['hold_hours']) \
       and abs(sig['pnl_now']) < STALL_COST_BAND:
        fired.append(ALERT_STALL)
    # 资金费率异动: 逆势费率深值说明市场与该单方向高度拥挤, 有轧空/踩踏风险。
    # 费率符号: 空单的逆势方向是费率深负(空头付费拥挤), 多单反之
    if ALERT_FUNDING not in sig['alerts_fired'] \
       and d.get('funding_rate_pct') is not None \
       and ((sig['direction'] == 'short' and d['funding_rate_pct'] <= -FUNDING_SPIKE)
            or (sig['direction'] == 'long' and d['funding_rate_pct'] >= FUNDING_SPIKE)):
        fired.append(ALERT_FUNDING)
    return fired


def _scan_window(sig, fetch_klines, d):
    """15m K 线扫描上次检查以来的窗口, 覆盖巡检间隔内的插针。
    窗口起点不早于开仓时刻(2026-10-21 移植): 旧信号 last_checked_at 可能
    因历史原因早于 triggered_at, 开仓前的插针不属于本信号。
    扫描失败退化为现价(hi=lo=现价), 行为不劣于旧口径。"""
    hi, lo = d['price'], d['price']
    if fetch_klines is None:
        return hi, lo
    try:
        last_ms = int(_parse_dt(sig['last_checked_at']).timestamp() * 1000)
        filled = sig.get('entry_filled_at') or sig['triggered_at']
        start_ms = max(last_ms, int(_parse_dt(filled).timestamp() * 1000))
        df15 = fetch_klines(sig['symbol'], '15m', start_ms)
        if df15 is not None and not df15.empty:
            if 'confirmed' in df15.columns:
                df15 = df15[df15['confirmed'].astype(bool)]
            if not df15.empty:
                hi = max(hi, float(df15['high'].max()))
                lo = min(lo, float(df15['low'].min()))
    except Exception:
        pass
    return hi, lo


def _entry_deadline_ms(sig):
    triggered = int(_parse_dt(sig['triggered_at']).timestamp() * 1000)
    return triggered + int(sig.get('entry_window_min', 60)) * 60_000


def _fetch_bars(sig, fetch_klines, start_ms, end_ms):
    if fetch_klines is None:
        return None
    try:
        df = fetch_klines(sig['symbol'], '15m', max(0, start_ms - FIFTEEN_MIN_MS))
    except Exception:
        return None
    if df is None or df.empty:
        return df
    df = df.copy()
    if 'confirmed' in df.columns:
        df = df[df['confirmed'].astype(bool)]
    if df.empty:
        return df
    if 'close_time' not in df.columns:
        df['close_time'] = df['timestamp'] + FIFTEEN_MIN_MS
    df = df[(df['close_time'] > start_ms) & (df['close_time'] <= end_ms)]
    df = df[df['timestamp'] >= start_ms - 60_000]
    return df.reset_index(drop=True)


def _bar_hits_entry(sig, strategy, bar):
    if sig['direction'] == 'long':
        return float(bar['low']) <= strategy['entry_max']
    return float(bar['high']) >= strategy['entry_min']


def _bar_hits_stop(sig, strategy, bar):
    if sig['direction'] == 'long':
        return float(bar['low']) <= strategy['stop']
    return float(bar['high']) >= strategy['stop']


def _set_filled(sig, filled_ms, now):
    sig['entry_filled_at'] = datetime.fromtimestamp(filled_ms / 1000, BJ_TZ).isoformat()
    sig['entry_price'] = (sig['strategy']['entry_max'] if sig['direction'] == 'long'
                          else sig['strategy']['entry_min'])
    sig['last_checked_at'] = now.isoformat()


def _settle(sig, reason, now):
    strategy = sig.get('strategy') or {}
    if reason == STOPPED_OUT:
        gross = _pnl_pct(sig['direction'], sig['entry_price'], strategy['stop'])
    elif reason == TOOK_PROFIT:
        tp1_pnl = _pnl_pct(sig['direction'], sig['entry_price'], strategy['tp1'])
        tp2_pnl = _pnl_pct(sig['direction'], sig['entry_price'], strategy['tp2'])
        gross = round(0.5 * tp1_pnl + 0.5 * tp2_pnl, 3)
    elif reason == BREAKEVEN_AFTER_TP1:
        gross = round(0.5 * _pnl_pct(sig['direction'], sig['entry_price'],
                                     strategy['tp1']), 3)
    elif reason == NOT_ENTERED:
        gross = 0.0
        sig['stats_eligible'] = False
    else:
        gross = sig['pnl_now']
    sig['pnl_now'] = round(gross, 3)
    cost = sig.get('cost_pct_est')
    if reason == NOT_ENTERED:
        sig['pnl_net_pct'] = None
    elif cost is not None:
        sig['pnl_net_pct'] = round(gross - cost, 3)
    sig['status'] = 'exited'
    sig['exit_reason'] = reason
    sig['last_checked_at'] = now.isoformat()


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
    now_ms = int(now.timestamp() * 1000)
    for sig in store['signals']:
        if sig['status'] != 'active':
            continue
        try:
            d = get_market_data(sig['symbol'])
        except Exception:
            continue  # 单币种取数失败不阻断其他信号更新

        fill_bar = None
        # 条件挂单: 60 分钟窗口内 15m 触带才成交; 窗口内仍占用槽位
        if sig.get('entry_model', 'immediate') == 'pending' and not sig.get('entry_filled_at'):
            strategy = sig.get('strategy') or {}
            trigger_ms = int(_parse_dt(sig['triggered_at']).timestamp() * 1000)
            deadline = _entry_deadline_ms(sig)
            bars = _fetch_bars(sig, fetch_klines, trigger_ms, now_ms)
            fill_bar = None
            if bars is not None and not bars.empty:
                window = bars[bars['close_time'] <= deadline]
                for _, bar in window.iterrows():
                    if _bar_hits_entry(sig, strategy, bar):
                        fill_bar = bar
                        break
            if fill_bar is not None:
                fill_ms = int(fill_bar['close_time'])
                _set_filled(sig, fill_ms, now)
                overlap = (trigger_ms - float(fill_bar['timestamp'])) / 1000
                if overlap > 0:
                    sig['first_bar_overlap_sec'] = round(overlap, 1)
                sig['entry_ambiguous'] = bool(_bar_hits_stop(sig, strategy, fill_bar))
                changed = True
                if sig['entry_ambiguous']:
                    _settle(sig, STOPPED_OUT, now)
                    events.append({"sig": sig, "type": "exit", "reason": STOPPED_OUT})
                    continue
                # MFE/MAE 从成交后开始; 成交当根只用于判断同根止盈
                bars_after_fill = bars[bars['close_time'] > fill_ms]
            elif now_ms >= deadline:
                # 到期只认已收盘 15m 触带; 不拿现价补一笔"假成交"
                _settle(sig, NOT_ENTERED, now)
                sig['entry_cancel_reason'] = 'window_expired'
                changed = True
                events.append({"sig": sig, "type": "exit", "reason": NOT_ENTERED})
                continue
            else:
                if not _trend_ok(sig['direction'], d):
                    _settle(sig, NOT_ENTERED, now)
                    sig['entry_cancel_reason'] = TREND_REVERSED
                    changed = True
                    events.append({"sig": sig, "type": "exit", "reason": NOT_ENTERED})
                    continue
                sig['last_price'] = d['price']
                sig['last_checked_at'] = now.isoformat()
                changed = True
                continue
        else:
            bars_after_fill = None

        # 48h 机械关闭(已成交信号)
        age = now - _parse_dt(sig['triggered_at'])
        if age >= timedelta(hours=TRACK_HOURS):
            _settle(sig, TIMEOUT, now)
            changed = True
            events.append({"sig": sig, "type": "exit", "reason": TIMEOUT})
            continue

        if bars_after_fill is not None:
            hi, lo = d['price'], d['price']
            if not bars_after_fill.empty:
                hi = max(hi, float(bars_after_fill['high'].max()))
                lo = min(lo, float(bars_after_fill['low'].min()))
        else:
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

        # 1) 策略点位离场(止损被扫到 => 事已发生, 无需紧急预警)。
        #    TP1 已命中后止损上移保本, 回撤打保本按半仓TP1+半仓保本结算。
        #    成交当根不计入 MFE/MAE, 但同根触及止盈仍应结案。
        exit_hi, exit_lo = hi, lo
        if fill_bar is not None:
            exit_hi = max(exit_hi, float(fill_bar['high']))
            exit_lo = min(exit_lo, float(fill_bar['low']))
        exit_reason = _check_exit(sig['strategy'], sig['direction'], exit_hi, exit_lo,
                                  entry_price=sig['entry_price'],
                                  tp1_already_hit=bool(sig.get('tp1_hit')))
        if exit_reason:
            # 结案浮盈按触及价位算, 不用巡检现价(衡量口径)
            _settle(sig, exit_reason, now)
            changed = True
            events.append({"sig": sig, "type": "exit", "reason": exit_reason})
            continue

        # 2) TP1 仅记档: 策略文本已交代减仓, 触及时交易所自动执行, 不推送
        if not sig['tp1_hit'] and _tp1_hit(sig['strategy'], sig['direction'],
                                            exit_hi, exit_lo):
            sig['tp1_hit'] = True
            changed = True

        # 3) 趋势排列翻转: 有策略点位且止损未触 => 紧急(别等止损, 尽快离场);
        #    无点位存档(旧信号) => 机械结案
        if not _trend_ok(sig['direction'], d):
            sig['exit_reason'] = TREND_REVERSED
            _settle(sig, TREND_REVERSED, now)
            changed = True
            # 有点位说明止损还没被扫到(第 1 步已 return), 才算紧急
            urgent = bool(sig['strategy'])
            events.append({"sig": sig, "type": "exit", "reason": TREND_REVERSED,
                           "urgent": urgent})
            continue

        # 4) 预警(不结案)
        for alert in _check_alerts(sig, d, now):
            sig['alerts_fired'].append(alert)
            changed = True
            events.append({"sig": sig, "type": "alert", "reason": alert})
    return changed, events




