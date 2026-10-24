"""Replay the production signal stack over historical OKX 1H bars (read-only).

Answers two questions with real data instead of hand-planted fixtures:
  1. the funnel — which gate kills how many candidate setups
  2. forward expectancy of the survivors (n / winR / expR / PF / MFE / MAE)

Design rule: this file calls strategy.signal_direction / strategy.build_plan
directly, on a data dict synthesized from the historical bar. It does NOT
re-implement gate or level math — a second copy of the rules is exactly how
the "tests green, production silent" defect hid for days.

Known approximations, stated rather than papered over:
  - The 15m trigger gate is STUBBED to always confirm, because OKX
    history-candles holds only ~22h of 15m depth. Every count here is
    therefore an UPPER BOUND on production signal volume and expectancy.
  - Historical funding rates are unavailable per-bar; funding is set to 0.
    Production costs are higher than measured here.
  - Fill and exit are decided on closed 1H bars, not ticks. Intra-bar order
    is unknowable, so stop-before-TP within one bar resolves conservatively
    (stop first). Pending fills use the same worst-entry band as production.
  - Spread/mark-divergence checks see a synthesized tight quote (0.02 tick),
    so they never reject in replay; production can reject on wide spreads.

Usage:
  uv run --no-sync python backtest.py --symbols BTC/USDT,ETH/USDT --days 120
  uv run --no-sync python backtest.py --symbols BTC/USDT --days 120 --json
  uv run --no-sync python backtest.py --snapshot tests/fixtures/doge_1h.json.gz
  uv run --no-sync python backtest.py --symbols DOGE/USDT --days 170 --record OUT.json.gz
"""
import argparse
import gzip
import json
import math
import sys
import time
from collections import Counter, deque

import pandas as pd
import pandas_ta as ta
import requests

import strategy as S

HOUR_MS = 3_600_000
_CALLS = deque()

# Production raises one Chinese message per gate. Mapping them to stable keys
# keeps the funnel comparable across runs and across code changes.
# Order matters: most specific first.
GATE_KEYS = [
    ("4H ADX 低于", "G1_adx"),
    ("4H 均线、斜率或方向指标不一致", "G2_4h_alignment"),
    ("回踩确认样本不足或无有效振幅", "G3_no_range"),
    ("1H 未出现方向一致的收盘反转确认", "G4_rsi_or_reversal"),
    ("没有均线回踩/反弹后收回的证据", "G5_touch"),
    ("现价偏离确认区间或重新失守", "G6_deviation"),
    ("1H 确认已超过", "G0_confirm_age"),
    ("策略报价已过期", "G0_quote_age"),
    ("15m 触发样本不足", "G7_15m_sample"),
    ("15m 未出现同向收盘触发确认", "G7_15m_trigger"),
    ("买卖价差或标记价偏离过大", "P0_spread_or_mark"),
    ("入场报价无有效区间", "P0_entry_band"),
    ("止损须不超过", "P1_stop_too_wide"),
    ("止损须至少", "P1_stop_too_narrow"),
    ("显著结构前的空间不足以容纳净 2R", "P3_tp1_beyond_cap"),
    ("可达空间不足以在 TP1 之外再设 TP2", "P3_no_room_for_tp2"),
    ("目标超出显著结构前的可达空间", "P3_cap_mismatch"),
    ("目标结构与行情证据不一致", "P3_boundary_mismatch"),
    ("最不利入场价扣成本后 TP1 盈亏比不足", "P4_net_rr"),
    ("点位方向、结构外止损或目标空间不合格", "P5_ordering"),
    ("点位不符合 OKX 价格步长", "P6_tick"),
    ("点位方向与确认信号不一致", "P0_direction"),
    ("K 线", "DATA_klines"),
    ("行情", "DATA_quote"),
]


def gate_key(message):
    for needle, key in GATE_KEYS:
        if needle in message:
            return key
    return "OTHER"


class _FrozenTime:
    """Delegate everything to the real time module except time()."""

    def __init__(self, real, ms):
        self._real = real
        self.ms = ms

    def time(self):
        return self.ms / 1000

    def __getattr__(self, name):
        return getattr(self._real, name)


