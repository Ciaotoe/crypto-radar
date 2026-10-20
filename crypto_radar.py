import os
import time
from datetime import datetime, timedelta
import requests
import pandas as pd
import pandas_ta as ta
import tracker
import strategy

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

# ================= 4. AI 复核(只可否决)与 TG 推送 =================
def ai_veto_check(plan, fng):
    """推送前 AI 复核: 只允许否决(利空/风险), 不能改点位或强行解释成立。
    返回 None = 放行推送; 返回字符串 = 否决原因(不推, 不建档案)。
    复核调用失败(网络/API)时放行 —— 机械规则已完整校验, 不能让推送依赖
    单点外部模型服务; 复核意见只进日志, 无档案可写。"""
    prompt = f"""
你是加密货币策略风控参谋。规则系统按以下已通过全部机械校验的计划准备推送一条交易策略：

- 币种：{plan.get('symbol', '')}（Binance USDT 永续）
- 方向：{'做多' if plan['direction'] == 'long' else '做空'}
- 入场区间：{plan['entry_min']} – {plan['entry_max']}
- 硬止损：{plan['stop']}；TP1 {plan['tp1']} / TP2 {plan['tp2']}
- 市场恐慌贪婪指数：{fng}

只回答一个问题：从消息面看，这条策略现在是否存在不该推送的重大风险（利空突袭、监管突发、轧空/踩踏、数据异常）？
- 没有重大风险：只输出 "OK"
- 有：用一句话说明风险，不超过 50 字。不要输出其他任何内容，不要建议修改点位。
"""
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    try:
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
    except Exception as e:
        print(f"[ai_veto_check] 复核调用失败, 放行推送: {e}")
        return None


def send_telegram_msg(symbol, price, strategy_text, direction="long"):
    """推送 TG 消息 (HTML 排版, 规避 Markdown 转义坑)"""
    emoji = "🟢" if direction == "long" else "🔴"
    text = (
        f"{emoji} <b>交易策略触发 | {symbol} (${price})</b>\n"
        f"<b>信号类型：</b>顺势{'做多' if direction == 'long' else '做空'}"
        f"（4H 趋势确立 + 1H 回踩确认，点位由规则生成）\n\n"
        f"{strategy_text}\n\n"
        f"数据来源：Binance USDT 永续 | {time.strftime('%Y-%m-%d %H:%M')}"
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
# 复核节流时间戳存档案字段 last_ai_review(进程内字典会随 job 内每轮巡检清零)


def review_signals_with_ai(store, events):
    """在持信号每 AI_REVIEW_HOURS 做一次消息面复核(真正看新闻的那一层)。
    机械规则覆盖不到的阴跌/消息面异动, 由大模型基于头条+费率+FNG 判断;
    只产出 alert 事件, 结案权仍在机械层。API 调用有节流, 无单时不调用。

    节流时间戳必须落盘到档案(last_ai_review): job 内循环每轮巡检都是
    新 python 进程, 进程内字典会清零, 落盘前实测每 20 分钟就复核一次。
    """
    now = datetime.now(tracker.BJ_TZ)
    to_review = []
    for s in open_signals(store):
        last = s.get('last_ai_review')
        if last is None or now - datetime.fromisoformat(last) >= timedelta(hours=AI_REVIEW_HOURS):
            to_review.append(s)
    if not to_review:
        return
    heads = fetch_news_headlines()
    fng = get_macro_sentiment()
    for s in to_review:
        s['last_ai_review'] = now.isoformat()
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
            # 新策略用 Binance 永续公开行情(strategy.py 独立数据入口);
            # get_market_data 仅保留给追踪层, 与新策略解耦
            data = strategy.get_strategy_market_data(sym)
            signal = strategy.signal_direction(data)
            candidates.append(data)
            print(f"[{sym}] 确认机会 ({signal}), 等待择优")
        except strategy.StrategyRejected as e:
            print(f"[{sym}] 暂无合格机会: {e}")
        except Exception as e:
            print(f"[{sym}] 扫描异常: {e}")

    # 手上有未结案信号 = 用户还在做这一个策略, 不推任何新策略(一次只搞一个)
    if open_signals(store):
        if candidates:
            print(f"[*] 有候选机会 {len(candidates)} 个, 但当前有信号在追踪, 不推新策略")
    elif candidates:
        plan, data, attempts = strategy.build_from_candidates(candidates)
        for sym, reason in attempts:
            print(f"[{sym}] 候选构建计划失败: {reason}")
        if plan is not None:
            sym = data['symbol']
            try:
                # 推送前复核: 距离扫描已过若干秒, 用新行情确认信号仍成立、
                # 现价仍在入场区间; 失效即放弃, 不现场改点位硬推
                fresh = strategy.get_strategy_market_data(sym)
                strategy.ensure_pushable(plan, fresh)
                # 事件驱动口径: 新策略推送保留, 文案由代码按点位计划格式化;
                # AI(Gemini) 只可否决, 不能改点位或解释成立
                verdict = ai_veto_check(plan, fng)
                if verdict:
                    print(f"[{sym}] AI 复核否决, 不推: {verdict}")
                else:
                    print(f"[{sym}] 择优胜出, 生成计划并推送 TG...")
                    send_telegram_msg(sym, fresh['price'],
                                      strategy.format_strategy_text(plan),
                                      plan['direction'])
                    # 推送成功才正式建档: 失败时回退, 下次巡检信号仍成立会重试推送
                    # record_new_signal 按文案里的 做多/做空 判方向, 传规则口径文案
                    sig = tracker.record_new_signal(
                        store, fresh,
                        f"顺势{'做多' if plan['direction'] == 'long' else '做空'}"
                        f"（4H 趋势确立 + 1H 回踩确认，规则点位）")
                    sig['strategy'] = strategy.simplify_plan(plan)
                    store['signals'].append(sig)
            except strategy.StrategyRejected as e:
                print(f"[{sym}] 推送前复核拒绝: {e}")
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
