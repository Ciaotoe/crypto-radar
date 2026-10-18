#!/usr/bin/env bash
# cron 包装脚本: 注入代理(OKX/Gemini 需走代理)与 .env 密钥后执行巡检
# 若代理服务(127.0.0.1:7890)未运行, OKX 资金费率会静默降级为 0, Gemini 调用会失败
cd "$(dirname "$0")"
export http_proxy=http://127.0.0.1:7890
export https_proxy=http://127.0.0.1:7890
export no_proxy=localhost,127.0.0.1
exec /home/ubuntu/.local/bin/uv run --env-file .env python crypto_radar.py