def _throttle(limit=18, window=2.0):
    now = time.monotonic()
    while _CALLS and now - _CALLS[0] >= window:
        _CALLS.popleft()
    if len(_CALLS) >= limit:
        time.sleep(window - (now - _CALLS[0]))
        now = time.monotonic()
        while _CALLS and now - _CALLS[0] >= window:
            _CALLS.popleft()
    _CALLS.append(time.monotonic())


def _get(path, params, retries=4):
    for attempt in range(retries):
        _throttle()
        try:
            response = requests.get(f"{S.OKX_API}{path}", params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
            if body.get("code") == "0" and isinstance(body.get("data"), list):
                return body["data"]
        except requests.RequestException:
            pass
        time.sleep(0.4 * 2 ** attempt)
    return None


def fetch_1h(symbol, count):
    """Closed 1H bars, ascending, deduplicated, via history-candles."""
    rows, after = [], ""
    while len(rows) < count:
        params = {"instId": S.okx_inst_id(symbol), "bar": "1H",
                  "limit": min(100, count - len(rows))}
        if after:
            params["after"] = after
        batch = _get("/api/v5/market/history-candles", params)
        if not batch:
            break
        rows = batch[::-1] + rows
        after = batch[-1][0]
        if len(batch) < 100:
            break
    return _rows_to_frame(rows)


def _rows_to_frame(rows):
    seen, out = set(), []
    for row in rows:
        try:
            ts = int(float(row[0]))
            values = [float(row[1]), float(row[2]), float(row[3]), float(row[4])]
        except (TypeError, ValueError, IndexError):
            continue
        if ts in seen or any(not math.isfinite(v) for v in values):
            continue
        seen.add(ts)
        vol = 0.0
        try:
            vol = float(row[5])
        except (TypeError, ValueError, IndexError):
            pass
        confirmed = len(row) < 9 or str(row[8]) == "1"
        if not confirmed:
            continue
        out.append([ts, values[0], values[1], values[2], values[3], vol])
    out.sort(key=lambda x: x[0])
    return pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "vol"])


_SPECS = {}


def fetch_specs(symbol):
    """Real contract specs, so tick rounding in replay matches production."""
    if symbol in _SPECS:
        return _SPECS[symbol]
    rows = _get("/api/v5/public/instruments", {"instType": "SWAP",
                                               "instId": S.okx_inst_id(symbol)})
    specs = None
    if rows:
        row = rows[0]
        try:
            specs = {"tick_size": float(row["tickSz"]), "qty_step": float(row["lotSz"]),
                     "min_qty": float(row["minSz"]), "max_qty": float(row["maxMktSz"]),
                     "ct_val": float(row["ctVal"])}
        except (KeyError, TypeError, ValueError):
            specs = None
    if specs is None:
        # Offline/failed lookup: neutral specs keep the replay runnable.
        # Rounding is then less faithful — flagged in the output.
        specs = {"tick_size": 0.01, "qty_step": 0.01, "min_qty": 0.01,
                 "max_qty": 1e9, "ct_val": 0.01, "approximated": True}
    _SPECS[symbol] = specs
    return specs


def prepare(df):
    """1H indicators plus 4H-resampled indicators with production semantics."""
    d = df.copy().reset_index(drop=True)
    d["ema20"] = ta.ema(d.close, length=20)
    d["rsi"] = ta.rsi(d.close, length=14)
    d["atr"] = ta.atr(d.high, d.low, d.close, length=14)
    d["bucket"] = d.ts // (4 * HOUR_MS)
    grouped = d.groupby("bucket")
    h4 = pd.DataFrame({"ts": grouped.ts.max(), "open": grouped.open.first(),
                       "high": grouped.high.max(), "low": grouped.low.min(),
                       "close": grouped.close.last()}).reset_index(drop=True)
    h4 = h4[h4.ts < d.ts.iloc[-1]]  # only fully-closed 4H bars
    for n in (20, 50, 200):
        h4[f"ema{n}"] = ta.ema(h4.close, length=n)
    adx = ta.adx(h4.high, h4.low, h4.close, length=14)
    h4["adx"], h4["dip"], h4["dim"] = adx["ADX_14"], adx["DMP_14"], adx["DMN_14"]
    h4["ema20_prev"] = h4.ema20.shift(3)  # production: ema20.iloc[-4] vs iloc[-1]
    # At the 1H bar's close (ts + 1h) the last fully closed 4H bar is in scope.
    h4["close_time"] = h4.ts + 4 * HOUR_MS
    d["h4i"] = h4.close_time.searchsorted((d.ts + HOUR_MS).values, side="right") - 1
    return d, h4


