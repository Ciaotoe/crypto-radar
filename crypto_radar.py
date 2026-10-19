import os
import time
import json
from datetime import datetime, timedelta
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

def fetch_klines_since(symbol, interval, start_ms):
    """从币安镜像拉取 start_ms(毫秒时间戳)之后的 K 线, 供追踪模块扫描
    上次检查以来的插针。窗口上限按 24h(Actions 故障重启后兜底)。"""
    pair = symbol.replace('/', '')
    res = requests.get(
        f"{BINANCE_MIRROR}/api/v3/klines",
        params={"symbol": pair, "interval": interval,
                "startTime": start_ms, "limit": 96},
        timeout=15,
    )
    res.raise_for_status()
    rows = res.json()
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
    """调用 Gemini 生成执行策略。要求严格 JSON 输出, 供追踪模块存档点位;
    解析或点位校验失败时返回 (纯文本, None), 信号走无点位机械追踪。"""
    prompt = f"""
你是一个实战派加密货币交易参谋。当前监控程序捕捉到一个确定性交易机会，请根据以下币安实时数据直接给出交易策略。
要求：极度精炼、拒绝废话，直接给点位和简短原因！

【实时数据】
- 币种：{data['symbol']} | 当前价：${data['price']}
- 触发类型：{signal_type}
- 4H 趋势指标：EMA20=${data['4h_ema20']}, EMA50=${data['4h_ema50']}, EMA200=${data['4h_ema200']}, ADX趋势强度={data['4h_adx']}
- 1H 点位指标：RSI={data['1h_rsi']}, ATR波动率=${data['1h_atr']}, 过去24h高低点=${data['24h_low']} - ${data['24h_high']}
- 合约资金费率：{data['funding_rate_pct']}% | 市场恐慌贪婪指数：{fng}

请严格按以下 JSON 格式输出（不要输出 JSON 以外的任何文字，不要用 markdown 代码块包裹）：
{{
  "direction": "做空",
  "entry": "建议入场价格区间",
  "stop": 硬止损位数字（参考1.5倍ATR或结构位，须能注明亏损百分比）,
  "tp1": 第一止盈位数字（确保盈亏比大于 1:2）,
  "tp2": 第二止盈位数字,
  "hold_hours": 数字，预计几小时内应脱离成本区（一般 6-24）,
  "timeout_action": "若超时横盘怎么处理"（一句话，超时预警时会原文转述）,
  "logic": "用 2 句话简明扼要说明制定该策略的原因（结合趋势、点位与资金费率）"
}}
"""
    # Gemini 原生 REST 接口, 无需 SDK; 模型可经 GEMINI_MODEL 环境变量覆盖
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    resp.raise_for_status()
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    return parse_strategy(raw, data, signal_type)


def parse_strategy(raw, data, signal_type):
    """解析 Gemini JSON 输出。返回 (推送文案, 策略点位dict或None)。
    点位校验: 方向与触发信号一致, stop/tp1/tp2 均为正数且方向关系正确
    (做空: stop>现价>tp1>tp2; 做多: stop<现价<tp1<tp2), 不满足则视为无点位。"""
    try:
        # 容错: 模型偶尔会用 ```json 包裹
        body = raw.strip('`')
        if body.startswith('json'):
            body = body[4:]
        obj = json.loads(body)
        direction = 'long' if '做多' in obj['direction'] else 'short'
        stop, tp1, tp2 = (float(obj[k]) for k in ('stop', 'tp1', 'tp2'))
        price = data['price']
        expect_long = '做多' in signal_type
        if direction == ('long' if expect_long else 'short') and \
           ((direction == 'short' and stop > price > tp1 > tp2) or
            (direction == 'long' and stop < price < tp1 < tp2)):
            # hold_hours/timeout_action 可选: 缺失不挡建档, 只是超时横盘预警无据可依
            strategy = {"stop": stop, "tp1": tp1, "tp2": tp2,
                        "hold_hours": float(obj['hold_hours']) if obj.get('hold_hours') else None,
                        "timeout_action": obj.get('timeout_action') or '',
                        "text": raw}
            text = format_strategy_text(obj, data)
            return text, strategy
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
        # Actions 日志里可见失败原因, 便于调整 prompt/校验规则
        print(f"[parse_strategy] JSON 解析/校验失败: {type(e).__name__}: {e}")
        print(f"[parse_strategy] 原始返回前500字: {raw[:500]}")
    # 退化为纯文本: 无点位档案, 该信号走机械追踪(无法判定止损/止盈离场)
    return raw, None


