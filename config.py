# config.py

import os

# =========================
# Discord
# =========================

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# =========================
# API
# =========================

POLYMARKET_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"

KALSHI_MARKETS_URL = "https://external-api.kalshi.com/trade-api/v2/markets"

BTC_SPOT_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"

USER_AGENT = "MarketLineRadarGitHubBot/1.0"

# =========================
# Files
# =========================

MARKETS_JSON_PATH = "markets.json"

# =========================
# Scan settings
# =========================

POLYMARKET_LIMIT = 300
POLYMARKET_MAX_PAGES = 3

KALSHI_LIMIT = 500

MIN_VOLUME = 0

# =========================
# Default grid settings
# =========================

GRID_HALF_STEPS = 5

# Discordは2000文字制限があるので余裕を見る
DISCORD_MESSAGE_LIMIT = 1900
