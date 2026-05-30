# main.py

import os
import re
import json
import time
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from config import (
    DISCORD_WEBHOOK_URL,
    POLYMARKET_GAMMA_MARKETS_URL,
    POLYMARKET_SEARCH_URL,
    KALSHI_MARKETS_URL,
    BINANCE_SPOT_PRICE_URL,
    OSTIUM_LATEST_PRICES_URL,
    OSTIUM_LATEST_PRICE_URL,
    HYPERLIQUID_INFO_URL,
    USER_AGENT,
    MARKETS_JSON_PATH,
    POLYMARKET_LIMIT,
    POLYMARKET_MAX_PAGES,
    KALSHI_LIMIT,
    MIN_VOLUME,
    DISCORD_MESSAGE_LIMIT,
)

load_dotenv()

BINANCE_PRICE_CACHE: Dict[str, float] = {}
HYPERLIQUID_MIDS_CACHE: Optional[Dict[str, Any]] = None
OSTIUM_LATEST_PRICES_CACHE: Optional[Any] = None

MAX_LINES_PER_SIDE = 10
MAX_FOCUS_LINES = 3


# ============================================================
# Basic utils
# ============================================================

def now_ts() -> int:
    return int(time.time())


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default

    if isinstance(value, bool):
        return default

    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)

    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("$", "")
        if s == "":
            return default
        try:
            return float(s)
        except ValueError:
            return default

    return default


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def normalize_symbol(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def format_pct(p: Optional[float]) -> str:
    if p is None:
        return "n/a"
    return f"{p * 100:.1f}%"


def parse_jsonish(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, (list, dict)):
        return value

    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value

    return value


def round_to_step(value: float, step: int) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def is_near_step(value: float, step: int, tolerance: float = 1.0) -> bool:
    if step <= 0:
        return True

    nearest = round_to_step(value, step)
    return abs(value - nearest) <= tolerance


def http_get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Any:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def http_post_json(url: str, payload: Dict[str, Any], timeout: int = 25) -> Any:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def get_discord_webhook_url() -> str:
    return os.getenv("DISCORD_WEBHOOK_URL") or DISCORD_WEBHOOK_URL or ""


def discord_send(content: str) -> bool:
    webhook = get_discord_webhook_url()

    if not webhook or webhook.strip() == "" or "ここに" in webhook:
        print("\n[WARN] DISCORD_WEBHOOK_URL が未設定です。Discordには送信せず、内容だけ表示します。\n")
        print(content)
        print("\n")
        return False

    payload = {
        "content": content[:DISCORD_MESSAGE_LIMIT]
    }

    try:
        response = requests.post(webhook, json=payload, timeout=25)

        if response.status_code >= 300:
            print("[ERROR] Discord webhook failed:", response.status_code, response.text)
            return False

        print("[INFO] Discord sent:", response.status_code)
        return True

    except Exception as e:
        print("[ERROR] Discord send failed:", e)
        return False


def discord_send_many(messages: List[str]) -> None:
    if not messages:
        return

    total = len(messages)

    for i, message in enumerate(messages, start=1):
        if total > 1:
            message = f"{message}\n\n`part {i}/{total}`"

        discord_send(message)
        time.sleep(0.7)


# ============================================================
# Market config
# ============================================================

def load_market_config() -> List[Dict[str, Any]]:
    if not os.path.exists(MARKETS_JSON_PATH):
        raise FileNotFoundError(f"{MARKETS_JSON_PATH} not found")

    with open(MARKETS_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    markets = data.get("markets", [])

    if not isinstance(markets, list):
        return []

    return [m for m in markets if m.get("enabled", True)]


def get_all_search_queries(markets: List[Dict[str, Any]]) -> List[str]:
    queries = []

    for market in markets:
        for q in market.get("search_queries", []) or []:
            q = str(q).strip()
            if q and q not in queries:
                queries.append(q)

    return queries


# ============================================================
# Center prices
# ============================================================

def normalize_possible_center_price(raw_price: Any, market: Dict[str, Any]) -> Optional[float]:
    p = safe_float(raw_price)

    if p is None or p <= 0:
        return None

    manual = safe_float(market.get("manual_center"))
    range_pct = safe_float(market.get("range_pct"), 0.1) or 0.1

    candidates = [
        p,
        p / 10,
        p / 100,
        p / 1_000,
        p / 10_000,
        p / 100_000,
        p / 1_000_000,
        p / 10_000_000,
        p / 100_000_000,
        p / 1_000_000_000,
        p / 10_000_000_000,
    ]

    if manual is not None and manual > 0:
        low = manual * (1 - max(range_pct, 0.25))
        high = manual * (1 + max(range_pct, 0.25))

        for x in candidates:
            if low <= x <= high:
                return x

    key = str(market.get("key", "")).lower()
    category = str(market.get("category", "")).lower()

    if "btc" in key:
        for x in candidates:
            if 10_000 <= x <= 1_000_000:
                return x

    if "eth" in key:
        for x in candidates:
            if 500 <= x <= 50_000:
                return x

    if "gold" in key or "xau" in key:
        for x in candidates:
            if 1_000 <= x <= 10_000:
                return x

    if "silver" in key or "xag" in key:
        for x in candidates:
            if 10 <= x <= 300:
                return x

    if "oil" in key or category == "oil":
        for x in candidates:
            if 40 <= x <= 200:
                return x

    return None


def fetch_binance_price(symbol: str) -> Optional[float]:
    symbol = (symbol or "").strip().upper()

    if not symbol:
        return None

    if symbol in BINANCE_PRICE_CACHE:
        return BINANCE_PRICE_CACHE[symbol]

    try:
        data = http_get_json(
            BINANCE_SPOT_PRICE_URL,
            params={"symbol": symbol},
            timeout=15,
        )

        price = safe_float(data.get("price"))

        if price is not None and price > 0:
            BINANCE_PRICE_CACHE[symbol] = price
            return price

    except Exception as e:
        print(f"[WARN] Binance price fetch failed symbol={symbol}:", e)

    return None


def fetch_hyperliquid_all_mids() -> Optional[Dict[str, Any]]:
    global HYPERLIQUID_MIDS_CACHE

    if HYPERLIQUID_MIDS_CACHE is not None:
        return HYPERLIQUID_MIDS_CACHE

    try:
        data = http_post_json(
            HYPERLIQUID_INFO_URL,
            payload={"type": "allMids"},
            timeout=20,
        )

        if isinstance(data, dict):
            HYPERLIQUID_MIDS_CACHE = data
            return data

    except Exception as e:
        print("[WARN] Hyperliquid allMids fetch failed:", e)

    return None


def fetch_hyperliquid_price(coin: str) -> Optional[float]:
    coin = (coin or "").strip().upper()

    if not coin:
        return None

    data = fetch_hyperliquid_all_mids()

    if not isinstance(data, dict):
        return None

    for key in [coin, coin.upper(), coin.lower()]:
        if key in data:
            price = safe_float(data.get(key))
            if price is not None and price > 0:
                print(f"[INFO] Hyperliquid price {coin}: {price}")
                return price

    for k, v in data.items():
        if str(k).upper() == coin:
            price = safe_float(v)
            if price is not None and price > 0:
                print(f"[INFO] Hyperliquid price {coin}: {price}")
                return price

    print(f"[WARN] Hyperliquid coin not found: {coin}")
    return None


def fetch_ostium_latest_prices() -> Optional[Any]:
    global OSTIUM_LATEST_PRICES_CACHE

    if OSTIUM_LATEST_PRICES_CACHE is not None:
        return OSTIUM_LATEST_PRICES_CACHE

    try:
        data = http_get_json(OSTIUM_LATEST_PRICES_URL, timeout=20)
        OSTIUM_LATEST_PRICES_CACHE = data
        return data
    except Exception as e:
        print("[WARN] Ostium latest-prices fetch failed:", e)
        return None


def extract_price_from_dict(obj: Dict[str, Any], market: Dict[str, Any]) -> Optional[float]:
    price_keys = [
        "price",
        "value",
        "last",
        "mark",
        "mid",
        "indexPrice",
        "index_price",
        "oraclePrice",
        "oracle_price",
        "answer",
        "bid",
        "ask",
    ]

    for key in price_keys:
        if key in obj:
            px = normalize_possible_center_price(obj.get(key), market)
            if px is not None:
                return px

    return None


def dict_text_blob(obj: Dict[str, Any]) -> str:
    parts = []

    for key in [
        "asset",
        "symbol",
        "ticker",
        "name",
        "pair",
        "feed",
        "market",
        "description",
        "id",
    ]:
        if key in obj:
            parts.append(str(obj.get(key)))

    return " ".join(parts)


def walk_ostium_for_asset(data: Any, asset_names: List[str], market: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    normalized_targets = [normalize_symbol(x) for x in asset_names if x]
    found: List[Tuple[float, str]] = []

    def walk(obj: Any, path: str = "") -> None:
        if isinstance(obj, list):
            for x in obj:
                walk(x, path)
            return

        if isinstance(obj, dict):
            blob = f"{path} {dict_text_blob(obj)}"
            normalized_blob = normalize_symbol(blob)

            matched = any(t and t in normalized_blob for t in normalized_targets)

            if matched:
                px = extract_price_from_dict(obj, market)
                if px is not None:
                    found.append((px, blob[:120]))

            for k, v in obj.items():
                walk(v, f"{path} {k}")

    walk(data)

    if not found:
        return None

    return found[0]


def fetch_ostium_individual_price(asset: str, market: Dict[str, Any]) -> Optional[float]:
    asset = (asset or "").strip()

    if not asset:
        return None

    try:
        data = http_get_json(
            OSTIUM_LATEST_PRICE_URL,
            params={"asset": asset},
            timeout=15,
        )

        print(f"[INFO] Ostium individual response asset={asset}: {str(data)[:220]}")

        if isinstance(data, dict):
            px = extract_price_from_dict(data, market)
            if px is not None:
                return px

            found = walk_ostium_for_asset(data, [asset], market)
            if found:
                return found[0]

        if isinstance(data, list):
            found = walk_ostium_for_asset(data, [asset], market)
            if found:
                return found[0]

        px = normalize_possible_center_price(data, market)
        if px is not None:
            return px

    except Exception as e:
        print(f"[WARN] Ostium individual fetch failed asset={asset}: {e}")

    return None


def fetch_ostium_price(market: Dict[str, Any]) -> Optional[float]:
    candidates = []

    primary = market.get("ostium_asset")
    if primary:
        candidates.append(str(primary))

    for x in market.get("ostium_asset_candidates", []) or []:
        s = str(x).strip()
        if s and s not in candidates:
            candidates.append(s)

    for asset in candidates:
        price = fetch_ostium_individual_price(asset, market)

        if price is not None:
            print(f"[INFO] Ostium center {market.get('key')}: {price} / asset={asset}")
            return price

    data = fetch_ostium_latest_prices()

    if data is None:
        return None

    found = walk_ostium_for_asset(data, candidates, market)

    if found:
        price, matched = found
        print(f"[INFO] Ostium latest-prices center {market.get('key')}: {price} / matched={matched}")
        return price

    print(f"[WARN] Ostium price not found for {market.get('key')} candidates={candidates}")
    return None


def choose_center(market: Dict[str, Any], rows: List[Dict[str, Any]]) -> Tuple[Optional[float], str]:
    center_type = market.get("center_type", "manual")
    step = int(market.get("grid_step") or 1)

    if center_type == "ostium":
        price = fetch_ostium_price(market)
        if price is not None:
            return round_to_step(price, step), "Ostium"

        manual = safe_float(market.get("manual_center"))
        if manual is not None and manual > 0:
            return round_to_step(manual, step), "Manual fallback"

    if center_type == "hyperliquid":
        coin = market.get("hyperliquid_coin", "")
        price = fetch_hyperliquid_price(coin)
        if price is not None:
            return round_to_step(price, step), "Hyperliquid"

        manual = safe_float(market.get("manual_center"))
        if manual is not None and manual > 0:
            return round_to_step(manual, step), "Manual fallback"

    if center_type == "manual":
        manual = safe_float(market.get("manual_center"))

        if manual is not None and manual > 0:
            return round_to_step(manual, step), "Manual"

    if center_type == "binance":
        symbol = market.get("binance_symbol", "")
        price = fetch_binance_price(symbol)

        if price is not None:
            return round_to_step(price, step), "Binance"

        manual = safe_float(market.get("manual_center"))
        if manual is not None and manual > 0:
            return round_to_step(manual, step), "Manual fallback"

    if rows:
        battleground = min(
            rows,
            key=lambda r: abs((r.get("probability") or 0) - 0.5)
        )
        return battleground.get("line_value"), "Prediction"

    return None, "n/a"


def get_reference_center_for_filter(market: Dict[str, Any]) -> Optional[float]:
    manual = safe_float(market.get("manual_center"))

    if manual is not None and manual > 0:
        return manual

    center_type = market.get("center_type", "manual")

    if center_type == "hyperliquid":
        return fetch_hyperliquid_price(market.get("hyperliquid_coin", ""))

    if center_type == "binance":
        return fetch_binance_price(market.get("binance_symbol", ""))

    if center_type == "ostium":
        return fetch_ostium_price(market)

    return None


# ============================================================
# Detect / extract
# ============================================================

def title_matches_market(title: str, market: Dict[str, Any]) -> bool:
    text = normalize_text(title)

    for bad in market.get("exclude", []) or []:
        bad = str(bad).strip().lower()
        if bad and bad in text:
            return False

    keywords = market.get("keywords", []) or []

    if not keywords:
        return False

    for kw in keywords:
        kw = str(kw).strip().lower()
        if kw and kw in text:
            return True

    return False


def detect_market_key(title: str, markets: List[Dict[str, Any]]) -> Optional[str]:
    for market in markets:
        if title_matches_market(title, market):
            return market.get("key")

    return None


def get_market_by_key(markets: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    for market in markets:
        if market.get("key") == key:
            return market

    return None


def format_line_label(value: float, market: Dict[str, Any]) -> str:
    step = int(market.get("grid_step") or 1)

    if value >= 10_000:
        k = value / 1000
        if abs(k - round(k)) < 0.01:
            return f"${int(round(k))}k"
        return f"${k:g}k"

    if step >= 1:
        if abs(value - round(value)) < 0.01:
            return f"${int(round(value))}"

    return f"${value:g}"


def detect_direction(title: str, line_value: Optional[float], market: Dict[str, Any]) -> str:
    text = normalize_text(title)

    down_patterns = [
        "below",
        "under",
        "less than",
        "at or below",
        "close below",
        "closes below",
        "dip to",
        "dip below",
        "lower than",
        "fall below",
        "falls below",
        "drop below",
        "drops below",
        "crash to",
        "collapse to",
        "go below",
    ]

    over_patterns = [
        "above",
        "over",
        "greater than",
        "at or above",
        "close above",
        "closes above",
        "higher than",
        "exceed",
        "exceeds",
        "go above",
        "be above",
    ]

    for p in down_patterns:
        if p in text:
            return "down"

    for p in over_patterns:
        if p in text:
            return "over"

    ambiguous_patterns = [
        "hit",
        "hits",
        "reach",
        "reaches",
        "touch",
        "touches",
    ]

    if any(p in text for p in ambiguous_patterns):
        ref_center = get_reference_center_for_filter(market)

        if ref_center is not None and line_value is not None:
            if line_value >= ref_center:
                return "over"
            return "down"

    return "unknown"


def extract_line(title: str, market: Dict[str, Any]) -> Tuple[Optional[str], Optional[float]]:
    text = title or ""
    step = int(market.get("grid_step") or 1)
    range_pct = safe_float(market.get("range_pct"), 0.1) or 0.1

    money_candidates: List[float] = []

    for match in re.finditer(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?)", text):
        raw = match.group(1).replace(",", "")
        value = safe_float(raw)

        if value is not None:
            money_candidates.append(value)

    for match in re.finditer(r"\$?\s*\b([0-9]+(?:\.\d+)?)\s*([kKmM])?\b", text):
        raw = match.group(1)
        suffix = match.group(2)
        value = safe_float(raw)

        if value is None:
            continue

        if suffix:
            if suffix.lower() == "k":
                value *= 1_000
            elif suffix.lower() == "m":
                value *= 1_000_000

        money_candidates.append(value)

    filtered = []
    ref_center = get_reference_center_for_filter(market)

    for value in money_candidates:
        if 2020 <= value <= 2035:
            continue

        if value <= 0:
            continue

        if ref_center is not None and ref_center > 0:
            low = ref_center * (1 - range_pct)
            high = ref_center * (1 + range_pct)

            if not (low <= value <= high):
                continue

            tolerance = max(1.0, step * 0.12)

            if not is_near_step(value, step, tolerance=tolerance):
                continue

        filtered.append(value)

    if not filtered:
        return None, None

    value = filtered[0]
    return format_line_label(value, market), value


# ============================================================
# Probability parsing
# ============================================================

def polymarket_yes_probability(market: Dict[str, Any]) -> Optional[float]:
    outcomes = parse_jsonish(market.get("outcomes"))
    prices = parse_jsonish(market.get("outcomePrices"))

    if isinstance(outcomes, list) and isinstance(prices, list):
        for i, outcome in enumerate(outcomes):
            if str(outcome).strip().lower() == "yes" and i < len(prices):
                p = safe_float(prices[i])

                if p is not None:
                    if p > 1:
                        p = p / 100

                    return max(0.0, min(1.0, p))

    for key in [
        "lastTradePrice",
        "last_trade_price",
        "bestAsk",
        "bestBid",
        "price",
    ]:
        p = safe_float(market.get(key))

        if p is not None:
            if p > 1:
                p = p / 100

            return max(0.0, min(1.0, p))

    return None


def kalshi_yes_probability(market: Dict[str, Any]) -> Optional[float]:
    yes_bid = safe_float(market.get("yes_bid"))
    yes_ask = safe_float(market.get("yes_ask"))

    if yes_bid is not None and yes_ask is not None:
        mid = (yes_bid + yes_ask) / 2

        if mid > 1:
            mid = mid / 100

        return max(0.0, min(1.0, mid))

    for key in [
        "last_price",
        "yes_price",
        "price",
    ]:
        p = safe_float(market.get(key))

        if p is not None:
            if p > 1:
                p = p / 100

            return max(0.0, min(1.0, p))

    return None


# ============================================================
# Market conversion
# ============================================================

def market_to_common_item(raw: Dict[str, Any], markets_config: List[Dict[str, Any]], source_hint: str) -> Optional[Dict[str, Any]]:
    title = (
        raw.get("question")
        or raw.get("title")
        or raw.get("name")
        or raw.get("slug")
        or ""
    )

    if not title:
        return None

    market_key = detect_market_key(title, markets_config)

    if not market_key:
        return None

    config_market = get_market_by_key(markets_config, market_key)

    if not config_market:
        return None

    line_label, line_value = extract_line(title, config_market)

    if not line_label:
        return None

    direction = detect_direction(title, line_value, config_market)

    if source_hint == "Polymarket":
        probability = polymarket_yes_probability(raw)
    else:
        probability = kalshi_yes_probability(raw)

    if probability is None:
        return None

    market_id = str(
        raw.get("id")
        or raw.get("conditionId")
        or raw.get("ticker")
        or raw.get("slug")
        or title
    )

    return {
        "ts": now_ts(),
        "source": source_hint,
        "market_id": market_id,
        "title": title,
        "market_key": market_key,
        "line_label": line_label,
        "line_value": line_value,
        "direction": direction,
        "probability": probability,
    }


def extract_markets_from_search_response(data: Any) -> List[Dict[str, Any]]:
    markets: List[Dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, list):
            for x in obj:
                walk(x)
            return

        if not isinstance(obj, dict):
            return

        has_title = any(k in obj for k in ["question", "title", "name"])
        has_price = any(k in obj for k in ["outcomePrices", "lastTradePrice", "bestAsk", "bestBid", "price"])

        if has_title and has_price:
            markets.append(obj)

        nested_markets = obj.get("markets")

        if isinstance(nested_markets, list):
            for m in nested_markets:
                if isinstance(m, dict):
                    markets.append(m)

        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk(v)

    walk(data)
    return markets


# ============================================================
# Fetchers
# ============================================================

def fetch_polymarket_search_markets(markets_config: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen_ids = set()

    queries = get_all_search_queries(markets_config)

    for q in queries:
        params = {
            "q": q,
            "events_status": "active",
            "limit_per_type": 20,
            "page": 1,
            "keep_closed_markets": 0,
            "optimized": "true",
        }

        try:
            data = http_get_json(POLYMARKET_SEARCH_URL, params=params)
        except Exception as e:
            print(f"[ERROR] Polymarket search failed q={q}:", e)
            continue

        raw_markets = extract_markets_from_search_response(data)
        print(f"[INFO] Polymarket search q={q}: raw markets={len(raw_markets)}")

        for raw in raw_markets:
            item = market_to_common_item(raw, markets_config, source_hint="Polymarket")

            if not item:
                continue

            dedupe_id = f"{item['market_id']}:{item['direction']}"

            if dedupe_id in seen_ids:
                continue

            seen_ids.add(dedupe_id)
            results.append(item)

        time.sleep(0.25)

    return results


def fetch_polymarket_markets(markets_config: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen_ids = set()

    for page in range(POLYMARKET_MAX_PAGES):
        offset = page * POLYMARKET_LIMIT

        params = {
            "active": "true",
            "closed": "false",
            "limit": POLYMARKET_LIMIT,
            "offset": offset,
        }

        try:
            data = http_get_json(POLYMARKET_GAMMA_MARKETS_URL, params=params)
        except Exception as e:
            print(f"[ERROR] Polymarket fetch failed page={page + 1} offset={offset}:", e)
            break

        if not isinstance(data, list):
            print("[WARN] Polymarket response is not list.")
            break

        if not data:
            break

        print(f"[INFO] Polymarket page {page + 1}: {len(data)} markets")

        for raw in data:
            item = market_to_common_item(raw, markets_config, source_hint="Polymarket")

            if not item:
                continue

            dedupe_id = f"{item['market_id']}:{item['direction']}"

            if dedupe_id in seen_ids:
                continue

            seen_ids.add(dedupe_id)
            results.append(item)

        time.sleep(0.25)

    search_items = fetch_polymarket_search_markets(markets_config)

    for item in search_items:
        dedupe_id = f"{item['market_id']}:{item['direction']}"

        if dedupe_id in seen_ids:
            continue

        seen_ids.add(dedupe_id)
        results.append(item)

    return results


def fetch_kalshi_markets(markets_config: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    cursor = None
    pages = 0
    max_pages = 3

    while pages < max_pages:
        pages += 1

        params: Dict[str, Any] = {
            "limit": KALSHI_LIMIT,
            "status": "open",
        }

        if cursor:
            params["cursor"] = cursor

        try:
            data = http_get_json(KALSHI_MARKETS_URL, params=params)
        except Exception as e:
            print("[ERROR] Kalshi fetch failed:", e)
            return results

        if not isinstance(data, dict):
            print("[WARN] Kalshi response is not dict.")
            return results

        markets = data.get("markets")

        if not isinstance(markets, list):
            print("[WARN] Kalshi markets is not list.")
            return results

        if not markets:
            break

        for raw in markets:
            title_parts = [
                str(raw.get("title") or ""),
                str(raw.get("subtitle") or ""),
                str(raw.get("event_title") or ""),
                str(raw.get("category") or ""),
            ]

            raw["title"] = " ".join([p for p in title_parts if p]).strip()

            item = market_to_common_item(raw, markets_config, source_hint="Kalshi")

            if item:
                results.append(item)

        cursor = data.get("cursor")

        if not cursor:
            break

        time.sleep(0.25)

    return results


# ============================================================
# Aggregation
# ============================================================

def aggregate_items(items: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}

    for item in items:
        key = (
            item["market_key"],
            item["direction"],
            item["line_label"],
        )
        grouped.setdefault(key, []).append(item)

    aggregated: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    for key, rows in grouped.items():
        probabilities = [
            safe_float(r.get("probability"))
            for r in rows
            if safe_float(r.get("probability")) is not None
        ]

        if not probabilities:
            continue

        probability = statistics.mean(probabilities)
        representative = rows[0]

        aggregated[key] = {
            "market_key": key[0],
            "direction": key[1],
            "line_label": key[2],
            "line_value": representative.get("line_value"),
            "probability": probability,
            "probabilities": probabilities,
            "count": len(rows),
            "title": representative.get("title") or "",
        }

    return aggregated


# ============================================================
# Thickness score
# ============================================================

def thickness_score(
    count: int,
    total_m: int,
    probabilities: List[float],
    line_value: Optional[float],
    center: float,
    grid_step: int,
) -> float:
    m = max(int(count or 1), 1)

    coverage = 1 - math.exp(-m / 5)
    market_share = math.sqrt(m / total_m) if total_m > 0 else 0

    if len(probabilities) >= 2:
        std = statistics.pstdev(probabilities)
        consistency = math.exp(-std / 0.15)
    else:
        consistency = 0.45

    if line_value is not None and grid_step > 0:
        distance_steps = abs(float(line_value) - float(center)) / float(grid_step)
    else:
        distance_steps = 999

    relevance = math.exp(-distance_steps / 8)

    score = 100 * (
        0.35 * coverage
        + 0.25 * market_share
        + 0.25 * consistency
        + 0.15 * relevance
    )

    return max(0, min(100, score))


def thickness_label(score: float) -> str:
    if score >= 70:
        return "厚"
    if score >= 40:
        return "中"
    return "薄"


# ============================================================
# Rendering
# ============================================================

def bar_for_probability(p: Optional[float]) -> str:
    if p is None:
        return ""

    bar_count = int(round(p * 10))
    bar_count = max(0, min(10, bar_count))
    return "█" * bar_count + "░" * (10 - bar_count)


def sort_rows_by_line(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: r.get("line_value") if r.get("line_value") is not None else 999999999
    )


def rows_total_count(rows: List[Dict[str, Any]]) -> int:
    return sum(int(r.get("count") or 1) for r in rows)


def prepare_grid_rows(rows: List[Dict[str, Any]], center: float) -> List[Dict[str, Any]]:
    """
    価格ラインを一本のGridとして表示する。
    down/overを分けすぎず、現在価格より下側・上側から近いものを残す。
    """
    valid = [r for r in rows if r.get("line_value") is not None]
    valid = sort_rows_by_line(valid)

    lower_or_equal = [r for r in valid if float(r.get("line_value")) <= float(center)]
    upper = [r for r in valid if float(r.get("line_value")) > float(center)]

    lower_keep = sorted(
        lower_or_equal,
        key=lambda r: abs(float(r.get("line_value")) - float(center))
    )[:MAX_LINES_PER_SIDE]

    upper_keep = sorted(
        upper,
        key=lambda r: abs(float(r.get("line_value")) - float(center))
    )[:MAX_LINES_PER_SIDE]

    combined = lower_keep + upper_keep
    return sort_rows_by_line(combined)


def direction_icon(direction: str) -> str:
    if direction == "down":
        return "🔴"
    if direction == "over":
        return "🟢"
    return "⚪"


def render_row(
    row: Dict[str, Any],
    total_m: int,
    center: float,
    market: Dict[str, Any],
    with_icon: bool = False,
) -> str:
    label = row.get("line_label") or ""
    p = row.get("probability")
    count = int(row.get("count") or 1)
    probabilities = row.get("probabilities") or []
    grid_step = int(market.get("grid_step") or 1)

    score = thickness_score(
        count=count,
        total_m=total_m,
        probabilities=probabilities,
        line_value=row.get("line_value"),
        center=center,
        grid_step=grid_step,
    )

    thick = thickness_label(score)
    icon = f"{direction_icon(row.get('direction'))} " if with_icon else ""

    return f"{icon}`{label:<8}` {bar_for_probability(p)} `{format_pct(p):>6}` `{thick}`"


def judge_market_bias(rows: List[Dict[str, Any]], center: float) -> str:
    """
    簡易判定。
    現価格より上側の確率平均と下側の確率平均を見る。
    """
    if not rows:
        return "判定不能"

    lower_probs = []
    upper_probs = []

    for r in rows:
        p = safe_float(r.get("probability"))
        line_value = safe_float(r.get("line_value"))

        if p is None or line_value is None:
            continue

        if line_value <= center:
            lower_probs.append(p)
        else:
            upper_probs.append(p)

    lower_avg = statistics.mean(lower_probs) if lower_probs else None
    upper_avg = statistics.mean(upper_probs) if upper_probs else None

    if upper_avg is not None and lower_avg is not None:
        diff = upper_avg - lower_avg

        if diff >= 0.15:
            return "上方向が強め"
        if diff <= -0.15:
            return "下方向が強め"
        if diff >= 0.07:
            return "中立〜やや上"
        if diff <= -0.07:
            return "中立〜やや下"
        return "中立"

    if upper_avg is not None:
        if upper_avg >= 0.6:
            return "上方向が強め"
        if upper_avg >= 0.45:
            return "中立〜やや上"
        return "中立"

    if lower_avg is not None:
        if lower_avg >= 0.6:
            return "下方向が強め"
        if lower_avg >= 0.45:
            return "中立〜やや下"
        return "中立"

    return "判定不能"


def build_focus_lines(
    rows: List[Dict[str, Any]],
    total_m: int,
    center: float,
    market: Dict[str, Any],
) -> List[str]:
    candidates = []

    for row in rows:
        p = safe_float(row.get("probability"))
        line_value = safe_float(row.get("line_value"))

        if p is None or line_value is None:
            continue

        grid_step = int(market.get("grid_step") or 1)
        count = int(row.get("count") or 1)
        probabilities = row.get("probabilities") or []

        thick_score = thickness_score(
            count=count,
            total_m=total_m,
            probabilities=probabilities,
            line_value=line_value,
            center=center,
            grid_step=grid_step,
        )

        distance = abs(line_value - center) / grid_step if grid_step > 0 else 999
        battleground = 1 - abs(p - 0.5) * 2
        distance_score = math.exp(-distance / 5)

        focus_score = (
            0.45 * battleground
            + 0.35 * (thick_score / 100)
            + 0.20 * distance_score
        )

        candidates.append((focus_score, row))

    candidates = sorted(candidates, key=lambda x: x[0], reverse=True)

    lines = []
    used = set()

    for _, row in candidates:
        key = (row.get("direction"), row.get("line_label"))

        if key in used:
            continue

        used.add(key)
        lines.append(render_row(row, total_m, center, market, with_icon=True))

        if len(lines) >= MAX_FOCUS_LINES:
            break

    return lines


def build_group_block(market: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    center, center_source = choose_center(market, rows)

    if center is None:
        return f"**{market.get('display', market.get('key'))}**\n`no center found`"

    display = market.get("display") or market.get("key")

    grid_rows = prepare_grid_rows(rows, center)
    total_m = rows_total_count(grid_rows)

    bias = judge_market_bias(grid_rows, center)
    focus_lines = build_focus_lines(grid_rows, total_m, center, market)

    center_label = format_line_label(center, market)

    lines = []

    lines.append(f"**{display}**")
    lines.append(f"`Center {center_label} / {center_source}`")
    lines.append(f"`市場感: {bias}`")
    lines.append("")

    lines.append("🔥 **注目ライン**")
    if focus_lines:
        lines.extend(focus_lines)
    else:
        lines.append("`なし`")

    lines.append("")
    lines.append("**Grid**")

    inserted_center = False

    for row in grid_rows:
        line_value = safe_float(row.get("line_value"))

        if not inserted_center and line_value is not None and line_value > center:
            lines.append("")
            lines.append(f"🔴 **現在価格 {center_label}**")
            lines.append("")
            inserted_center = True

        lines.append(render_row(row, total_m, center, market, with_icon=False))

    if not inserted_center:
        lines.append("")
        lines.append(f"🔴 **現在価格 {center_label}**")

    lines.append("")
    lines.append("`注:`")
    lines.append("`% = 予測市場のYES価格ベースの市場確率`")
    lines.append("`薄 / 中 / 厚 = 市場データの厚み。確率の高さとは別`")

    return "\n".join(lines)


def build_message_chunks(markets_config: List[Dict[str, Any]], aggregated: Dict[Tuple[str, str, str], Dict[str, Any]]) -> List[str]:
    by_key: Dict[str, List[Dict[str, Any]]] = {}

    for row in aggregated.values():
        by_key.setdefault(row["market_key"], []).append(row)

    header = "\n".join([
        "📊 **Market Line Radar**",
        f"`{utc_now_text()}`",
    ])

    chunks: List[str] = []
    current = header

    for market in markets_config:
        key = market.get("key")
        block = build_group_block(market, by_key.get(key, []))

        candidate = current + "\n\n" + block

        if len(candidate) <= DISCORD_MESSAGE_LIMIT - 80:
            current = candidate
        else:
            chunks.append(current)
            current = header + "\n\n" + block

            if len(current) > DISCORD_MESSAGE_LIMIT - 80:
                current = current[:DISCORD_MESSAGE_LIMIT - 80]

    if current.strip():
        chunks.append(current)

    return chunks


# ============================================================
# Main
# ============================================================

def run_once() -> None:
    print("[INFO] Loading markets.json...")
    markets_config = load_market_config()

    if not markets_config:
        discord_send("⚠️ **Market Line Radar Bot**\nNo enabled markets in markets.json.")
        return

    print(f"[INFO] Enabled markets: {len(markets_config)}")

    print("[INFO] Fetching Polymarket...")
    polymarket_items = fetch_polymarket_markets(markets_config)
    print(f"[INFO] Polymarket target items: {len(polymarket_items)}")

    print("[INFO] Fetching Kalshi...")
    kalshi_items = fetch_kalshi_markets(markets_config)
    print(f"[INFO] Kalshi target items: {len(kalshi_items)}")

    items = polymarket_items + kalshi_items

    if not items:
        discord_send("⚠️ **Market Line Radar Bot**\nNo target markets found.")
        return

    aggregated = aggregate_items(items)

    counts: Dict[str, int] = {}

    for item in items:
        key = item["market_key"]
        direction = item.get("direction", "unknown")
        count_key = f"{key}:{direction}"
        counts[count_key] = counts.get(count_key, 0) + 1

    print("[INFO] Counts:", counts)

    messages = build_message_chunks(markets_config, aggregated)
    print(f"[INFO] Discord message chunks: {len(messages)}")

    discord_send_many(messages)

    print("[INFO] Finished.")


if __name__ == "__main__":
    run_once()
