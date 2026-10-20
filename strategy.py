"""Binance USDT perpetual pullback strategy; public data only, no order execution."""
import math
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import pandas as pd
import pandas_ta as ta
import requests

FUTURES_API = "https://fapi.binance.com"
MARKET = "Binance USDT 永续"
HOUR_MS = 3_600_000
MAX_QUOTE_AGE_MS = 120_000
# These are conservative sizing assumptions, not the user's actual fee tier or
# calibrated profitable parameters. Replace only after cost/sample validation.
TAKER_FEE = 0.0005
SLIPPAGE = 0.0002
ACCOUNT_RISK = 0.005
MAX_NOTIONAL_EQUITY = 1.0
HOLD_HOURS = 24
MIN_NET_RR = 2.0


class StrategyRejected(ValueError):
    """An opportunity cannot be supported by the available data and rules."""


def positive(value, name):
    if isinstance(value, bool):
        raise StrategyRejected(f"{name} 不是有效数字")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyRejected(f"{name} 缺失或不是数字") from exc
    if not math.isfinite(value) or value <= 0:
        raise StrategyRejected(f"{name} 必须是有限正数")
    return value


def public_get(path, params=None):
    response = requests.get(f"{FUTURES_API}{path}", params=params, timeout=15)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict) and "code" in body:
        raise StrategyRejected(f"Binance 行情接口返回错误 {body['code']}")
    return body


