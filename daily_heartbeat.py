"""Daily 08:00 summary: universe size, open positions and last scan time."""
import os

import requests

import tracker
import universe

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
DAILY_ENABLED = os.getenv("RADAR_DAILY_HEARTBEAT", "1") == "1"


def build_message(store, pool):
    signals = store.get("signals", []) if isinstance(store, dict) else []
    active = [s for s in signals if s.get("status") == "active"]
    longs = sum(1 for s in active if s.get("direction") == "long")
    shorts = sum(1 for s in active if s.get("direction") == "short")
    symbols = pool.get("symbols", []) if isinstance(pool, dict) else []
    latest = max((s.get("last_checked_at") for s in signals
                  if s.get("last_checked_at")), default=None)
    return (
        "crypto-radar 每日巡检\n"
        f"标的池: {len(symbols)}\n"
        f"在持: {len(active)}（多 {longs} / 空 {shorts}）\n"
        f"最近扫描: {latest or '无'}"
    )


def main():
    if not DAILY_ENABLED:
        print("daily heartbeat disabled")
        return
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        raise SystemExit("TG_BOT_TOKEN / TG_CHAT_ID 未设置")
    text = build_message(tracker.load_signals(), universe.load_cached())
    requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10
    ).raise_for_status()


if __name__ == "__main__":
    main()