def stub_15m(direction, price):
    """15m frame that always confirms `direction`.

    The real 15m gate cannot be replayed (history depth ~22h), so it is stubbed
    open. This is what makes every count in this tool an upper bound.
    """
    rows = []
    base = price
    for _ in range(30):
        rows.append([base, base + 0.1, base - 0.1, base, base])
    last_open = price * (0.999 if direction == "long" else 1.001)
    last_close = price * (1.001 if direction == "long" else 0.999)
    rows[-1] = [last_open, max(last_open, last_close) * 1.001,
                min(last_open, last_close) * 0.999, last_close, last_close * 0.999]
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close", "ema20"])
    frame["ema20"] = price * (0.9995 if direction == "long" else 1.0005)
    return frame


def replay_data(d, h4, i, symbol, specs):
    """Synthesize the get_strategy_market_data dict for bar i."""
    j = int(d.h4i.iloc[i])
    if j < 210:
        return None
    h = h4.iloc[j]
    cur = d.iloc[i]
    if not all(math.isfinite(x) for x in (h.ema20, h.ema50, h.ema200, h.adx, h.dip,
                                          h.dim, h.ema20_prev, cur.ema20, cur.rsi,
                                          cur.atr)):
        return None
    if cur.atr <= 0:
        return None
    price = float(cur.close)
    close_ms = int(cur.ts) + HOUR_MS
    tick = specs["tick_size"]
    half = max(tick, price * 1e-6)
    return {
        "symbol": symbol, "market": S.MARKET, "ct_val": specs["ct_val"],
        "price": price, "bid": price - half, "ask": price + half,
        "mark_price": price, "funding_rate": 0.0,
        "next_funding_ms": close_ms + 8 * HOUR_MS,
        "prev_funding_ms": close_ms, "funding_interval_hours": 8.0,
        "quoted_at_ms": close_ms, "signal_close_ms": close_ms,
        "4h_ema20": float(h.ema20), "4h_ema50": float(h.ema50),
        "4h_ema200": float(h.ema200), "4h_ema20_prev": float(h.ema20_prev),
        "4h_adx": float(h.adx), "4h_di_plus": float(h.dip),
        "4h_di_minus": float(h.dim), "4h_close": float(h.close),
        "1h_rsi": float(cur.rsi), "1h_atr": float(cur.atr),
        "1h_ema20": float(cur.ema20),
        "bars_1h": d.iloc[max(0, i - 300):i + 1].reset_index(drop=True),
        "bars_4h": h4.iloc[max(0, j - 260):j + 1].reset_index(drop=True),
        "15m_ema20": price, "15m_close": price, "15m_open": price,
        "tick_size": tick, "qty_step": specs["qty_step"],
        "min_qty": specs["min_qty"], "max_qty": specs["max_qty"],
        "min_notional": specs["min_qty"] * specs["ct_val"] * price,
    }


def evaluate_bar(d, h4, i, symbol, specs):
    """Run production signal_direction + build_plan at bar i.

    Returns (plan, None) on success, (None, gate_key) on rejection,
    (None, None) when the bar lacks indicator history.
    """
    data = replay_data(d, h4, i, symbol, specs)
    if data is None:
        return None, None
    # Production scans just after the boundary (systemd sleeps to :00+15s), and
    # signal_direction requires 0 < now - signal_close_ms <= 45min strictly.
    # Scanning exactly AT the close would make every historical bar fail on age.
    now_ms = int(d.ts.iloc[i]) + HOUR_MS + 60_000
    data["quoted_at_ms"] = now_ms
    saved = S.time
    S.time = _FrozenTime(saved, now_ms)
    try:
        # 15m is stubbed open, so the direction must be read first to build it.
        try:
            direction = S.signal_direction(data, now_ms)
        except S.StrategyRejected as exc:
            return None, gate_key(str(exc))
        data["bars_15m"] = stub_15m(direction, float(d.close.iloc[i]))
        plan = S.build_plan(data, direction)
        return plan, None
    except S.StrategyRejected as exc:
        return None, gate_key(str(exc))
    except Exception:
        return None, "OTHER_error"
    finally:
        S.time = saved


