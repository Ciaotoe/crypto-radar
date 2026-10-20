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


def round_tick(price, tick, up=False):
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    units = (Decimal(str(price)) / Decimal(str(tick))).to_integral_value(rounding=rounding)
    return float(units * Decimal(str(tick)))


def structure_boundary(data, direction, entry):
    """Nearest observed obstacle, using confirmed pivots and the preceding 24h.

    A pivot needs two closed candles on either side; today's still forming high
    is never treated as a confirmed swing. Unknown space is not a target.
    """
    levels = []
    key = "high" if direction == "long" else "low"
    for frame in (data["bars_1h"].iloc[-168:], data["bars_4h"].iloc[-180:]):
        values = frame[key].tolist()
        for i in range(2, len(values) - 2):
            neighbors = values[i-2:i] + values[i+1:i+3]
            if (values[i] > max(neighbors) if direction == "long" else values[i] < min(neighbors)):
                levels.append(values[i])
    recent = data["bars_1h"].iloc[-25:-1][key]
    levels.append(float(recent.max() if direction == "long" else recent.min()))
    ahead = [x for x in levels if x > entry] if direction == "long" else [x for x in levels if x < entry]
    if not ahead:
        raise StrategyRejected("前方没有可确认的目标结构，暂不生成目标")
    return min(ahead) if direction == "long" else max(ahead)


def cost_per_unit(data, entry, exit_price, direction, hours=HOLD_HOURS):
    """Estimate both taker fees/slippage plus adverse funding, no funding credit.

    The latest funding rate can change. One additional settlement is reserved;
    this is a cost estimate, not a guaranteed limit on future funding or gaps.
    """
    interval = positive(data.get("funding_interval_hours"), "资金周期")
    rate = float(data["funding_rate"])
    if not math.isfinite(rate):
        raise StrategyRejected("费率不可用")
    adverse_rate = max(rate if direction == "long" else -rate, 0)
    settlements = math.ceil(hours / interval) + 1
    return (entry + exit_price) * (TAKER_FEE + SLIPPAGE) + entry * adverse_rate * settlements


def risk_size(plan, equity):
    equity = positive(equity, "账户权益")
    loss = positive(plan.get("loss_per_unit"), "每单位预计止损损失")
    entry = positive(plan.get("entry_max"), "入场上界")
    qty = min(equity * ACCOUNT_RISK / loss, equity * MAX_NOTIONAL_EQUITY / entry,
              positive(plan.get("max_qty"), "最大下单量"))
    qty = round_tick(qty, positive(plan.get("qty_step"), "数量步长"))
    if qty < plan["min_qty"] or qty * plan["entry_min"] < plan["min_notional"]:
        raise StrategyRejected("账户权益对应的风险额度不足最小下单要求；不可加仓凑单")
    return {"qty": qty, "estimated_loss": qty * loss,
            "notional_max": qty * entry, "account_risk_pct": qty * loss / equity * 100}


def validate_plan(plan, data):
    if plan.get("direction") not in ("long", "short"):
        raise StrategyRejected("方向必须明确为 long 或 short")
    fields = ["entry", "entry_min", "entry_max", "stop", "tp1", "tp2", "hold_hours", "structure_boundary"]
    values = {key: positive(plan.get(key), key) for key in fields}
    side = plan["direction"]
    low, high = values["entry_min"], values["entry_max"]
    if not low <= values["entry"] <= high or not 6 <= values["hold_hours"] <= 24:
        raise StrategyRejected("入场区间或持仓时间不合理")
    if side == "long":
        ordered = values["stop"] < low <= high < values["tp1"] < values["tp2"] < values["structure_boundary"]
        worst = high
        structure = float(data["bars_1h"].low.iloc[-4:].min())
        beyond_structure = values["stop"] <= structure - .25 * data["1h_atr"] + 1e-9
    else:
        ordered = values["stop"] > high >= low > values["tp1"] > values["tp2"] > values["structure_boundary"]
        worst = low
        structure = float(data["bars_1h"].high.iloc[-4:].max())
        beyond_structure = values["stop"] >= structure + .25 * data["1h_atr"] - 1e-9
    if not ordered or not beyond_structure:
        raise StrategyRejected("点位方向、结构外止损或目标空间不合格")
    risk = abs(worst - values["stop"])
    minrisk = min(abs(low-values["stop"]), abs(high-values["stop"]))
    atr = positive(data["1h_atr"], "ATR")
    if minrisk < 1.5 * atr - 1e-9 or risk > 3 * atr + 1e-9 or risk / worst > .03:
        raise StrategyRejected("止损须至少 1.5 ATR、至多 3 ATR，且不超过价格的 3%")
    loss = risk + cost_per_unit(data, worst, values["stop"], side, values["hold_hours"])
    reward = abs(values["tp1"]-worst) - cost_per_unit(data, worst, values["tp1"], side, values["hold_hours"])
    if reward / loss < MIN_NET_RR - 1e-9:
        raise StrategyRejected("最不利入场价扣成本后 TP1 盈亏比不足 2:1")
    actual_boundary = structure_boundary(data, side, worst)
    if values["structure_boundary"] != actual_boundary:
        raise StrategyRejected("目标结构与行情证据不一致")
    if any(abs(x - round_tick(x, data["tick_size"])) > 1e-8 for x in [values["stop"], values["tp1"], values["tp2"], low, high]):
        raise StrategyRejected("点位不符合 Binance 价格步长")
    return {"loss_per_unit": loss, "net_rr": reward / loss,
            "estimated_cost_per_unit": cost_per_unit(data, worst, values["stop"], side)}


