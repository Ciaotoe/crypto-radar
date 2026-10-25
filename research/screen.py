"""Feature screening loop over the research corpus (offline, read-only).

Pipeline: family → vectorized event detection on 1H bars → exits via
backtest.simulate_walk (the SAME kernel production replay uses; no second copy
of exit math — round-4 lesson) → train/test split stats.

Discipline rules baked in:
  - Events are detected on CLOSED bars only: an event at bar i uses data up to
    and including bar i, entry simulation starts at i+1. No lookahead.
  - Train = bars closing before SPLIT_UTC, test = on/after. The test segment
    is read once per hypothesis; tuning on it invalidates the split.
  - Every expectancy number is net of ROUND_TRIP_COST_PCT (taker+slippage both
    legs, matching strategy.TAKER_FEE + strategy.SLIPPAGE = 0.07% * 2). Funding
    is unavailable historically and stays 0 — results are an upper bound.

Usage:
  uv run --no-sync python research/screen.py                 # all families
  uv run --no-sync python research/screen.py --family squeeze_breakout
  uv run --no-sync python research/screen.py --json > research/results/run.json
"""
import argparse
import datetime
import glob
import json
import math
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd
import pandas_ta as ta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest as B  # noqa: E402
import strategy as S  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
SPLIT_UTC = pd.Timestamp("2025-01-01", tz="UTC").value // 10**6  # ms
ROUND_TRIP_COST_PCT = 2 * (S.TAKER_FEE + S.SLIPPAGE) * 100  # 0.14%
# ATR below this fraction of price = dead tape (flat OHLC listing bars); such
# bars would produce risk_pct ~ 0 and R-multiples of ~1e11. See run_family.
MIN_ATR_FRAC = 1e-4

# Warmup: indicators need history before the first usable bar. EMA200 on the
# 4H resample needs ~800 1H bars to settle; families below cap at 1H-only
# indicators (rolling 100) plus a flat margin.
WARMUP_BARS = 200


# --------------------------------------------------------------------------
# Families: each takes the enriched frame and returns a boolean event mask
# (event fires on bar i's close) plus, per event, direction / stop / targets
# expressed as ATR multiples. Everything is vectorized; no per-bar Python.
# --------------------------------------------------------------------------

def family_squeeze_breakout(d, *, lookback=48, atr_mult=1.5, hold=24):
    """Volatility-compression breakout.

    Squeeze: rolling stdev of close returns at its lowest 20% of the past
    `lookback` bars (BB-width style compression). Event: the first bar AFTER
    a squeeze window closes beyond `atr_mult * ATR` from the range midpoint,
    direction = breakout side. Exit model: stop 1.5 ATR against, TP1 +2R,
    TP2 +3R, `hold` hours — mirrors the production plan geometry so results
    are comparable with backtest.py output.
    """
    ret = d.close.pct_change()
    vol = ret.rolling(20).std()
    # Rank of current vol within its trailing `lookback` window (0 = lowest).
    rank = vol.rolling(lookback).apply(lambda w: (w[:-1] > w[-1]).mean(), raw=True)
    squeezed_prev = rank.shift(1) < 0.2  # squeeze state on the PREVIOUS bar
    mid = (d.high.rolling(lookback).max() + d.low.rolling(lookback).min()) / 2
    band = atr_mult * d.atr
    up = squeezed_prev & (d.close > mid + band) & (d.close.shift(1) <= mid.shift(1) + band.shift(1))
    dn = squeezed_prev & (d.close < mid - band) & (d.close.shift(1) >= mid.shift(1) - band.shift(1))
    events = up | dn
    events &= d.index >= WARMUP_BARS + lookback
    direction = np.where(up, 1, -1)
    return events.values, {
        "direction": direction,
        "stop_atr": 1.5, "tp1_r": 2.0, "tp2_r": 3.0, "hold": hold,
    }


def family_vol_imbalance(d, *, lookback=100, z=2.0, hold=24):
    """Volume-price imbalance continuation.

    Event: close-direction move whose volume z-score (vs trailing `lookback`)
    exceeds `z` AND price closes in the top/bottom 25% of the bar range
    (conviction close, same spirit as production REVERSAL_CLOSE_POS but as
    continuation). Direction = close side. Same exit geometry as above.
    """
    vol_z = (d.vol - d.vol.rolling(lookback).mean()) / d.vol.rolling(lookback).std()
    rng = (d.high - d.low).replace(0, np.nan)
    pos = (d.close - d.low) / rng
    big_up = (vol_z > z) & (pos > 0.75) & (d.close > d.open)
    big_dn = (vol_z > z) & (pos < 0.25) & (d.close < d.open)
    events = (big_up | big_dn)
    events &= d.index >= WARMUP_BARS + lookback
    direction = np.where(big_up, 1, -1)
    return events.values, {
        "direction": direction,
        "stop_atr": 1.5, "tp1_r": 2.0, "tp2_r": 3.0, "hold": hold,
    }


def family_baseline(d, *, hold=24):
    """Unconditional control: long every bar. Any family must beat this."""
    events = np.zeros(len(d), dtype=bool)
    events[WARMUP_BARS:] = True
    direction = np.ones(len(d), dtype=int)
    return events, {"direction": direction,
                    "stop_atr": 1.5, "tp1_r": 2.0, "tp2_r": 3.0, "hold": hold}


FAMILIES = {
    "baseline": family_baseline,
    "squeeze_breakout": family_squeeze_breakout,
    "vol_imbalance": family_vol_imbalance,
}


# --------------------------------------------------------------------------
# Corpus loading & enrichment
# --------------------------------------------------------------------------