def format_strategy_text(obj, data):
    """由代码把 JSON 渲染成与旧版观感一致的推送文案"""
    d = "做多 🟢" if obj['direction'] == '做多' else "做空 🔴"
    stop_loss_pct = abs(obj['stop'] - data['price']) / data['price'] * 100
    timeout_txt = f"{obj['hold_hours']:.0f} 小时内应脱离成本区"
    if obj.get('timeout_action'):
        timeout_txt += f"；超时则{obj['timeout_action']}"
    return (
        f"【方向】：{d}\n"
        f"【建议入场】：{obj['entry']}\n"
        f"【硬止损位】：{obj['stop']:.2f}（约亏 {stop_loss_pct:.1f}%）\n"
        f"【分批止盈】：TP1 {obj['tp1']:.2f} / TP2 {obj['tp2']:.2f}（TP1 到位可减仓保本）\n"
        f"【时间预期】：{timeout_txt}\n"
        f"【核心逻辑】：{obj['logic']}"
    )

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

def fetch_news_headlines():
    """拉加密市场新闻头条(CoinDesk RSS, 公开无 key), 供 AI 复核消息面。
    失败返回空列表 —— 复核在没有新闻时仍可基于费率/FNG 判断。"""
    try:
        res = requests.get("https://www.coindesk.com/arc/outboundfeeds/rss/",
                           timeout=10)
        res.raise_for_status()
        items = res.text.split('<item>')[1:6]  # 最近 5 条够了
        heads = []
        for it in items:
            t = it.split('<title>')[1].split('</title>')[0]
            heads.append(t.replace('<![CDATA[', '').replace(']]>', '').strip())
        return heads
    except Exception:
        return []


# AI 复核间隔(小时): 消息面变化通常数小时内兑现到价格, 2h 在轧空等快速
# 行情里能抢在排列翻转前提醒(2026-10-19 与用户确认 4h 太久)。可经环境变量调整
AI_REVIEW_HOURS = float(os.getenv("RADAR_AI_REVIEW_HOURS", "2"))
_sig_last_review = {}  # {signal_id: iso时间} 复核节流, 不落盘(丢一次无碍)


def review_signals_with_ai(store, events):
    """在持信号每 AI_REVIEW_HOURS 做一次消息面复核(真正看新闻的那一层)。
    机械规则覆盖不到的阴跌/消息面异动, 由大模型基于头条+费率+FNG 判断;
    只产出 alert 事件, 结案权仍在机械层。API 调用有节流, 无单时不调用。"""
    now = datetime.now(tracker.BJ_TZ)
    to_review = []
    for s in open_signals(store):
        last = _sig_last_review.get(s['id'])
        if last is None or now - datetime.fromisoformat(last) >= timedelta(hours=AI_REVIEW_HOURS):
            to_review.append(s)
    if not to_review:
        return
    heads = fetch_news_headlines()
    fng = get_macro_sentiment()
    for s in to_review:
        _sig_last_review[s['id']] = now.isoformat()
        try:
            d = get_market_data(s['symbol'])
            verdict = ask_ai_review(s, d, heads, fng)
            if verdict:
                s['alerts_fired'].append('ai_review')
                events.append({"sig": s, "type": "alert", "reason": "ai_review",
                               "detail": verdict})
        except Exception as e:
            print(f"[{s['symbol']}] AI 复核异常: {e}")