def simulate(d, i, plan, hold_bars=24, fill_bars=4):
    """Forward-simulate a plan on closed 1H bars; stop-first tie-break."""
    sign = 1 if plan["direction"] == "long" else -1
    worst = plan["entry_max"] if sign > 0 else plan["entry_min"]
    stop, tp1, tp2 = plan["stop"], plan["tp1"], plan["tp2"]
    filled = False
    for k in range(i + 1, min(len(d), i + 1 + fill_bars)):
        bar = d.iloc[k]
        if (bar.low <= worst if sign > 0 else bar.high >= worst):
            filled = True
            break
    if not filled:
        return {"outcome": "not_entered", "pnl_pct": None, "mfe": None, "mae": None}
    tp1_hit = False
    mfe = mae = 0.0
    for k in range(i + 1, min(len(d), i + 1 + hold_bars)):
        bar = d.iloc[k]
        hi, lo = float(bar.high), float(bar.low)
        fav = sign * (hi - worst) / worst * 100
        unfav = sign * (lo - worst) / worst * 100
        mfe, mae = max(mfe, fav), min(mae, unfav)
        if not tp1_hit and (hi >= tp1 if sign > 0 else lo <= tp1):
            tp1_hit = True
        eff_stop = worst if tp1_hit else stop
        if (lo <= eff_stop if sign > 0 else hi >= eff_stop):
            if tp1_hit:
                return {"outcome": "breakeven_after_tp1", "mfe": mfe, "mae": mae,
                        "pnl_pct": 0.5 * abs(tp1 - worst) / worst * 100}
            return {"outcome": "stopped_out", "mfe": mfe, "mae": mae,
                    "pnl_pct": -abs(worst - stop) / worst * 100}
        if (hi >= tp2 if sign > 0 else lo <= tp2):
            return {"outcome": "took_profit", "mfe": mfe, "mae": mae,
                    "pnl_pct": 0.5 * (abs(tp1 - worst) + abs(tp2 - worst)) / worst * 100}
    exitp = float(d.iloc[min(len(d) - 1, i + hold_bars)].close)
    return {"outcome": "timeout", "mfe": mfe, "mae": mae,
            "pnl_pct": sign * (exitp - worst) / worst * 100}


def stats(events):
    filled = [e for e in events if e["outcome"] != "not_entered"]
    counts = Counter(e["outcome"] for e in events)
    rs, mfes, maes = [], [], []
    net_pnl = 0.0
    for e in filled:
        if e["pnl_pct"] is None or not e["risk_pct"]:
            continue
        rs.append(e["pnl_pct"] / e["risk_pct"])
        mfes.append(e["mfe"])
        maes.append(e["mae"])
        net_pnl += e["pnl_pct"]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_loss = abs(sum(losses))
    return {
        "signals": len(events), "filled": len(filled),
        "not_entered": counts["not_entered"], "stopped_out": counts["stopped_out"],
        "took_profit": counts["took_profit"],
        "breakeven_after_tp1": counts["breakeven_after_tp1"],
        "timeout": counts["timeout"],
        "win_rate": len(wins) / len(rs) if rs else None,
        "expectancy_r": sum(rs) / len(rs) if rs else None,
        "total_r": sum(rs), "net_pnl_pct": net_pnl,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "avg_mfe_pct": sum(mfes) / len(mfes) if mfes else None,
        "avg_mae_pct": sum(maes) / len(maes) if maes else None,
    }


APPROXIMATIONS = [
    "15m trigger gate stubbed open (~22h history depth): counts are an UPPER BOUND",
    "historical funding unavailable; funding cost = 0, real costs are higher",
    "fills/exits on closed 1H bars, stop-first tie-break, not tick data",
    "spread/mark checks see a synthesized tight quote and never reject",
]


def run_frames(frames, specs_by_symbol, hold_hours=24):
    funnel = Counter()
    events = []
    bars_scanned = 0
    for symbol, (d, h4) in frames.items():
        specs = specs_by_symbol[symbol]
        for i in range(26, len(d) - 1):
            bars_scanned += 1
            plan, key = evaluate_bar(d, h4, i, symbol, specs)
            if plan is None:
                if key:
                    funnel[key] += 1
                continue
            funnel["PASSED"] += 1
            outcome = simulate(d, i, plan, hold_bars=int(hold_hours))
            events.append({"symbol": symbol, "ts": int(d.ts.iloc[i]),
                           "direction": plan["direction"],
                           "risk_pct": abs(plan["entry_max"] - plan["stop"])
                                       / plan["entry_max"] * 100
                           if plan["direction"] == "long"
                           else abs(plan["entry_min"] - plan["stop"])
                                / plan["entry_min"] * 100,
                           "structure_boundary": plan.get("structure_boundary"),
                           **outcome})
    return {"frames": frames, "funnel": funnel, "events": events,
            "bars_scanned": bars_scanned, "symbols": list(frames)}