def build_plan(data, direction=None):
    confirmed = signal_direction(data)
    if direction is not None and direction != confirmed:
        raise StrategyRejected("点位方向与确认信号不一致")
    direction = confirmed
    sign = 1 if direction == "long" else -1
    atr, tick = data["1h_atr"], data["tick_size"]
    entry = data["ask"] if direction == "long" else data["bid"]
    if data["ask"] - data["bid"] > .1 * atr or abs(data["mark_price"]-data["price"]) > .25 * atr:
        raise StrategyRejected("买卖价差或标记价偏离过大")
    # This is an immediate-entry setup, not an unconfirmed future limit order.
    lower = round_tick(entry - .1 * atr, tick, up=True)
    upper = round_tick(entry + .1 * atr, tick)
    if lower > upper or abs(entry-data["price"]) > .1 * atr:
        raise StrategyRejected("入场报价无有效区间")
    worst = upper if direction == "long" else lower
    structure = float(data["bars_1h"]["low" if direction == "long" else "high"].iloc[-4:].agg("min" if direction == "long" else "max"))
    stop = min(structure-.25*atr, lower-1.5*atr) if direction == "long" else max(structure+.25*atr, upper+1.5*atr)
    stop = round_tick(stop, tick, up=direction == "short")
    boundary = structure_boundary(data, direction, worst)
    risk = abs(worst-stop)
    if risk > 3 * atr + 1e-9 or risk / worst > .03:
        raise StrategyRejected("止损须不超过 3 ATR 及价格的 3%")
    loss = risk + cost_per_unit(data, worst, stop, direction)
    rate = max(data["funding_rate"] * sign, 0) * (math.ceil(HOLD_HOURS/data["funding_interval_hours"])+1)
    targets = []
    for rr in (2., 3.):
        # Solve net reward = rr * net stop loss, including price-dependent exit fee.
        desired = (rr*loss + worst*(2*(TAKER_FEE+SLIPPAGE)+rate)) / (1-sign*(TAKER_FEE+SLIPPAGE))
        targets.append(round_tick(worst + sign*desired, tick, up=direction == "long"))
    if sign * (boundary - targets[1]) < .25 * atr:
        raise StrategyRejected("已知支撑/阻力前的空间不足以容纳净 2R/3R 目标")
    plan = {"direction": direction, "entry": entry, "entry_min": lower, "entry_max": upper,
            "stop": stop, "tp1": targets[0], "tp2": targets[1],
            "structure_boundary": boundary, "hold_hours": HOLD_HOURS,
            "timeout_action": "人工复核剩余仓位，信号走弱则退出；不因横盘扩大止损",
            "signal_close_ms": data["signal_close_ms"], "created_at_ms": int(time.time()*1000),
            "market": MARKET, "stop_trigger": "CONTRACT_PRICE",
            "account_risk_pct": ACCOUNT_RISK * 100,
            **{key: data[key] for key in ["qty_step", "min_qty", "max_qty", "min_notional"]}}
    plan.update(validate_plan(plan, data))
    return plan
