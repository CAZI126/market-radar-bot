# config.py

import os

# ============================================================
# Discord
# ============================================================

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_MESSAGE_LIMIT = 1900


# ============================================================
# File paths
# ============================================================

MARKETS_JSON_PATH = "markets.json"


# ============================================================
# HTTP
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)


# ============================================================
# Polymarket
# ============================================================

POLYMARKET_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"

POLYMARKET_LIMIT = 100
POLYMARKET_MAX_PAGES = 3


# ============================================================
# Kalshi
# ============================================================

KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_LIMIT = 200


# ============================================================
# Price sources
# ============================================================

BINANCE_SPOT_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"

OSTIUM_LATEST_PRICES_URL = "https://metadata-backend.ostium.io/PricePublish/latest-prices"
OSTIUM_LATEST_PRICE_URL = "https://metadata-backend.ostium.io/PricePublish/latest-price"

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"


# ============================================================
# Filtering
# ============================================================

MIN_VOLUME = 0