def run(symbols, days, hold_hours=24):
    frames, specs_by_symbol = {}, {}
    for symbol in symbols:
        df = fetch_1h(symbol, days * 24 + 50)
        if len(df) < 900:
            print(f"[{symbol}] 历史样本不足({len(df)}), 跳过", file=sys.stderr)
            continue
        frames[symbol] = prepare(df)
        specs_by_symbol[symbol] = fetch_specs(symbol)
        print(f"[{symbol}] {len(df)} 根 1H bar", file=sys.stderr)
    return run_frames(frames, specs_by_symbol, hold_hours)


def load_snapshot(path):
    """Offline replay from a recorded snapshot (see --record)."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    bars = payload["bars"]
    df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "vol"])
    symbol = payload["symbol"]
    return {symbol: prepare(df)}, {symbol: payload.get("specs") or fetch_specs(symbol)}


def record_snapshot(path, symbol, days):
    df = fetch_1h(symbol, days * 24 + 50)
    if len(df) < 900:
        raise SystemExit(f"{symbol}: 只有 {len(df)} 根 1H bar, 不足以录制快照")
    payload = {"symbol": symbol, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                              time.gmtime()),
               "days": days, "specs": fetch_specs(symbol),
               "bars": [[int(r.ts), r.open, r.high, r.low, r.close, r.vol]
                        for r in df.itertuples()]}
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    print(f"recorded {len(df)} bars -> {path}")


def render(results):
    print(f"回放范围: {len(results['symbols'])} 币种 × "
          f"{results['bars_scanned']} 根已收盘 1H bar")
    print("已知近似(结果偏乐观):")
    for note in APPROXIMATIONS:
        print(f"  - {note}")
    print("\n漏斗(生产判定栈逐级淘汰):")
    for key, count in sorted(results["funnel"].items(), key=lambda x: -x[1]):
        print(f"  {key:24s} {count}")
    print()
    s = stats(results["events"])
    print(f"通过全部门槛: {s['signals']}")
    print(f"成交 {s['filled']}, 未成交 {s['not_entered']}, 止损 {s['stopped_out']}, "
          f"止盈 {s['took_profit']}, 保本 {s['breakeven_after_tp1']}, 超时 {s['timeout']}")
    if s["expectancy_r"] is not None:
        print(f"胜率 {s['win_rate']:.1%}, 单笔期望 {s['expectancy_r']:+.3f}R, "
              f"累计 {s['total_r']:+.2f}R, PF {s['profit_factor']:.2f}")
        print(f"MFE/MAE 均值 {s['avg_mfe_pct']:+.2f}% / {s['avg_mae_pct']:+.2f}%")
    else:
        print("无成交样本, 无法计算期望")


def main(argv=None):
    parser = argparse.ArgumentParser(description="OKX 1H replay backtest (read-only)")
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--hold-hours", type=int, default=24)
    parser.add_argument("--snapshot", help="replay a recorded snapshot, no network")
    parser.add_argument("--record", help="record a snapshot to this path and exit")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    if args.record:
        if len(symbols) != 1:
            raise SystemExit("--record 需要且仅需要一个 --symbols")
        record_snapshot(args.record, symbols[0], args.days)
        return
    if args.snapshot:
        frames, specs = load_snapshot(args.snapshot)
        results = run_frames(frames, specs, args.hold_hours)
    else:
        results = run(symbols, args.days, args.hold_hours)
    if args.json:
        s = stats(results["events"])
        print(json.dumps({"symbols": results["symbols"],
                          "bars_scanned": results["bars_scanned"],
                          "funnel": dict(results["funnel"]),
                          "approximations": APPROXIMATIONS,
                          **{k: v for k, v in s.items()}},
                         ensure_ascii=False, indent=2))
    else:
        render(results)


if __name__ == "__main__":
    main()