def ask_ai_review(sig, d, heads, fng):
    """单信号 AI 复核: 返回异常描述(触发预警)或 None(正常持有)。"""
    news = "\n".join(f"- {h}" for h in heads) or "(暂无可用头条)"
    prompt = f"""你是加密货币持仓风控参谋。我在{'做空' if sig['direction']=='short' else '做多'} {sig['symbol']}，入场价 {sig['entry_price']}，
当前价 {d['price']}，浮盈 {sig['pnl_now']:+.2f}%（期间峰值 {sig['mfe']:+.2f}%）。
4H ADX={d['4h_adx']}，资金费率 {d['funding_rate_pct']}%，恐慌贪婪指数 {fng}。

近期市场头条：
{news}

只回答一个问题：是否有值得持仓者立刻注意的消息面或数据异常（利空突袭、监管突发、轧空风险、动能与消息背离等）？
- 一切正常：只输出"OK"
- 有异常：用一句话说明异常和应采取的动作，不超过50字。不要输出其他任何内容。"""
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    resp.raise_for_status()
    out = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    if out.upper().startswith("OK"):
        return None
    return out[:60]


def open_signals(store):
    """返回所有未结案(仍在追踪)的信号"""
    return [s for s in store['signals'] if s['status'] == 'active']


def pick_best_candidate(candidates):
    """多个机会并存时只推一个(2026-10-19 用户要求一次只做一个策略):
    按 4H ADX 择优 —— 趋势强度是初筛漏斗里确定性的核心来源;
    并列时取价格更贴近 EMA20 的(回踩结构更好, 入场风险更小)。"""
    return max(candidates, key=lambda c: (c[0]['4h_adx'],
                                          -abs(c[0]['price'] - c[0]['4h_ema20'])))


def send_event_notice(events):
    """事件驱动推送: 离场结案与预警合并成一条, 平时完全静默(2026-10-19 用户确认)。
    - 紧急(排列翻转但止损未触): ⚠️ 提示立刻决策离场, 单独成段置顶;
    - 普通结案: 📋 一行结算(原因+最终浮盈+峰值), 供长期衡量;
    - 预警(不结案): ⚠️ 一行, 提示不对劲但策略点位仍有效。
    """
    if not events:
        return
    urgent, exits, alerts = [], [], []
    for e in events:
        if e['type'] == 'exit':
            if e.get('urgent'):
                urgent.append(e)
            else:
                exits.append(e)
        else:
            alerts.append(e)
    lines = []
    for e in urgent:
        s = e['sig']
        lines.append(
            f"⚠️ <b>{s['symbol']} 趋势排列已翻转但止损未触发</b>\n"
            f"{'做空' if s['direction'] == 'short' else '做多'} @{s['entry_price']} "
            f"现价 {s['last_price']} (浮盈 {s['pnl_now']:+.2f}%, 止损位 {s['strategy']['stop']})\n"
            f"止损单不会保护你——别等止损, 尽快人工决策离场。"
        )
    if exits:
        lines.append("📋 <b>信号结案</b>")
        for e in exits:
            s = e['sig']
            reason = {
                'stopped_out': "触及止损离场", 'took_profit': "止盈离场",
                'trend_reversed': "趋势翻转离场", 'timeout': "追踪满48h关闭",
                'manual_reset': "人工清仓重置",
            }[e['reason']]
            mech = "" if s.get('strategy') else "（无点位存档, 机械口径）"
            lines.append(
                f"{s['symbol']} {'做空' if s['direction'] == 'short' else '做多'}"
                f" @{s['entry_price']}: {reason} | 最终 {s['pnl_now']:+.2f}%"
                f" | 峰值 {s['mfe']:+.2f}%{mech}"
            )
    if alerts:
        lines.append("⚠️ <b>预警</b>")
        for e in alerts:
            s = e['sig']
            strat = s.get('strategy') or {}
            if e['reason'] == 'drawdown':
                detail = (f"浮盈大幅回吐: 峰值 {s['mfe']:+.2f}% -> 现 {s['pnl_now']:+.2f}%, "
                          f"注意离场时机")
            elif e['reason'] == 'stall':
                act = strat.get('timeout_action') or '策略未给出超时预案'
                detail = (f"开仓已超策略时间预期({strat.get('hold_hours')}h)仍在成本区"
                          f"(浮盈 {s['pnl_now']:+.2f}%), 策略自己说过: {act}")
            elif e['reason'] == 'funding':
                detail = "资金费率出现逆势深值, 该方向拥挤, 有轧空/踩踏风险"
            elif e['reason'] == 'ai_review':
                detail = f"AI 消息面复核: {e.get('detail', '')}"
            else:
                detail = "4H ADX 跌破 22, 趋势动能消散, 留意假突破"
            lines.append(
                f"{s['symbol']} {'做空' if s['direction'] == 'short' else '做多'}"
                f" @{s['entry_price']}: {detail}"
            )
    requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": "\n\n".join(lines), "parse_mode": "HTML"},
        timeout=10,
    ).raise_for_status()


