"""Download multi-symbol full-history 1H corpus into research/data/ (read-only w.r.t. prod).

Snapshots use the exact format of `backtest.py --record`, so every file here is
loadable by `backtest.load_snapshot` without touching the production data path.

OKX history-candles holds years of 1H depth (BTC since 2019-12), unlike 15m
(~15 days) — this corpus is what makes long-horizon signal screening possible.

Usage:
  uv run --no-sync python research/fetch_corpus.py            # all symbols
  uv run --no-sync python research/fetch_corpus.py BTC/USDT   # single symbol
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest as B  # noqa: E402

# Today's liquidity leaders with OKX 1H history back to at least 2021-06
# (verified 2026-10-25). APT/ARB/OP/SUI listed later and are intentionally
# excluded to keep the corpus rectangular. Selecting by *current* liquidity
# introduces survivorship bias — stated, not hidden; results must carry it.
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "XRP/USDT", "ADA/USDT",
    "AVAX/USDT", "LINK/USDT", "LTC/USDT", "DOT/USDT", "TRX/USDT", "UNI/USDT",
    "FIL/USDT", "NEAR/USDT",
]

DAYS = 2500  # ~6.8 years; fetch stops earlier on its own where history ends
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def fetch_one(symbol, days=DAYS):
    path = os.path.join(OUT_DIR, symbol.replace("/", "_") + ".json.gz")
    if os.path.exists(path):
        print(f"[{symbol}] 快照已存在, 跳过 ({path})")
        return
    t0 = time.time()
    # Reuse backtest's own recorder so the format can never drift from the
    # loader used by tests/screening.
    B.record_snapshot(path, symbol, days)
    print(f"[{symbol}] {time.time() - t0:.0f}s")


def main(argv):
    symbols = argv or SYMBOLS
    failed = []
    for symbol in symbols:
        try:
            fetch_one(symbol)
        except (Exception, SystemExit) as exc:
            # one coin failing must not kill the corpus; record_snapshot
            # raises SystemExit (BaseException) on insufficient history.
            failed.append(symbol)
            print(f"[{symbol}] 下载失败: {exc}", file=sys.stderr)
    if failed:
        raise SystemExit(f"失败币种(未静默跳过): {failed}")


if __name__ == "__main__":
    main([s.strip() for s in sys.argv[1:] if s.strip()])
