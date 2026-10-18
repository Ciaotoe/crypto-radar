#!/usr/bin/env bash
# 本地手动运行(可选; 常规运行走 GitHub Actions, 云端无地域封锁):
# 注意: 本机直连 TG/Gemini/OKX 不通, 依赖本地代理(127.0.0.1:7890)
cd "$(dirname "$0")"
export http_proxy=http://127.0.0.1:7890
export https_proxy=http://127.0.0.1:7890
export no_proxy=localhost,127.0.0.1
exec /home/ubuntu/.local/bin/uv run --env-file .env python crypto_radar.py