def quote(symbol, now_ms=None):
    pair = symbol.replace("/", "")
    premium = public_get("/fapi/v1/premiumIndex", {"symbol": pair})
    book = public_get("/fapi/v1/ticker/bookTicker", {"symbol": pair})
    current = public_get("/fapi/v2/ticker/price", {"symbol": pair})
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    for item in (premium, book, current):
        if item.get("symbol") != pair:
            raise StrategyRejected("行情币种不一致")
        timestamp = positive(item.get("time"), "行情时间")
        if abs(now_ms - timestamp) > MAX_QUOTE_AGE_MS:
            raise StrategyRejected("永续行情过期或时钟偏差过大")
    # Missing funding is unknown, never an invented zero.
    try:
        funding = float(premium["lastFundingRate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyRejected("Binance 资金费率缺失") from exc
    if not math.isfinite(funding):
        raise StrategyRejected("资金费率不是有限数")
    bid, ask = positive(book.get("bidPrice"), "买一"), positive(book.get("askPrice"), "卖一")
    if ask < bid:
        raise StrategyRejected("买卖报价交叉")
    next_funding = positive(premium.get("nextFundingTime"), "下次资金结算时间")
    if next_funding <= now_ms:
        raise StrategyRejected("资金结算时间已过期")
    return {"price": positive(current.get("price"), "永续最新价"),
            "bid": bid, "ask": ask,
            "mark_price": positive(premium.get("markPrice"), "标记价"),
            "funding_rate": funding, "next_funding_ms": next_funding,
            "quoted_at_ms": now_ms}


def closed_klines(symbol, interval, now_ms):
    hours, count = (4, 800) if interval == "4h" else (1, 300)
    rows = public_get("/fapi/v1/klines", {"symbol": symbol.replace("/", ""),
                                       "interval": interval, "limit": count})
    if not isinstance(rows, list) or not rows or any(len(row) < 7 for row in rows):
        raise StrategyRejected("K 线格式不完整")
    df = pd.DataFrame([row[:7] for row in rows], columns=[
        "timestamp", "open", "high", "low", "close", "volume", "close_time"])
    try:
        df = df.astype(float)
    except (ValueError, TypeError) as exc:
        raise StrategyRejected("K 线含非数字") from exc
    # Binance includes the current candle; never use its changing OHLC in signals.
    df = df[df.close_time < now_ms].reset_index(drop=True)
    minimum = 750 if interval == "4h" else 250
    if len(df) < minimum:
        raise StrategyRejected(f"{interval} 已收盘样本不足，指标尚未充分预热")
    if not all(math.isfinite(x) for x in df.to_numpy().ravel()):
        raise StrategyRejected("K 线含非有限数")
    if (df[["open", "high", "low", "close"]] <= 0).any().any() \
       or (df.volume < 0).any() \
       or (df.high < df[["open", "close", "low"]].max(axis=1)).any() \
       or (df.low > df[["open", "close"]].min(axis=1)).any():
        raise StrategyRejected("K 线价格关系异常")
    duration = hours * HOUR_MS
    if not (df.timestamp.diff().iloc[1:] == duration).all() \
       or not (df.close_time == df.timestamp + duration - 1).all():
        raise StrategyRejected("K 线重复、乱序或缺口")
    if not 0 < now_ms - df.close_time.iloc[-1] <= duration:
        raise StrategyRejected(f"{interval} K 线过期")
    return df


def get_strategy_market_data(symbol):
    now_ms = int(time.time() * 1000)
    df4 = closed_klines(symbol, "4h", now_ms)
    df1 = closed_klines(symbol, "1h", now_ms)
    for length in (20, 50, 200):
        df4[f"ema{length}"] = ta.ema(df4.close, length=length, talib=False)
    adx = ta.adx(df4.high, df4.low, df4.close, length=14, talib=False)
    df4["adx"], df4["di_plus"], df4["di_minus"] = adx["ADX_14"], adx["DMP_14"], adx["DMN_14"]
    df1["ema20"] = ta.ema(df1.close, length=20, talib=False)
    df1["rsi"] = ta.rsi(df1.close, length=14, talib=False)
    df1["atr"] = ta.atr(df1.high, df1.low, df1.close, length=14, talib=False)
    exchange = public_get("/fapi/v1/exchangeInfo")
    instrument = next((x for x in exchange.get("symbols", [])
                       if x.get("symbol") == symbol.replace("/", "")), None)
    if not instrument or instrument.get("status") != "TRADING" \
       or instrument.get("contractType") != "PERPETUAL" \
       or instrument.get("quoteAsset") != "USDT" or instrument.get("marginAsset") != "USDT":
        raise StrategyRejected("品种不是正在交易的 USDT 永续")
    filters = {x["filterType"]: x for x in instrument["filters"]}
    funding_info = public_get("/fapi/v1/fundingInfo")
    if not isinstance(funding_info, list):
        raise StrategyRejected("资金结算周期数据不完整")
    # fundingInfo lists contracts with adjustments; unlisted contracts use 8h.
    adjusted = next((x for x in funding_info if x.get("symbol") == instrument["symbol"]), None)
    interval = positive(adjusted.get("fundingIntervalHours") if adjusted else 8, "资金周期")
    q = quote(symbol)
    if interval > 8 or q["next_funding_ms"] - q["quoted_at_ms"] > interval * HOUR_MS:
        raise StrategyRejected("资金结算周期与下次结算时间不一致")
    c4, c1 = df4.iloc[-1], df1.iloc[-1]
    data = {"symbol": symbol, "market": MARKET, **q,
            "4h_ema20": float(c4.ema20), "4h_ema50": float(c4.ema50),
            "4h_ema200": float(c4.ema200), "4h_ema20_prev": float(df4.ema20.iloc[-4]),
            "4h_adx": float(c4.adx), "4h_di_plus": float(c4.di_plus),
            "4h_di_minus": float(c4.di_minus), "4h_close": float(c4.close),
            "1h_rsi": float(c1.rsi), "1h_atr": float(c1.atr),
            "1h_ema20": float(c1.ema20), "bars_1h": df1, "bars_4h": df4,
            "signal_close_ms": int(c1.close_time), "funding_interval_hours": interval,
            "tick_size": positive(filters.get("PRICE_FILTER", {}).get("tickSize"), "价格步长"),
            "qty_step": positive(filters.get("MARKET_LOT_SIZE", {}).get("stepSize"), "数量步长"),
            "min_qty": positive(filters.get("MARKET_LOT_SIZE", {}).get("minQty"), "最小数量"),
            "max_qty": positive(filters.get("MARKET_LOT_SIZE", {}).get("maxQty"), "最大数量"),
            "min_notional": positive(filters.get("MIN_NOTIONAL", {}).get("notional"), "最小名义金额")}
    if not all(math.isfinite(data[k]) for k in ["4h_ema20", "4h_ema50", "4h_ema200",
                    "4h_ema20_prev", "4h_adx", "4h_di_plus", "4h_di_minus", "1h_rsi", "1h_atr", "1h_ema20"]):
        raise StrategyRejected("指标不完整")
    return data


def signal_direction(data, now_ms=None):
    """Require a completed retest and reversal, rather than mere EMA proximity."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if data.get("market") != MARKET:
        raise StrategyRejected("策略需要 Binance USDT 永续行情")
    if not 0 <= now_ms - positive(data.get("quoted_at_ms"), "报价时间") <= MAX_QUOTE_AGE_MS:
        raise StrategyRejected("策略报价已过期")
    if not 0 < now_ms - positive(data.get("signal_close_ms"), "确认时间") <= 45 * 60_000:
        raise StrategyRejected("1H 确认已超过 45 分钟，等待新确认")
    atr = positive(data.get("1h_atr"), "ATR")
    price = positive(data.get("price"), "现价")
    e20, e50, e200 = (positive(data.get(f"4h_ema{x}"), f"EMA{x}") for x in (20, 50, 200))
    if data["4h_adx"] < 22:
        raise StrategyRejected("4H ADX 低于趋势筛选阈值")
    if e20 > e50 > e200 and data["4h_ema20_prev"] < e20 \
       and data["4h_close"] > e20 and data["4h_di_plus"] > data["4h_di_minus"]:
        direction, sign = "long", 1
    elif e20 < e50 < e200 and data["4h_ema20_prev"] > e20 \
         and data["4h_close"] < e20 and data["4h_di_minus"] > data["4h_di_plus"]:
        direction, sign = "short", -1
    else:
        raise StrategyRejected("4H 均线、斜率或方向指标不一致")
    bars = data["bars_1h"]
    current, prev = bars.iloc[-1], bars.iloc[-2]
    if len(bars) < 5 or current.high <= current.low:
        raise StrategyRejected("回踩确认样本不足或无有效振幅")
    rsi = data["1h_rsi"]
    if direction == "long":
        rsi_ok = 40 <= rsi <= 65 and rsi > prev.rsi
        reversal = current.close > current.open and current.close > prev.high \
                   and (current.close - current.low) / (current.high - current.low) >= .65
    else:
        rsi_ok = 35 <= rsi <= 60 and rsi < prev.rsi
        reversal = current.close < current.open and current.close < prev.low \
                   and (current.high - current.close) / (current.high - current.low) >= .65
    if not rsi_ok or not reversal:
        raise StrategyRejected("1H 未出现方向一致的收盘反转确认")
    touched = False
    for _, bar in bars.iloc[-4:].iterrows():
        for ema in (bar.ema20, e20):
            if bar.low <= ema + .25 * atr and bar.high >= ema - .25 * atr:
                touched = True
    if not touched or sign * (current.close - data["1h_ema20"]) <= 0:
        raise StrategyRejected("没有均线回踩/反弹后收回的证据")
    if abs(price - data["1h_ema20"]) > 1.5 * atr or abs(price - e20) > 2 * atr \
       or abs(price - current.close) > .5 * atr \
       or sign * (price - data["1h_ema20"]) <= 0 \
       or sign * (price - prev.close) <= 0:
        raise StrategyRejected("现价偏离确认区间或重新失守，放弃追价")
    return direction