def load_corpus(data_dir=DATA_DIR):
    """Load every recorded snapshot; returns {symbol: enriched frame}."""
    frames = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "*.json.gz"))):
        loaded, _specs = B.load_snapshot(path)
        for symbol, value in loaded.items():
            d_1h = value[0] if isinstance(value, tuple) else value  # (d, h4)
            frames[symbol] = enrich(d_1h)
    return frames


def enrich(df):
    """1H indicators shared by all families (computed once per symbol)."""
    d = df.reset_index(drop=True)
    d["atr"] = ta.atr(d.high, d.low, d.close, length=14)
    return d


# --------------------------------------------------------------------------
# Event → trade → stats
# --------------------------------------------------------------------------

def run_family(frames, name, fn, cooldown_bars=24):
    """Screen one family across the corpus.

    cooldown_bars: after an event, suppress further events on the same symbol
    for this many bars (overlapping positions would fake sample independence).
    """
    trades = []
    for symbol, d in frames.items():
        events, params = fn(d)
        high, low, close = d.high.values, d.low.values, d.close.values
        atr = d.atr.values
        ts = d.ts.values
        direction = params["direction"]
        last_i = -10**9
        for i in np.flatnonzero(events):
            if i - last_i < cooldown_bars:
                continue
            last_i = i
            if not math.isfinite(atr[i]) or atr[i] <= 0 or i + 1 >= len(d):
                continue
            # Dead-tape guard: early-listing bars can be flat OHLC with 0 vol
            # (observed on ADA 2020-03), leaving ATR ~0 but > 0 after tick
            # rounding. risk_pct then collapses and R-multiples explode.
            if atr[i] / close[i] < MIN_ATR_FRAC:
                continue
            sign = int(direction[i])
            # Event is known at bar i's close → enter at bar i+1's OPEN.
            # worst=open[i+1] guarantees fill on the first bar (low<=open),
            # so no fill-ambiguity bias; no lookahead either.
            if i + 1 >= len(d):
                continue
            entry = float(d.open.values[i + 1])
            stop = entry - sign * params["stop_atr"] * atr[i]
            risk = abs(entry - stop)
            tp1 = entry + sign * risk * params["tp1_r"]
            tp2 = entry + sign * risk * params["tp2_r"]
            out = B.simulate_walk(high, low, close, i, sign, entry, stop,
                                  tp1, tp2, hold_bars=params["hold"],
                                  fill_bars=1)
            if out["outcome"] == "not_entered":
                continue
            trades.append({"symbol": symbol, "ts": int(ts[i]), "sign": sign,
                           "pnl_pct": out["pnl_pct"] - ROUND_TRIP_COST_PCT,
                           "risk_pct": risk / entry * 100,
                           "outcome": out["outcome"]})
    return trades


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


def segment_stats(trades, lo_ms=None, hi_ms=None):
    sel = [t for t in trades
           if (lo_ms is None or t["ts"] >= lo_ms) and (hi_ms is None or t["ts"] < hi_ms)]
    rs = [t["pnl_pct"] / t["risk_pct"] for t in sel if t["risk_pct"] > 0]
    if not rs:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    n, k = len(rs), len(wins)
    wr = k / n
    lo, hi = wilson(k, n)
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / max(1, n - 1))
    tval = mean / (sd / math.sqrt(n)) if sd > 0 else float("inf")
    return {"n": n, "win_rate": wr, "wilson95": [lo, hi],
            "expectancy_r": mean, "t": tval, "total_r": sum(rs),
            "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
            "outcomes": dict(Counter(t["outcome"] for t in sel))}


def report(frames, families, as_json=False):
    out = {}
    for name in families:
        trades = run_family(frames, name, FAMILIES[name])
        out[name] = {
            "all": segment_stats(trades),
            "train": segment_stats(trades, hi_ms=SPLIT_UTC),
            "test": segment_stats(trades, lo_ms=SPLIT_UTC),
        }
    if as_json:
        return out
    lines = []
    lines.append(f"语料: {len(frames)} 币种; 成本 -{ROUND_TRIP_COST_PCT:.2f}%/笔; "
                 f"train < {pd.Timestamp(SPLIT_UTC, unit='ms')} UTC <= test")
    lines.append("已知近似: funding=0(上界), 1H 收盘判定, 幸存者偏差(按今日流动性选币)")
    for name, seg in out.items():
        lines.append(f"\n== {name} ==")
        for label in ("train", "test", "all"):
            s = seg[label]
            if s["n"] == 0:
                lines.append(f"  {label:5s} n=0")
                continue
            pf = (f"{s['profit_factor']:.2f}"
                  if s["profit_factor"] is not None else "inf")
            lines.append(
                f"  {label:5s} n={s['n']:5d} winR={s['win_rate']:.3f} "
                f"[{s['wilson95'][0]:.3f},{s['wilson95'][1]:.3f}] "
                f"expR={s['expectancy_r']:+.3f} t={s['t']:+.2f} PF={pf} "
                f"totalR={s['total_r']:+.0f}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="corpus feature screening (read-only)")
    parser.add_argument("--family", action="append",
                        help=f"repeatable; default all of {list(FAMILIES)}")
    parser.add_argument("--data-dir", default=DATA_DIR)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    families = args.family or list(FAMILIES)
    unknown = [f for f in families if f not in FAMILIES]
    if unknown:
        raise SystemExit(f"未知 family: {unknown}")
    frames = load_corpus(args.data_dir)
    if not frames:
        raise SystemExit(f"语料为空: {args.data_dir} (先跑 research/fetch_corpus.py)")
    result = report(frames, families, as_json=args.json)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        os.makedirs(RESULTS_DIR, exist_ok=True)
        path = os.path.join(RESULTS_DIR, f"screen_{pd.Timestamp.now(datetime.UTC):%Y%m%d_%H%M%S}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
        print(f"saved -> {path}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()
