"""OKX USDT perpetual pullback strategy; public data only, no order execution."""
import math
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import pandas as pd
import pandas_ta as ta
import requests

OKX_API = "https://www.okx.com"
MARKET = "OKX USDT 永续"
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


def okx_inst_id(symbol):
    """'BTC/USDT' -> 'BTC-USDT-SWAP'"""
    return f"{symbol.replace('/', '-')}-SWAP"


def public_get(path, params=None):
    """OKX v5 统一 {"code": "0", "data": [...]}; 非 0 即业务错误, data 恒为列表。
    单一 OKX 数据源, 失败不回退其他交易所(2026-10-21 与用户确认)。"""
    response = requests.get(f"{OKX_API}{path}", params=params, timeout=15)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") != "0" \
       or not isinstance(body.get("data"), list):
        raise StrategyRejected(f"OKX 行情接口返回错误 {body.get('code', '格式异常')}")
    return body["data"]


def quote(symbol, now_ms=None):
    inst = okx_inst_id(symbol)
    (ticker,) = public_get("/api/v5/market/ticker", {"instId": inst})
    (mark,) = public_get("/api/v5/public/mark-price", {"instId": inst, "instType": "SWAP"})
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    for item in (ticker, mark):
        if item.get("instId") != inst:
            raise StrategyRejected("行情币种不一致")
        timestamp = positive(item.get("ts"), "行情时间")
        if abs(now_ms - timestamp) > MAX_QUOTE_AGE_MS:
            raise StrategyRejected("永续行情过期或时钟偏差过大")
    funding = public_get("/api/v5/public/funding-rate", {"instId": inst})[0]
    # Missing funding is unknown, never an invented zero.
    try:
        rate = float(funding["fundingRate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyRejected("OKX 资金费率缺失") from exc
    if not math.isfinite(rate):
        raise StrategyRejected("资金费率不是有限数")
    bid, ask = positive(ticker.get("bidPx"), "买一"), positive(ticker.get("askPx"), "卖一")
    if ask < bid:
        raise StrategyRejected("买卖报价交叉")
    next_funding = positive(funding.get("nextFundingTime"), "下次资金结算时间")
    if next_funding <= now_ms:
        raise StrategyRejected("资金结算时间已过期")
    return {"price": positive(ticker.get("last"), "永续最新价"),
            "bid": bid, "ask": ask,
            "mark_price": positive(mark.get("markPx"), "标记价"),
            "funding_rate": rate, "next_funding_ms": next_funding,
            "quoted_at_ms": now_ms}


def closed_klines(symbol, interval, now_ms):
    """OKX v5 candles 行: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]。
    confirm=="1" 才是已收盘; 最新未收盘 bar 的 OHLC 仍在变化, 永不进信号。
    OKX 单次上限 300 根, 4H EMA200 需 750 根预热, 用 after 游标分页向前翻。"""
    hours, count = (4, 800) if interval == "4h" else (1, 300)
    bar = {"4h": "4H", "1h": "1H", "15m": "15m"}[interval]
    rows, after = [], ""
    while len(rows) < count:
        params = {"instId": okx_inst_id(symbol), "bar": bar,
                  "limit": min(300, count - len(rows))}
        if after:
            params["after"] = after
        batch = public_get("/api/v5/market/candles", params)
        if not batch:
            break
        # 每页倒序(最新在前): 先翻成本页升序, 再接到已收集的更旧数据前
        rows = batch[::-1] + rows
        after = batch[-1][0]
    if not rows:
        raise StrategyRejected("K 线格式不完整")
    df = pd.DataFrame([row[:6] for row in rows], columns=[
        "timestamp", "open", "high", "low", "close", "volume"])
    # confirm 是第 9 列字符串 "1"/"0"; astype(float) 前先抽出
    df["confirmed"] = [str(row[8]) == "1" for row in rows]
    try:
        df = df.astype(float)
    except (ValueError, TypeError) as exc:
        raise StrategyRejected("K 线含非数字") from exc
    df = df[df.confirmed == 1].reset_index(drop=True)
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
    # OKX timestamp 是 K 线开始时间; 收盘 = 开始 + 周期
    if not (df.timestamp.diff().iloc[1:] == duration).all():
        raise StrategyRejected("K 线重复、乱序或缺口")
    if not 0 < now_ms - (df.timestamp.iloc[-1] + duration) <= duration:
        raise StrategyRejected(f"{interval} K 线过期")
    df["close_time"] = df.timestamp + duration
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
    specs = public_get("/api/v5/public/instruments", {"instType": "SWAP",
                                                      "instId": okx_inst_id(symbol)})
    if not specs:
        raise StrategyRejected("品种不是正在交易的 USDT 永续")
    inst = specs[0]
    # linear U 本位: ctType=linear 且以 USDT 结算; state=live 才可交易
    if inst.get("state") != "live" or inst.get("ctType") != "linear" \
       or inst.get("settleCcy") != "USDT":
        raise StrategyRejected("品种不是正在交易的 USDT 永续")
    q = quote(symbol)
    # 结算周期按接口推导, 但防"跳过结算"特例: OKX 费率极低时会跳过某次结算,
    # 实测 BTC next-prev 可达 16h(2026-10-21 端到端验证发现)。若把 16h 当
    # 常规周期会低估结算次数和成本; 取观测值与 8h(OKX 常规上限)的较小者,
    # 宁可多预算一次结算(成本上界是保守方向)。
    funding = public_get("/api/v5/public/funding-rate", {"instId": okx_inst_id(symbol)})[0]
    interval_ms = positive(funding.get("nextFundingTime"), "下次结算") \
        - positive(funding.get("prevFundingTime"), "上次结算")
    interval_hours = min(interval_ms / HOUR_MS, 8)
    # 观测间隔 > 16h = 连跳两次以上, 属数据异常而非常规跳过
    if interval_hours <= 0 or interval_ms > 16 * HOUR_MS:
        raise StrategyRejected("资金结算周期与下次结算时间不一致")
    c4, c1 = df4.iloc[-1], df1.iloc[-1]
    # OKX 合约是张数模式: qty 单位是"张", 1 张 = ctVal 个币(BTC 1张=0.01 BTC)。
    # 最小名义金额无直接字段, 按 最小张数*面值*现价 推导。
    ctval = positive(inst.get("ctVal"), "合约面值")
    data = {"symbol": symbol, "market": MARKET, "ct_val": ctval, **q,
            "4h_ema20": float(c4.ema20), "4h_ema50": float(c4.ema50),
            "4h_ema200": float(c4.ema200), "4h_ema20_prev": float(df4.ema20.iloc[-4]),
            "4h_adx": float(c4.adx), "4h_di_plus": float(c4.di_plus),
            "4h_di_minus": float(c4.di_minus), "4h_close": float(c4.close),
            "1h_rsi": float(c1.rsi), "1h_atr": float(c1.atr),
            "1h_ema20": float(c1.ema20), "bars_1h": df1, "bars_4h": df4,
            "signal_close_ms": int(c1.close_time),
            "funding_interval_hours": interval_hours,
            "tick_size": positive(inst.get("tickSz"), "价格步长"),
            "qty_step": positive(inst.get("lotSz"), "数量步长"),
            "min_qty": positive(inst.get("minSz"), "最小数量"),
            "max_qty": positive(inst.get("maxMktSz"), "最大下单量"),
            "min_notional": positive(inst.get("minSz"), "最小张数") * ctval * q["price"]}
    if not all(math.isfinite(data[k]) for k in ["4h_ema20", "4h_ema50", "4h_ema200",
                    "4h_ema20_prev", "4h_adx", "4h_di_plus", "4h_di_minus", "1h_rsi", "1h_atr", "1h_ema20"]):
        raise StrategyRejected("指标不完整")
    return data


def signal_direction(data, now_ms=None):
    """Require a completed retest and reversal, rather than mere EMA proximity."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if data.get("market") != MARKET:
        raise StrategyRejected("策略需要 OKX USDT 永续行情")
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
    """OKX 张数语义: qty 单位是"张", 1 张 = ctVal 个币(lotSz 即每张面值,
    minSz 是最小张数)。风险/名义先按币数量算, 再除以 ctVal 得张数并按
    整数张向下取整; 损失/名义换回币单位输出。凑不足最小张数即放弃。"""
    equity = positive(equity, "账户权益")
    loss = positive(plan.get("loss_per_unit"), "每单位预计止损损失")
    entry = positive(plan.get("entry_max"), "入场上界")
    ct_val = positive(plan.get("ct_val"), "合约面值")
    base_qty = min(equity * ACCOUNT_RISK / loss, equity * MAX_NOTIONAL_EQUITY / entry,
                   positive(plan.get("max_qty"), "最大下单量") * ct_val)
    lots = round_tick(base_qty / ct_val, 1)  # 张数取整数张, lotSz 是面值不是步长
    base_lots = lots * ct_val  # 取整后的实际币数量
    if lots < positive(plan.get("min_qty"), "最小张数") \
       or base_lots * plan["entry_min"] < plan["min_notional"]:
        raise StrategyRejected("账户权益对应的风险额度不足最小下单要求；不可加仓凑单")
    return {"qty": lots, "base_qty": base_lots,
            "estimated_loss": base_lots * loss,
            "notional_max": base_lots * entry,
            "account_risk_pct": base_lots * loss / equity * 100}


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
        raise StrategyRejected("点位不符合 OKX 价格步长")
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
    plan = {"symbol": data["symbol"], "direction": direction, "entry": entry,
            "entry_min": lower, "entry_max": upper,
            "stop": stop, "tp1": targets[0], "tp2": targets[1],
            "structure_boundary": boundary, "hold_hours": HOLD_HOURS,
            "timeout_action": "人工复核剩余仓位，信号走弱则退出；不因横盘扩大止损",
            "signal_close_ms": data["signal_close_ms"], "created_at_ms": int(time.time()*1000),
            "market": MARKET, "stop_trigger": "标记价格(MARK_PRICE)",
            "account_risk_pct": ACCOUNT_RISK * 100,
            **{key: data[key] for key in ["qty_step", "min_qty", "max_qty", "min_notional", "ct_val"]}}
    plan.update(validate_plan(plan, data))
    return plan


def candidate_score(data):
    """候选择优打分, 跨币价可比(2026-10-20): 4H ADX 优先, 并列时取回踩
    更贴近 4H EMA20 的。贴近度按 1H ATR 归一 —— 绝对价差在 BTC 与 SOL
    之间不可比, 旧口径的 abs(price - ema20) 会让高价币永远输给低价币。"""
    atr = positive(data["1h_atr"], "ATR")
    ema = positive(data["4h_ema20"], "EMA20")
    price = positive(data["price"], "现价")
    return positive(data["4h_adx"], "ADX"), -abs(price - ema) / atr


def build_from_candidates(candidates):
    """按可比指标择优逐个构建点位计划; 首选被拒不阻断其余合格候选。
    返回 (plan, data, attempts); 全部不合格时 plan 为 None,
    attempts 收集每个候选的拒绝原因, 供日志与只读检查输出。"""
    attempts = []
    for data in sorted(candidates, key=candidate_score, reverse=True):
        try:
            return build_plan(data), data, attempts
        except StrategyRejected as exc:
            attempts.append((data["symbol"], str(exc)))
    return None, None, attempts


def ensure_pushable(plan, fresh):
    """推送前复核: 用推送时刻的新行情重新确认信号仍成立、现价仍在计划
    入场区间内。任一不满足即拒绝本轮推送 —— 计划失效就放弃, 不现场改点位。"""
    if signal_direction(fresh) != plan["direction"]:
        raise StrategyRejected("确认方向已改变，计划失效")
    price = positive(fresh["price"], "现价")
    if not plan["entry_min"] <= price <= plan["entry_max"]:
        raise StrategyRejected("现价已离开计划入场区间，本轮放弃推送")
    return fresh


def format_strategy_text(plan):
    """由代码把点位计划渲染成推送文案(观感与旧版一致); 点位全由规则生成,
    文案不再经过模型, 推送内容与存档点位不会出现两个口径。"""
    long_side = plan["direction"] == "long"
    d = "做多 🟢" if long_side else "做空 🔴"
    worst = plan["entry_max"] if long_side else plan["entry_min"]
    stop_pct = abs(worst - plan["stop"]) / worst * 100
    return (
        f"【方向】：{d}\n"
        f"【建议入场】：{plan['entry_min']:.2f} – {plan['entry_max']:.2f}"
        f"（现价即时入场，超出区间不追）\n"
        f"【硬止损位】：{plan['stop']:.2f}（最不利入场约亏 {stop_pct:.1f}%）\n"
        f"【分批止盈】：TP1 {plan['tp1']:.2f} / TP2 {plan['tp2']:.2f}"
        f"（TP1 到位可减仓保本）\n"
        f"【时间预期】：{plan['hold_hours']:.0f} 小时内应脱离成本区；"
        f"超时则{plan['timeout_action']}\n"
        f"【核心逻辑】：4H 趋势排列与动能一致，1H 回踩均线后出现同向收盘"
        f"反转确认；止损置于近端结构之外，目标按净盈亏比 2R/3R 预设，"
        f"前方已确认结构边界 {plan['structure_boundary']:.2f}。"
        f"单笔风险不超过账户权益的 {plan['account_risk_pct']:.1f}%。"
    )


def simplify_plan(plan):
    """追踪档案只存点位与预案; 完整文案存 text, 行情快照与中间计算量不落盘"""
    return {"direction": plan["direction"], "stop": plan["stop"],
            "tp1": plan["tp1"], "tp2": plan["tp2"],
            "hold_hours": plan["hold_hours"],
            "timeout_action": plan["timeout_action"],
            "entry_min": plan["entry_min"], "entry_max": plan["entry_max"],
            "net_rr": plan["net_rr"],
            "account_risk_pct": plan["account_risk_pct"],
            "text": format_strategy_text(plan)}
