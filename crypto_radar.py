import os
import time
import requests
import pandas as pd
import pandas_ta as ta
import tracker

# ================= 1. 核心配置区 =================
SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
# 通过环境变量注入(本地 .env 或 GitHub Actions Secrets), 勿在代码里写死密钥
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# 本机出口 IP 被币安主站按地域封锁(HTTP 451), K线改走币安官方公开行情镜像;
# 资金费率主站同样被封, 改用 OKX 同品种 U 本位永续的费率作参考(2026-10 确认可用)
BINANCE_MIRROR = "https://data-api.binance.vision"
OKX_API = "https://www.okx.com"

# ================= 2. 权威数据抓取与指标计算 =================
def fetch_klines(symbol, interval, limit):
    """从币安公开行情镜像拉取 K 线 (无需 API Key)"""
    pair = symbol.replace('/', '')
    res = requests.get(
        f"{BINANCE_MIRROR}/api/v3/klines",
        params={"symbol": pair, "interval": interval, "limit": limit},
        timeout=15,
    )
    res.raise_for_status()
    rows = res.json()
    # 币安 klines 实际返回 12 列, 只取前 6 列 (time/open/high/low/close/volume)
    df = pd.DataFrame([row[:6] for row in rows],
                      columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    return df.astype(float)

def fetch_funding_rate(symbol):
    """从 OKX 公开接口获取同品种 U 本位永续实时资金费率, 失败返回 0"""
    inst_id = symbol.replace('/USDT', '-USDT-SWAP')
    try:
        res = requests.get(
            f"{OKX_API}/api/v5/public/funding-rate",
            params={"instId": inst_id},
            timeout=10,
        ).json()
        return round(float(res['data'][0]['fundingRate']) * 100, 4)
    except Exception:
        return 0.0

def get_market_data(symbol):
    """拉取 4H(看趋势) 和 1H(找点位) 数据并精确计算指标"""
    df_4h = fetch_klines(symbol, '4h', 250)
    df_1h = fetch_klines(symbol, '1h', 250)

    # --- 4H 大周期趋势指标 ---
    df_4h['ema20'] = ta.ema(df_4h['close'], length=20)
    df_4h['ema50'] = ta.ema(df_4h['close'], length=50)
    df_4h['ema200'] = ta.ema(df_4h['close'], length=200)
    adx_4h = ta.adx(df_4h['high'], df_4h['low'], df_4h['close'], length=14)
    df_4h['adx'] = adx_4h['ADX_14']

    # --- 1H 小周期点位与波动率指标 ---
    df_1h['ema20'] = ta.ema(df_1h['close'], length=20)
    df_1h['rsi'] = ta.rsi(df_1h['close'], length=14)
    df_1h['atr'] = ta.atr(df_1h['high'], df_1h['low'], df_1h['close'], length=14)
    # 过去24小时高低点作为近期结构参考
    df_1h['recent_high'] = df_1h['high'].rolling(24).max()
    df_1h['recent_low'] = df_1h['low'].rolling(24).min()

    # --- 获取永续合约实时资金费率 (OKX) ---
    funding_rate = fetch_funding_rate(symbol)

    c4 = df_4h.iloc[-1]
    c1 = df_1h.iloc[-1]

    return {
        "symbol": symbol,
        "price": round(float(c1['close']), 2),
        "4h_ema20": round(float(c4['ema20']), 2),
        "4h_ema50": round(float(c4['ema50']), 2),
        "4h_ema200": round(float(c4['ema200']), 2),
        "4h_adx": round(float(c4['adx']), 1),
        "1h_rsi": round(float(c1['rsi']), 1),
        "1h_atr": round(float(c1['atr']), 2),
        "24h_high": round(float(c1['recent_high']), 2),
        "24h_low": round(float(c1['recent_low']), 2),
        "funding_rate_pct": funding_rate
    }

def get_macro_sentiment():
    """获取加密市场当日恐慌贪婪指数"""
    try:
        res = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5).json()
        val = res['data'][0]['value']
        status = res['data'][0]['value_classification']
        return f"{val} ({status})"
    except Exception:
        return "未知"

# ================= 3. 趋势与点位硬性初筛漏斗 =================
def check_signal(data):
    """
    判断当前是否有确定性方向与合适点位：
    - 若处于震荡期 (ADX < 22) 或价格悬空，返回 None
    - 只有大趋势确立 + 小周期回踩/反弹到位，才唤醒 AI
    """
    price = data['price']
    ema20_4h = data['4h_ema20']
    ema50_4h = data['4h_ema50']
    adx = data['4h_adx']
    rsi_1h = data['1h_rsi']

    # 1. 多头顺势回踩机会：4H多头排列 + ADX>22确认趋势 + 价格回踩接近4H EMA20(偏离度小于2%) 且 1H RSI未超买(<58)
    dist_to_ema20 = abs(price - ema20_4h) / ema20_4h
    if price > ema50_4h and ema20_4h > ema50_4h and adx >= 22:
        if dist_to_ema20 <= 0.02 and rsi_1h <= 58:
            return "顺势做多 (4H上升趋势确立 + 1H回踩关键均线支撑)"

    # 2. 空头顺势反弹机会：4H空头排列 + ADX>22确认趋势 + 价格反弹接近4H EMA20 且 1H RSI未超卖(>42)
    if price < ema50_4h and ema20_4h < ema50_4h and adx >= 22:
        if dist_to_ema20 <= 0.02 and rsi_1h >= 42:
            return "顺势做空 (4H下跌趋势确立 + 1H反弹触及关键均线阻力)"

    return None