def run_scan():
    if not TG_BOT_TOKEN or not TG_CHAT_ID or not GEMINI_API_KEY:
        raise SystemExit("TG_BOT_TOKEN / TG_CHAT_ID / GEMINI_API_KEY 未设置")

    # 先更新历史信号档案: 结案/预警事件攒起来最后合并推一条
    store = tracker.load_signals()
    _, events = tracker.update_open_signals(store, get_market_data, fetch_klines_since)
    # 在持信号的消息面复核(带节流, 有单才调用 API)
    review_signals_with_ai(store, events)

    fng = get_macro_sentiment()
    # 全币种先扫描完再择优: 一次只推一个策略, 避免同时来好几单无从选择
    candidates = []
    for sym in SYMBOLS:
        try:
            data = get_market_data(sym)
            signal = check_signal(data)
            if signal:
                candidates.append((data, signal))
                print(f"[{sym}] 发现确定性机会: {signal} (候选, 等待择优)")
            else:
                print(f"[{sym}] 当前价 ${data['price']} (4H ADX={data['4h_adx']}, 1H RSI={data['1h_rsi']}) -> 暂无高确定性趋势点位，继续静默监控。")
        except Exception as e:
            print(f"[{sym}] 扫描异常: {e}")

    # 手上有未结案信号 = 用户还在做这一个策略, 不推任何新策略(一次只搞一个)
    if open_signals(store):
        if candidates:
            print(f"[*] 有候选机会 {len(candidates)} 个, 但当前有信号在追踪, 不推新策略")
    elif candidates:
        data, signal = pick_best_candidate(candidates)
        sym = data['symbol']
        try:
            sig = tracker.record_new_signal(store, data, signal)
            if sig is None:
                # 理论上不会到这(无未结案信号才会进这分支), 兜底防止重复建档
                print(f"[{sym}] 建档去重命中, 跳过")
            else:
                print(f"[{sym}] 择优胜出: {signal}，正在生成策略并推送 TG...")
                strategy_text, strategy = generate_ai_strategy(data, signal, fng)
                send_telegram_msg(sym, data['price'], signal, strategy_text)
                # 推送成功才正式建档: 失败时回退, 下次巡检信号仍成立会重试推送
                sig['strategy'] = strategy
                store['signals'].append(sig)
        except Exception as e:
            print(f"[{sym}] 策略生成/推送异常: {e}")

    # 事件驱动推送: 有结案/预警才发, 平时完全静默
    if events:
        try:
            send_event_notice(events)
        except Exception as e:
            print(f"事件通知推送失败: {e}")

    tracker.save_signals(store)

if __name__ == "__main__":
    run_scan()