# ================= 4. AI 极简策略生成与飞书推送 =================
def generate_ai_strategy(data, signal_type, fng):
    """调用 Gemini 原生 REST 接口生成直接、简练的执行策略"""
    prompt = f"""
你是一个实战派加密货币交易参谋。当前监控程序捕捉到一个确定性交易机会，请根据以下币安实时数据直接给出交易策略。
要求：极度精炼、拒绝废话，直接给点位和简短原因！

【实时数据】
- 币种：{data['symbol']} | 当前价：${data['price']}
- 触发类型：{signal_type}
- 4H 趋势指标：EMA20=${data['4h_ema20']}, EMA50=${data['4h_ema50']}, EMA200=${data['4h_ema200']}, ADX趋势强度={data['4h_adx']}
- 1H 点位指标：RSI={data['1h_rsi']}, ATR波动率=${data['1h_atr']}, 过去24h高低点=${data['24h_low']} - ${data['24h_high']}
- 合约资金费率：{data['funding_rate_pct']}% | 市场恐慌贪婪指数：{fng}

请严格按以下格式输出（不要多余寒暄）：
【方向】：做多 / 做空
【建议入场】：具体价格区间
【硬止损位】：具体价格（参考1.5倍ATR或结构位，注明亏损百分比）
【分批止盈】：TP1 价格 / TP2 价格（确保盈亏比大于 1:2）
【时间预期】：X 小时内应脱离成本区，若超时横盘怎么处理
【核心逻辑】：用 2 句话简明扼要说明制定该策略的原因（结合趋势、点位与资金费率）。
"""
    # Gemini 原生 REST 接口, 无需 SDK; 模型可经 GEMINI_MODEL 环境变量覆盖
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

def send_telegram_msg(symbol, price, signal_type, strategy_text):
    """推送 TG 消息 (HTML 排版, 规避 Markdown 转义坑)"""
    emoji = "🟢" if "做多" in signal_type else "🔴"
    text = (
        f"{emoji} <b>交易策略触发 | {symbol} (${price})</b>\n"
        f"<b>信号类型：</b>{signal_type}\n\n"
        f"{strategy_text}\n\n"
        f"数据来源：Binance 镜像 + OKX | {time.strftime('%Y-%m-%d %H:%M')}"
    )
    # TG 单条消息上限 4096 字符
    if len(text) > 4000:
        text = text[:4000] + "…"
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    resp.raise_for_status()

def open_signals(store):
    """返回所有未机械关闭(仍在追踪)的信号"""
    return [s for s in store['signals'] if s['status'] != 'closed']


def send_tracking_summary(store):
    """推送追踪中信号的表现摘要(每小时至多一条; 无未关闭信号则不推)"""
    open_sigs = open_signals(store)
    if not open_sigs:
        return
    lines = ["📊 <b>信号追踪摘要</b>"]
    for s in sorted(open_sigs, key=lambda x: x['triggered_at']):
        emoji = "🟢" if s['direction'] == 'long' else "🔴"
        trig = s['triggered_at'][:16]
        status_txt = {"active": "趋势成立", "invalid": "⚠️趋势已翻转"}[s['status']]
        lines.append(
            f"{emoji} {s['symbol']} {'做多' if s['direction'] == 'long' else '做空'}"
            f" @{s['entry_price']} ({trig})\n"
            f"现价 {s['last_price']} | 浮盈 {s['pnl_now']:+.2f}%"
            f" | 峰值 {s['mfe']:+.2f}% | 最差 {s['mae']:+.2f}% | {status_txt}"
        )
    requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": "\n\n".join(lines), "parse_mode": "HTML"},
        timeout=10,
    ).raise_for_status()


def run_scan():
    if not TG_BOT_TOKEN or not TG_CHAT_ID or not GEMINI_API_KEY:
        raise SystemExit("TG_BOT_TOKEN / TG_CHAT_ID / GEMINI_API_KEY 未设置")

    # 先更新历史信号档案
    store = tracker.load_signals()
    tracking_changed = tracker.update_open_signals(store, get_market_data)

    fng = get_macro_sentiment()
    new_pushed = False
    for sym in SYMBOLS:
        try:
            data = get_market_data(sym)
            signal = check_signal(data)
            if signal:
                sig = tracker.record_new_signal(data, signal)
                if sig is None:
                    # 同日同向信号已在追踪, 不重复推新策略, 只更新档案
                    print(f"[{sym}] 信号持续成立(已在追踪), 不重复推送")
                    continue
                print(f"[{sym}] 发现确定性机会: {signal}，正在生成策略并推送 TG...")
                strategy = generate_ai_strategy(data, signal, fng)
                send_telegram_msg(sym, data['price'], signal, strategy)
                new_pushed = True
            else:
                print(f"[{sym}] 当前价 ${data['price']} (4H ADX={data['4h_adx']}, 1H RSI={data['1h_rsi']}) -> 暂无高确定性趋势点位，继续静默监控。")
        except Exception as e:
            print(f"[{sym}] 扫描异常: {e}")

    # 每小时至多推一条追踪摘要: 仅当有新信号推过, 或本次是整点后首次巡检
    hour_mark = int(time.strftime('%M')) < 25  # cron 5 分档为整点后首跑
    if open_signals(store) and (new_pushed or hour_mark):
        try:
            send_tracking_summary(store)
        except Exception as e:
            print(f"追踪摘要推送失败: {e}")

    tracker.save_signals(store)

if __name__ == "__main__":
    run_scan()
