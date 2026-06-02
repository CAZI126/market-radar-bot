# main.py

import os
import re
import json
import time
import math
import statistics
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from config import (
    DISCORD_WEBHOOK_URL,
    POLYMARKET_SEARCH_URL,
    HYPERLIQUID_INFO_URL,
    USER_AGENT,
    MARKETS_JSON_PATH,
    DISCORD_MESSAGE_LIMIT,
)

load_dotenv()

# ============================================================
# Paths / Settings
# ============================================================

POSITION_STATE_PATH = "position_state.json"
SIGNAL_LOG_PATH = "market_signal_log.jsonl"

POLYMARKET_MARKET_BY_SLUG_URL = "https://gamma-api.polymarket.com/markets/slug/{}"

SUPPORTED_MARKET_KEYS = ["oil", "wti", "btc", "bitcoin"]

MAX_RAW_MARKETS_PER_QUERY = int(os.getenv("MAX_RAW_MARKETS_PER_QUERY", "80"))
MAX_DETAIL_FETCH = int(os.getenv("MAX_DETAIL_FETCH", "30"))
MAX_LINES_PER_SIDE = int(os.getenv("MAX_LINES_PER_SIDE", "6"))
MAX_FOCUS_LINES = int(os.getenv("MAX_FOCUS_LINES", "5"))

MAX_DAYS_TO_EXPIRY = int(os.getenv("MAX_DAYS_TO_EXPIRY", "7"))

MIN_LIQUIDITY_FOR_HIGH = float(os.getenv("MIN_LIQUIDITY_FOR_HIGH", "1000"))
MIN_LIQUIDITY_FOR_MEDIUM = float(os.getenv("MIN_LIQUIDITY_FOR_MEDIUM", "300"))

TOUCH_HIGH_THRESHOLD = 0.60
BREAKOUT_LOW_THRESHOLD = 0.20

HYPERLIQUID_ALL_MIDS_CACHE: Dict[str, Dict[str, Any]] = {}
HYPERLIQUID_META_CACHE: Dict[str, Any] = {}
POLYMARKET_DETAIL_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}

VOLUME_KEYS = [
    "volume",
    "volumeNum",
    "volume_num",
    "volume24hr",
    "volume24hrClob",
    "volumeClob",
    "volume1wk",
    "volume1mo",
    "volume1yr",
]

LIQUIDITY_KEYS = [
    "liquidity",
    "liquidityNum",
    "liquidity_num",
    "liquidityClob",
]

SPREAD_KEYS = ["spread"]

PRICE_KEYS = [
    "outcomePrices",
    "lastTradePrice",
    "last_trade_price",
    "bestAsk",
    "bestBid",
    "price",
    "spread",
]


# ============================================================
# Basic utils
# ============================================================

def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def today_utc_date() -> date:
    return datetime.now(timezone.utc).date()


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
        if not s:
            return default
        try:
            return float(s)
        except ValueError:
            return default

    return default


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


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


def format_price(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def format_line_label(value: float, step: int = 1) -> str:
    if value >= 10_000:
        k = value / 1000
        if abs(k - round(k)) < 0.01:
            return f"${int(round(k))}k"
        return f"${k:g}k"

    if step >= 1 and abs(value - round(value)) < 0.01:
        return f"${int(round(value))}"

    return f"${value:g}"


def round_to_step(value: float, step: int) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def parse_date_yyyy_mm_dd(value: str) -> Optional[date]:
    value = str(value or "").strip()
    if not value:
        return None

    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def days_until_date(date_text: str) -> Optional[int]:
    d = parse_date_yyyy_mm_dd(date_text)
    if d is None:
        return None
    return (d - today_utc_date()).days


def http_get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Any:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def http_post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> Any:
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


# ============================================================
# Config / position
# ============================================================

def load_markets_json() -> Dict[str, Any]:
    if not os.path.exists(MARKETS_JSON_PATH):
        return {"markets": []}

    with open(MARKETS_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        return {"markets": []}

    if "markets" not in data or not isinstance(data["markets"], list):
        data["markets"] = []

    return data


def default_markets() -> List[Dict[str, Any]]:
    return [
        {
            "key": "oil_wti",
            "display": "💧 Oil - WTI",
            "enabled": True,
            "center_type": "hyperliquid",
            "hyperliquid_coin": "WTIOIL",
            "hyperliquid_coin_candidates": [
                "WTIOIL",
                "WTIOIL-USDC",
                "WTIOILUSDC",
                "xyz:WTIOIL",
                "xyz:WTIOIL-USDC",
                "CL",
                "xyz:CL"
            ],
            "manual_center": 91,
            "grid_step": 1,
            "range_pct": 0.20,
            "search_queries": [
                "WTI",
                "Oil",
                "Crude Oil",
                "WTI above",
                "WTI below",
                "Oil above",
                "Oil below",
                "Crude above",
                "Crude below"
            ],
            "keywords": ["wti", "oil", "crude"],
            "exclude": [
                "brent",
                "gasoline",
                "natural gas",
                "gas price",
                "bitcoin",
                "btc",
                "ethereum",
                "eth",
                "gold",
                "silver"
            ],
        },
        {
            "key": "btc",
            "display": "₿ BTC",
            "enabled": True,
            "center_type": "hyperliquid",
            "hyperliquid_coin": "BTC",
            "hyperliquid_coin_candidates": [
                "BTC",
                "BTCUSDC",
                "BTCUSD"
            ],
            "manual_center": 74000,
            "grid_step": 1000,
            "range_pct": 0.15,
            "search_queries": ["BTC", "Bitcoin"],
            "keywords": ["btc", "bitcoin"],
            "exclude": ["ethereum", "eth", "oil", "wti", "gold", "silver"],
        },
    ]


def is_supported_market(market: Dict[str, Any]) -> bool:
    key = str(market.get("key", "")).lower()
    display = str(market.get("display", "")).lower()
    keywords = " ".join(str(x).lower() for x in market.get("keywords", []) or [])
    blob = f"{key} {display} {keywords}"

    return any(x in blob for x in SUPPORTED_MARKET_KEYS)


def load_enabled_supported_markets() -> List[Dict[str, Any]]:
    data = load_markets_json()
    markets = data.get("markets", [])

    enabled = [
        m for m in markets
        if isinstance(m, dict)
        and m.get("enabled", True)
        and is_supported_market(m)
    ]

    if enabled:
        return enabled

    return default_markets()


def load_position_state() -> Dict[str, Any]:
    if not os.path.exists(POSITION_STATE_PATH):
        return {}

    try:
        with open(POSITION_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception as e:
        print("[WARN] position_state.json read failed:", e)

    return {}


def position_enabled_for_market(market: Dict[str, Any]) -> bool:
    state = load_position_state()

    if not state.get("enabled", True):
        return False

    state_market_key = str(state.get("market_key", "")).strip()
    market_key = str(market.get("key", "")).strip()

    if not state_market_key:
        return True

    return state_market_key == market_key


def get_position_side(market: Dict[str, Any]) -> str:
    if not position_enabled_for_market(market):
        return ""

    state = load_position_state()
    return str(state.get("side", "")).strip().lower()


def get_entry_price(market: Dict[str, Any]) -> Optional[float]:
    if not position_enabled_for_market(market):
        return None

    state = load_position_state()
    return safe_float(state.get("entry_price"))


def get_danger_price(market: Dict[str, Any]) -> Optional[float]:
    if not position_enabled_for_market(market):
        return None

    state = load_position_state()
    return safe_float(state.get("danger_price"))


def get_cme_last_price(market: Dict[str, Any]) -> Optional[float]:
    if not position_enabled_for_market(market):
        return None

    state = load_position_state()
    return safe_float(state.get("cme_last_price"))


def get_position_memo(market: Dict[str, Any]) -> str:
    if not position_enabled_for_market(market):
        return ""

    state = load_position_state()
    return str(state.get("memo", "")).strip()


# ============================================================
# Market helpers
# ============================================================

def market_key_text(market: Dict[str, Any]) -> str:
    key = str(market.get("key", "")).lower()
    display = str(market.get("display", "")).lower()
    keywords = " ".join(str(x).lower() for x in market.get("keywords", []) or [])
    return f"{key} {display} {keywords}"


def is_oil_market(market: Dict[str, Any]) -> bool:
    blob = market_key_text(market)
    return "oil" in blob or "wti" in blob or "crude" in blob


def is_btc_market(market: Dict[str, Any]) -> bool:
    blob = market_key_text(market)
    return "btc" in blob or "bitcoin" in blob


def market_price_range(market: Dict[str, Any]) -> Tuple[float, float]:
    manual = safe_float(market.get("manual_center"))

    if is_btc_market(market):
        return 10_000, 1_000_000

    if is_oil_market(market):
        return 40, 200

    if manual and manual > 0:
        return manual * 0.5, manual * 1.5

    return 0, 999999999


def normalize_possible_market_price(raw_price: Any, market: Dict[str, Any]) -> Optional[float]:
    p = safe_float(raw_price)

    if p is None or p <= 0:
        return None

    manual = safe_float(market.get("manual_center"))
    range_pct = safe_float(market.get("range_pct"), 0.20) or 0.20

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
    ]

    if manual is not None and manual > 0:
        low = manual * max(0.01, 1 - max(range_pct, 0.15))
        high = manual * (1 + max(range_pct, 0.15))

        if is_oil_market(market):
            low = 40
            high = 200

        for x in candidates:
            if low <= x <= high:
                return x

    low_abs, high_abs = market_price_range(market)

    for x in candidates:
        if low_abs <= x <= high_abs:
            return x

    return None


# ============================================================
# Hyperliquid price
# ============================================================

def fetch_hyperliquid_all_mids(dex: str = "") -> Optional[Dict[str, Any]]:
    cache_key = dex or "__default__"

    if cache_key in HYPERLIQUID_ALL_MIDS_CACHE:
        return HYPERLIQUID_ALL_MIDS_CACHE[cache_key]

    payload: Dict[str, Any] = {"type": "allMids"}

    if dex:
        payload["dex"] = dex

    try:
        data = http_post_json(
            HYPERLIQUID_INFO_URL,
            payload=payload,
            timeout=15,
        )

        if isinstance(data, dict):
            HYPERLIQUID_ALL_MIDS_CACHE[cache_key] = data
            return data

    except Exception as e:
        print(f"[WARN] Hyperliquid allMids failed dex={dex or 'default'}:", e)

    return None


def fetch_hyperliquid_meta_and_ctxs(dex: str = "") -> Optional[Any]:
    cache_key = dex or "__default__"

    if cache_key in HYPERLIQUID_META_CACHE:
        return HYPERLIQUID_META_CACHE[cache_key]

    payload: Dict[str, Any] = {"type": "metaAndAssetCtxs"}

    if dex:
        payload["dex"] = dex

    try:
        data = http_post_json(
            HYPERLIQUID_INFO_URL,
            payload=payload,
            timeout=15,
        )

        HYPERLIQUID_META_CACHE[cache_key] = data
        return data

    except Exception as e:
        print(f"[WARN] Hyperliquid metaAndAssetCtxs failed dex={dex or 'default'}:", e)

    return None


def key_norm(value: Any) -> str:
    return str(value or "").upper().replace("-", "").replace("/", "").replace(":", "").replace("_", "")


def is_wti_oil_key(key: Any) -> bool:
    n = key_norm(key)
    return (
        "WTIOIL" in n
        or n in ["WTI", "WTIUSDC", "WTIUSD"]
    )


def is_cl_key(key: Any) -> bool:
    n = key_norm(key)
    return n in ["CL", "CLUSDC", "CLUSD", "XYZCL"]


def is_btc_key(key: Any) -> bool:
    n = key_norm(key)
    return (
        n in ["BTC", "BTCUSDC", "BTCUSD"]
        or n.endswith("BTCUSDC")
        or n.endswith("BTCUSD")
    )


def get_price_from_all_mids_by_keys(
    data: Optional[Dict[str, Any]],
    keys: List[str],
    market: Dict[str, Any],
    label: str,
) -> Optional[float]:
    if not isinstance(data, dict):
        return None

    key_map = {key_norm(k): k for k in data.keys()}

    for target in keys:
        target_norm = key_norm(target)

        if target_norm in key_map:
            real_key = key_map[target_norm]
            price = normalize_possible_market_price(data.get(real_key), market)

            if price is not None:
                print(f"[INFO] {label} exact key={real_key} price={price}")
                return price

    return None


def get_price_from_all_mids_predicate(
    data: Optional[Dict[str, Any]],
    predicate,
    market: Dict[str, Any],
    label: str,
) -> Optional[float]:
    if not isinstance(data, dict):
        return None

    matches = []

    for k, v in data.items():
        if not predicate(k):
            continue

        price = normalize_possible_market_price(v, market)

        if price is not None:
            matches.append((str(k), price))

    if not matches:
        return None

    matches = sorted(matches, key=lambda x: x[0])
    key, price = matches[0]
    print(f"[INFO] {label} selected key={key} price={price}")
    return price


def get_price_from_meta_exact_wtioil(dex: str, market: Dict[str, Any]) -> Optional[float]:
    data = fetch_hyperliquid_meta_and_ctxs(dex)

    if not isinstance(data, list) or len(data) < 2:
        return None

    meta = data[0]
    ctxs = data[1]

    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        return None

    universe = meta.get("universe")

    if not isinstance(universe, list):
        return None

    possible = []

    for idx, asset in enumerate(universe):
        if not isinstance(asset, dict):
            continue

        name = str(asset.get("name") or "").upper()
        coin = str(asset.get("coin") or "").upper()
        symbol = str(asset.get("symbol") or "").upper()
        full = str(asset.get("fullName") or asset.get("displayName") or "").upper()

        joined = key_norm(" ".join([name, coin, symbol, full]))

        is_target = (
            "WTIOIL" in joined
            or name == "WTIOIL"
            or coin == "WTIOIL"
            or symbol == "WTIOIL"
        )

        if not is_target:
            continue

        if idx >= len(ctxs) or not isinstance(ctxs[idx], dict):
            continue

        ctx = ctxs[idx]

        for px_key in ["midPx", "markPx", "oraclePx", "lastPx", "price"]:
            if px_key not in ctx:
                continue

            price = normalize_possible_market_price(ctx.get(px_key), market)

            if price is not None:
                possible.append((idx, name or coin or symbol or "WTIOIL", px_key, price))
                break

    if not possible:
        return None

    idx, asset_name, px_key, price = possible[0]
    print(f"[INFO] OIL realtime meta selected dex={dex or 'default'} asset={asset_name} px_key={px_key} price={price}")
    return price


def get_price_from_meta_cl_fallback(dex: str, market: Dict[str, Any]) -> Optional[float]:
    data = fetch_hyperliquid_meta_and_ctxs(dex)

    if not isinstance(data, list) or len(data) < 2:
        return None

    meta = data[0]
    ctxs = data[1]

    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        return None

    universe = meta.get("universe")

    if not isinstance(universe, list):
        return None

    possible = []

    for idx, asset in enumerate(universe):
        if not isinstance(asset, dict):
            continue

        name = str(asset.get("name") or "").upper()
        coin = str(asset.get("coin") or "").upper()
        symbol = str(asset.get("symbol") or "").upper()

        if name != "CL" and coin != "CL" and symbol != "CL":
            continue

        if idx >= len(ctxs) or not isinstance(ctxs[idx], dict):
            continue

        ctx = ctxs[idx]

        for px_key in ["midPx", "markPx", "oraclePx", "lastPx", "price"]:
            if px_key not in ctx:
                continue

            price = normalize_possible_market_price(ctx.get(px_key), market)

            if price is not None:
                possible.append((idx, name or coin or symbol or "CL", px_key, price))
                break

    if not possible:
        return None

    idx, asset_name, px_key, price = possible[0]
    print(f"[WARN] OIL CL fallback meta selected dex={dex or 'default'} asset={asset_name} px_key={px_key} price={price}")
    return price


def fetch_oil_realtime_price_from_hyperliquid(market: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """
    Oilだけ特別扱い。
    WTIOIL-USDC / dex=xyz のリアルタイム価格を最優先。
    CLは最後の最後。
    """

    wtioil_keys = [
        "WTIOIL",
        "WTIOIL-USDC",
        "WTIOILUSDC",
        "xyz:WTIOIL",
        "xyz:WTIOIL-USDC",
        "XYZWTIOIL",
        "XYZWTIOILUSDC",
    ]

    cl_keys = [
        "CL",
        "CL-USDC",
        "CLUSDC",
        "xyz:CL",
        "XYZCL",
    ]

    # 1. dex=xyz allMids から WTIOIL を最優先
    xyz_mids = fetch_hyperliquid_all_mids("xyz")
    price = get_price_from_all_mids_by_keys(
        xyz_mids,
        wtioil_keys,
        market,
        "OIL realtime allMids dex=xyz WTIOIL"
    )
    if price is not None:
        return price, "Hyperliquid WTIOIL realtime"

    price = get_price_from_all_mids_predicate(
        xyz_mids,
        is_wti_oil_key,
        market,
        "OIL realtime allMids dex=xyz WTIOIL predicate"
    )
    if price is not None:
        return price, "Hyperliquid WTIOIL realtime"

    # 2. dex=xyz metaAndAssetCtxs から WTIOIL を探す
    price = get_price_from_meta_exact_wtioil("xyz", market)
    if price is not None:
        return price, "Hyperliquid WTIOIL realtime"

    # 3. default allMids から WTIOIL を探す
    default_mids = fetch_hyperliquid_all_mids("")
    price = get_price_from_all_mids_by_keys(
        default_mids,
        wtioil_keys,
        market,
        "OIL realtime allMids default WTIOIL"
    )
    if price is not None:
        return price, "Hyperliquid WTIOIL realtime"

    price = get_price_from_all_mids_predicate(
        default_mids,
        is_wti_oil_key,
        market,
        "OIL realtime allMids default WTIOIL predicate"
    )
    if price is not None:
        return price, "Hyperliquid WTIOIL realtime"

    # 4. default metaAndAssetCtxs から WTIOIL を探す
    price = get_price_from_meta_exact_wtioil("", market)
    if price is not None:
        return price, "Hyperliquid WTIOIL realtime"

    # 5. ここからCL fallback。ログはWARNにする。
    price = get_price_from_all_mids_by_keys(
        xyz_mids,
        cl_keys,
        market,
        "OIL fallback allMids dex=xyz CL"
    )
    if price is not None:
        return price, "Hyperliquid CL fallback"

    price = get_price_from_meta_cl_fallback("xyz", market)
    if price is not None:
        return price, "Hyperliquid CL fallback"

    price = get_price_from_all_mids_by_keys(
        default_mids,
        cl_keys,
        market,
        "OIL fallback allMids default CL"
    )
    if price is not None:
        return price, "Hyperliquid CL fallback"

    price = get_price_from_meta_cl_fallback("", market)
    if price is not None:
        return price, "Hyperliquid CL fallback"

    manual = safe_float(market.get("manual_center"))
    if manual is not None and manual > 0:
        print(f"[ERROR] OIL realtime price not found. manual fallback={manual}")
        return manual, "Manual fallback"

    return None, "n/a"


def fetch_btc_price_from_hyperliquid(market: Dict[str, Any]) -> Tuple[Optional[float], str]:
    btc_keys = [
        "BTC",
        "BTC-USDC",
        "BTCUSDC",
        "BTCUSD",
    ]

    data = fetch_hyperliquid_all_mids("")
    price = get_price_from_all_mids_by_keys(
        data,
        btc_keys,
        market,
        "BTC allMids default"
    )
    if price is not None:
        return price, "Hyperliquid"

    price = get_price_from_all_mids_predicate(
        data,
        is_btc_key,
        market,
        "BTC allMids default predicate"
    )
    if price is not None:
        return price, "Hyperliquid"

    manual = safe_float(market.get("manual_center"))
    if manual is not None and manual > 0:
        print(f"[WARN] BTC price not found. manual fallback={manual}")
        return manual, "Manual fallback"

    return None, "n/a"


def choose_center(market: Dict[str, Any]) -> Tuple[Optional[float], str]:
    step = int(market.get("grid_step") or 1)
    center_type = str(market.get("center_type", "hyperliquid")).lower()

    if center_type == "manual":
        manual = safe_float(market.get("manual_center"))
        if manual is not None and manual > 0:
            return round_to_step(manual, step), "Manual"

    if is_oil_market(market):
        price, source = fetch_oil_realtime_price_from_hyperliquid(market)
        if price is not None:
            # Oilは内部計算も1ドル丸めでよいが、表示は別で小数表示する
            return round_to_step(price, step), source

    if is_btc_market(market):
        price, source = fetch_btc_price_from_hyperliquid(market)
        if price is not None:
            return round_to_step(price, step), source

    manual = safe_float(market.get("manual_center"))

    if manual is not None and manual > 0:
        return round_to_step(manual, step), "Manual fallback"

    return None, "n/a"


# ============================================================
# Polymarket parsing
# ============================================================

def title_matches_market(title: str, market: Dict[str, Any]) -> bool:
    text = normalize_text(title)

    for bad in market.get("exclude", []) or []:
        bad = str(bad).strip().lower()
        if bad and bad in text:
            return False

    keywords = market.get("keywords", []) or []

    if not keywords:
        if is_btc_market(market):
            keywords = ["btc", "bitcoin"]
        elif is_oil_market(market):
            keywords = ["wti", "oil", "crude"]

    for kw in keywords:
        kw = str(kw).strip().lower()
        if kw and kw in text:
            return True

    return False


def extract_line(title: str, market: Dict[str, Any], center: Optional[float]) -> Tuple[Optional[str], Optional[float]]:
    text = title or ""
    step = int(market.get("grid_step") or 1)
    range_pct = safe_float(market.get("range_pct"), 0.15) or 0.15
    manual_center = safe_float(market.get("manual_center"))

    ref_center = center or manual_center

    candidates: List[float] = []

    for match in re.finditer(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?)", text):
        value = safe_float(match.group(1).replace(",", ""))
        if value is not None:
            candidates.append(value)

    for match in re.finditer(r"\$?\s*\b([0-9]+(?:\.\d+)?)\s*([kKmM])?\b", text):
        value = safe_float(match.group(1))
        suffix = match.group(2)

        if value is None:
            continue

        if suffix:
            if suffix.lower() == "k":
                value *= 1_000
            elif suffix.lower() == "m":
                value *= 1_000_000

        candidates.append(value)

    filtered = []
    low_abs, high_abs = market_price_range(market)

    for value in candidates:
        if 2020 <= value <= 2035:
            continue

        if value <= 0:
            continue

        if not (low_abs <= value <= high_abs):
            continue

        if ref_center is not None and ref_center > 0:
            low = ref_center * (1 - range_pct)
            high = ref_center * (1 + range_pct)

            if not (low <= value <= high):
                continue

        if step > 0:
            nearest = round_to_step(value, step)
            tolerance = max(1.0, step * 0.12)
            if abs(value - nearest) > tolerance:
                continue

        filtered.append(value)

    if not filtered:
        return None, None

    if center is not None:
        value = sorted(filtered, key=lambda x: abs(x - center))[0]
    else:
        value = filtered[0]

    return format_line_label(value, step), value


def detect_direction(title: str, line_value: Optional[float], center: Optional[float]) -> str:
    text = normalize_text(title)

    down_patterns = [
        "below", "under", "less than", "at or below",
        "close below", "closes below", "dip to", "dip below",
        "lower than", "fall below", "falls below",
        "drop below", "drops below", "crash to", "collapse to",
        "go below",
    ]

    over_patterns = [
        "above", "over", "greater than", "at or above",
        "close above", "closes above", "higher than",
        "exceed", "exceeds", "go above", "be above",
    ]

    for p in down_patterns:
        if p in text:
            return "down"

    for p in over_patterns:
        if p in text:
            return "over"

    ambiguous_patterns = ["hit", "hits", "reach", "reaches", "touch", "touches"]

    if any(p in text for p in ambiguous_patterns):
        if center is not None and line_value is not None:
            if line_value >= center:
                return "over"
            return "down"

    return "unknown"


def get_yes_probability(raw: Dict[str, Any]) -> Optional[float]:
    outcomes = parse_jsonish(raw.get("outcomes"))
    prices = parse_jsonish(raw.get("outcomePrices"))

    if isinstance(outcomes, list) and isinstance(prices, list):
        for i, outcome in enumerate(outcomes):
            if str(outcome).strip().lower() == "yes" and i < len(prices):
                p = safe_float(prices[i])
                if p is not None:
                    if p > 1:
                        p = p / 100
                    return max(0.0, min(1.0, p))

    for key in ["lastTradePrice", "last_trade_price", "bestAsk", "bestBid", "price"]:
        p = safe_float(raw.get(key))
        if p is not None:
            if p > 1:
                p = p / 100
            return max(0.0, min(1.0, p))

    return None


def pick_first_number(raw: Dict[str, Any], keys: List[str]) -> Tuple[Optional[float], Optional[str]]:
    for key in keys:
        if key in raw:
            value = safe_float(raw.get(key))
            if value is not None:
                return value, key
    return None, None


def fetch_polymarket_market_detail_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    slug = str(slug or "").strip()

    if not slug:
        return None

    if slug in POLYMARKET_DETAIL_CACHE:
        return POLYMARKET_DETAIL_CACHE[slug]

    url = POLYMARKET_MARKET_BY_SLUG_URL.format(slug)

    try:
        data = http_get_json(url, timeout=15)

        if isinstance(data, dict):
            POLYMARKET_DETAIL_CACHE[slug] = data
            return data

    except Exception as e:
        print(f"[WARN] Polymarket slug detail failed slug={slug}: {e}")

    POLYMARKET_DETAIL_CACHE[slug] = None
    return None


def merge_raw_and_detail(raw: Dict[str, Any], detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(raw)

    if isinstance(detail, dict):
        merged.update(detail)

        for key in [
            "question", "title", "name", "slug",
            "outcomes", "outcomePrices", "lastTradePrice",
            "bestAsk", "bestBid", "spread",
            "groupItemTitle", "endDate", "endDateIso",
            "eventStartTime", "events",
        ]:
            if key not in merged and key in raw:
                merged[key] = raw[key]

    return merged


# ============================================================
# Group / expiry
# ============================================================

def extract_event_date_text(raw: Dict[str, Any]) -> str:
    for key in ["endDateIso", "endDate", "eventStartTime", "gameStartTime", "startDateIso", "startDate"]:
        value = raw.get(key)
        if value:
            return str(value)[:10]
    return ""


def is_expiry_allowed(date_text: str) -> bool:
    if MAX_DAYS_TO_EXPIRY <= 0:
        return True

    days = days_until_date(date_text)

    if days is None:
        return True

    if days < 0:
        return False

    return days <= MAX_DAYS_TO_EXPIRY


def extract_event_title_text(raw: Dict[str, Any]) -> str:
    events = parse_jsonish(raw.get("events"))

    if isinstance(events, list) and events:
        first = events[0]

        if isinstance(first, dict):
            for key in ["title", "ticker", "slug"]:
                value = first.get(key)
                if value:
                    return str(value).strip()
        else:
            return str(first).strip()

    return ""


def build_market_group_key(raw: Dict[str, Any], market: Dict[str, Any]) -> Tuple[str, str]:
    market_key = str(market.get("key", "unknown"))
    date_text = extract_event_date_text(raw)
    event_title = extract_event_title_text(raw)

    if date_text:
        group_key = f"{market_key}|{date_text}"
        group_label = date_text
        return group_key, group_label

    if event_title:
        normalized_title = normalize_text(event_title)
        group_key = f"{market_key}|no_date|{normalized_title}"
        group_label = "期限不明"
        return group_key, group_label

    q = str(raw.get("question") or raw.get("title") or raw.get("slug") or "")
    q = re.sub(r"\$?\d+(?:,\d{3})*(?:\.\d+)?[kKmM]?", "<LINE>", q)
    q = normalize_text(q)

    group_key = f"{market_key}|no_date|{q[:80]}"
    group_label = "期限不明"
    return group_key, group_label


def choose_best_group(rows: List[Dict[str, Any]], center: float) -> Tuple[List[Dict[str, Any]], str]:
    if not rows:
        return [], ""

    groups: Dict[str, List[Dict[str, Any]]] = {}

    for row in rows:
        group_key = str(row.get("market_group_key") or "__ungrouped__")
        groups.setdefault(group_key, []).append(row)

    if len(groups) == 1:
        only_rows = list(groups.values())[0]
        label = str(only_rows[0].get("market_group_label") or "")
        return only_rows, label

    scored = []

    for group_key, group_rows in groups.items():
        total_n = sum(safe_float(r.get("effective_n"), 5.0) or 5.0 for r in group_rows)
        line_count = len(group_rows)
        strength_score = sum(strength_rank(str(r.get("data_strength") or "低")) for r in group_rows)

        distances = [
            abs((safe_float(r.get("line_value")) or center) - center)
            for r in group_rows
            if safe_float(r.get("line_value")) is not None
        ]

        nearest_distance = min(distances) if distances else 999999

        score = (
            total_n * 1.0
            + line_count * 20.0
            + strength_score * 10.0
            + math.exp(-nearest_distance / max(center * 0.01, 1)) * 50.0
        )

        label = str(group_rows[0].get("market_group_label") or "")
        scored.append((score, group_key, group_rows, label))

    scored = sorted(scored, key=lambda x: x[0], reverse=True)
    best_score, best_key, best_rows, best_label = scored[0]

    print(f"[INFO] Selected group={best_key} score={best_score:.1f} rows={len(best_rows)} label={best_label}")

    return best_rows, best_label


# ============================================================
# Quality / signal
# ============================================================

def effective_sample_size(
    volume: Optional[float],
    liquidity: Optional[float],
    spread: Optional[float],
) -> float:
    volume = volume or 0
    liquidity = liquidity or 0

    if volume > 0 and liquidity > 0:
        n_volume = max(volume / 100.0, 1.0)
        n_liquidity = max(liquidity / 50.0, 1.0)
        n = math.sqrt(n_volume * n_liquidity)
        return max(5.0, min(300.0, n))

    if volume > 0:
        n = math.sqrt(max(volume / 100.0, 1.0) * 5.0)
        return max(5.0, min(120.0, n))

    if liquidity > 0:
        n = math.sqrt(max(liquidity / 50.0, 1.0) * 5.0)
        return max(5.0, min(120.0, n))

    if spread is not None:
        if spread <= 0.02:
            return 120.0
        if spread <= 0.05:
            return 60.0
        if spread <= 0.10:
            return 25.0
        return 8.0

    return 5.0


def error_margin_95(p: Optional[float], n: float) -> Optional[float]:
    if p is None or n <= 0:
        return None

    se = math.sqrt(max(0.0, p * (1.0 - p)) / n)
    return 1.96 * se


def data_strength_from_error_and_liquidity(
    error: Optional[float],
    liquidity: Optional[float],
) -> str:
    liquidity = liquidity or 0

    if error is None:
        return "低"

    if error <= 0.10 and liquidity >= MIN_LIQUIDITY_FOR_HIGH:
        return "高"

    if error <= 0.20 and liquidity >= MIN_LIQUIDITY_FOR_MEDIUM:
        return "中"

    return "低"


def strength_rank(strength: str) -> int:
    if strength == "高":
        return 3
    if strength == "中":
        return 2
    return 1


def quality_from_market(raw: Dict[str, Any], p: Optional[float]) -> Dict[str, Any]:
    volume, _ = pick_first_number(raw, VOLUME_KEYS)
    liquidity, _ = pick_first_number(raw, LIQUIDITY_KEYS)
    spread, _ = pick_first_number(raw, SPREAD_KEYS)

    n = effective_sample_size(volume, liquidity, spread)
    err = error_margin_95(p, n)
    strength = data_strength_from_error_and_liquidity(err, liquidity)

    return {
        "volume": volume,
        "liquidity": liquidity,
        "spread": spread,
        "effective_n": n,
        "error_95": err,
        "data_strength": strength,
    }


def aggregate_strength(probability: float, effective_n_total: float, liquidity_total: Optional[float]) -> Tuple[Optional[float], str]:
    effective_n_total = max(5.0, min(500.0, effective_n_total))
    error = error_margin_95(probability, effective_n_total)
    strength = data_strength_from_error_and_liquidity(error, liquidity_total)
    return error, strength


def signal_word(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return ""

    p = safe_float(row.get("probability"))
    strength = str(row.get("data_strength") or "低")

    if p is None:
        return "参考程度"

    if strength == "低":
        return "参考程度"

    if strength == "中":
        if p >= 0.80:
            return "意識"
        if p <= 0.20:
            return "否定"
        return ""

    if strength == "高":
        if p >= 0.80:
            return "強く意識"
        if p >= 0.60:
            return "意識"
        if p <= 0.20:
            return "強く否定"
        if p <= 0.40:
            return "否定"
        return ""

    return "参考程度"


def row_signal_text(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return "n/a"

    label = str(row.get("line_label") or "n/a")
    signal = signal_word(row)

    if signal:
        return f"{label} / {signal}"

    return label


def is_positive_signal(row: Optional[Dict[str, Any]]) -> bool:
    return signal_word(row) in ["強く意識", "意識"]


def is_negative_signal(row: Optional[Dict[str, Any]]) -> bool:
    return signal_word(row) in ["否定", "強く否定"]


# ============================================================
# Fetch Polymarket
# ============================================================

def market_search_queries(market: Dict[str, Any]) -> List[str]:
    queries = []

    for q in market.get("search_queries", []) or []:
        s = str(q).strip()
        if s and s not in queries:
            queries.append(s)

    if not queries:
        if is_btc_market(market):
            queries = ["BTC", "Bitcoin"]
        elif is_oil_market(market):
            queries = ["WTI", "Oil", "Crude Oil"]

    return queries


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
        has_price = any(k in obj for k in PRICE_KEYS)

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


def fetch_polymarket_search_raw(market: Dict[str, Any]) -> List[Dict[str, Any]]:
    results = []

    for q in market_search_queries(market):
        params = {
            "q": q,
            "events_status": "active",
            "limit_per_type": 20,
            "page": 1,
            "keep_closed_markets": 0,
            "optimized": "true",
        }

        try:
            data = http_get_json(POLYMARKET_SEARCH_URL, params=params, timeout=20)
        except Exception as e:
            print(f"[ERROR] Polymarket search failed market={market.get('key')} q={q}: {e}")
            continue

        raw_markets = extract_markets_from_search_response(data)
        raw_markets = raw_markets[:MAX_RAW_MARKETS_PER_QUERY]

        print(f"[INFO] Polymarket search market={market.get('key')} q={q}: raw markets={len(raw_markets)}")
        results.extend(raw_markets)

        time.sleep(0.2)

    return results


def raw_to_item(
    raw: Dict[str, Any],
    market: Dict[str, Any],
    center: Optional[float],
    detail: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    merged = merge_raw_and_detail(raw, detail)

    title = (
        merged.get("question")
        or merged.get("title")
        or merged.get("name")
        or merged.get("slug")
        or ""
    )

    if not title:
        return None

    if not title_matches_market(title, market):
        return None

    date_text = extract_event_date_text(merged)

    if date_text and not is_expiry_allowed(date_text):
        return None

    line_label, line_value = extract_line(title, market, center)

    if not line_label:
        return None

    p = get_yes_probability(merged)

    if p is None:
        return None

    direction = detect_direction(title, line_value, center)
    quality = quality_from_market(merged, p)
    group_key, group_label = build_market_group_key(merged, market)

    market_id = str(
        merged.get("id")
        or merged.get("conditionId")
        or merged.get("slug")
        or title
    )

    return {
        "source": "Polymarket",
        "market_id": market_id,
        "title": title,
        "market_key": str(market.get("key", "")),
        "line_label": line_label,
        "line_value": line_value,
        "direction": direction,
        "probability": p,
        "slug": merged.get("slug"),
        "volume": quality.get("volume"),
        "liquidity": quality.get("liquidity"),
        "spread": quality.get("spread"),
        "effective_n": quality.get("effective_n"),
        "error_95": quality.get("error_95"),
        "data_strength": quality.get("data_strength"),
        "market_group_key": group_key,
        "market_group_label": group_label,
        "event_date": date_text,
    }


def fetch_polymarket_items(market: Dict[str, Any], center: Optional[float]) -> List[Dict[str, Any]]:
    raw_markets = fetch_polymarket_search_raw(market)

    candidates = []
    seen_raw = set()

    for raw in raw_markets:
        title = (
            raw.get("question")
            or raw.get("title")
            or raw.get("name")
            or raw.get("slug")
            or ""
        )

        if not title or not title_matches_market(title, market):
            continue

        line_label, line_value = extract_line(title, market, center)

        if not line_label:
            continue

        slug = str(raw.get("slug") or title)
        key = (slug, line_label)

        if key in seen_raw:
            continue

        seen_raw.add(key)
        candidates.append(raw)

    print(f"[INFO] {market.get('key')} candidates before detail limit: {len(candidates)}")

    candidates = candidates[:MAX_DETAIL_FETCH]

    print(f"[INFO] {market.get('key')} candidates after detail limit: {len(candidates)}")

    items = []
    seen_items = set()

    for idx, raw in enumerate(candidates, start=1):
        if idx == 1 or idx % 10 == 0 or idx == len(candidates):
            print(f"[INFO] Fetching slug details {market.get('key')}... {idx}/{len(candidates)}")

        slug = str(raw.get("slug") or "").strip()
        detail = fetch_polymarket_market_detail_by_slug(slug) if slug else None

        item = raw_to_item(raw, market, center, detail)

        if not item:
            continue

        dedupe_key = (
            item.get("market_id"),
            item.get("direction"),
            item.get("line_label"),
            item.get("market_group_key"),
        )

        if dedupe_key in seen_items:
            continue

        seen_items.add(dedupe_key)
        items.append(item)

        time.sleep(0.08)

    return items


# ============================================================
# Aggregate
# ============================================================

def aggregate_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}

    for item in items:
        key = (
            str(item.get("direction", "unknown")),
            str(item.get("line_label")),
            str(item.get("market_group_key") or "__ungrouped__"),
        )
        grouped.setdefault(key, []).append(item)

    aggregated = []

    for key, rows in grouped.items():
        weighted_sum = 0.0
        weight_total = 0.0
        probabilities = []

        volume_total = 0.0
        liquidity_total = 0.0
        spread_values = []
        effective_n_total = 0.0

        for r in rows:
            p = safe_float(r.get("probability"))
            n = safe_float(r.get("effective_n"), 5.0) or 5.0

            if p is None:
                continue

            probabilities.append(p)
            weighted_sum += p * n
            weight_total += n
            effective_n_total += n

            volume_total += safe_float(r.get("volume"), 0.0) or 0.0
            liquidity_total += safe_float(r.get("liquidity"), 0.0) or 0.0

            s = safe_float(r.get("spread"))
            if s is not None:
                spread_values.append(s)

        if not probabilities:
            continue

        probability = weighted_sum / weight_total if weight_total > 0 else statistics.mean(probabilities)
        representative = rows[0]

        error, strength = aggregate_strength(
            probability=probability,
            effective_n_total=effective_n_total,
            liquidity_total=liquidity_total,
        )

        aggregated.append({
            "market_key": representative.get("market_key"),
            "direction": key[0],
            "line_label": key[1],
            "market_group_key": key[2],
            "market_group_label": representative.get("market_group_label") or "",
            "event_date": representative.get("event_date") or "",
            "line_value": representative.get("line_value"),
            "probability": probability,
            "probabilities": probabilities,
            "count": len(rows),
            "title": representative.get("title") or "",
            "volume": volume_total if volume_total > 0 else None,
            "liquidity": liquidity_total if liquidity_total > 0 else None,
            "spread": statistics.mean(spread_values) if spread_values else None,
            "effective_n": max(5.0, min(500.0, effective_n_total)),
            "error_95": error,
            "data_strength": strength,
        })

    return aggregated


# ============================================================
# Reading helpers
# ============================================================

def sort_rows_by_line(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: r.get("line_value") if r.get("line_value") is not None else 999999999
    )


def prepare_grid_rows(rows: List[Dict[str, Any]], center: float) -> List[Dict[str, Any]]:
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

    return sort_rows_by_line(lower_keep + upper_keep)


def split_rows_by_side(rows: List[Dict[str, Any]], center: float) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    lower_rows = []
    upper_rows = []

    for row in rows:
        lv = safe_float(row.get("line_value"))
        p = safe_float(row.get("probability"))

        if lv is None or p is None:
            continue

        direction = str(row.get("direction") or "unknown").lower()

        if direction == "over":
            upper_rows.append(row)
        elif direction == "down":
            lower_rows.append(row)
        else:
            if lv >= center:
                upper_rows.append(row)
            else:
                lower_rows.append(row)

    lower_rows = sorted(lower_rows, key=lambda r: safe_float(r.get("line_value")) or -999999)
    upper_rows = sorted(upper_rows, key=lambda r: safe_float(r.get("line_value")) or 999999)

    return lower_rows, upper_rows


def nearest_upper_any(rows: List[Dict[str, Any]], center: float) -> Optional[Dict[str, Any]]:
    candidates = [
        r for r in rows
        if safe_float(r.get("line_value")) is not None
        and safe_float(r.get("line_value")) >= center
    ]

    if not candidates:
        return None

    return min(candidates, key=lambda r: abs((safe_float(r.get("line_value")) or center) - center))


def nearest_lower_any(rows: List[Dict[str, Any]], center: float) -> Optional[Dict[str, Any]]:
    candidates = [
        r for r in rows
        if safe_float(r.get("line_value")) is not None
        and safe_float(r.get("line_value")) <= center
    ]

    if not candidates:
        return None

    return min(candidates, key=lambda r: abs((safe_float(r.get("line_value")) or center) - center))


def nearest_upper_negative(rows: List[Dict[str, Any]], center: float) -> Optional[Dict[str, Any]]:
    candidates = [
        r for r in rows
        if safe_float(r.get("line_value")) is not None
        and safe_float(r.get("line_value")) >= center
        and is_negative_signal(r)
    ]

    if not candidates:
        return None

    return min(candidates, key=lambda r: abs((safe_float(r.get("line_value")) or center) - center))


def nearest_lower_negative(rows: List[Dict[str, Any]], center: float) -> Optional[Dict[str, Any]]:
    candidates = [
        r for r in rows
        if safe_float(r.get("line_value")) is not None
        and safe_float(r.get("line_value")) <= center
        and is_negative_signal(r)
    ]

    if not candidates:
        return None

    return min(candidates, key=lambda r: abs((safe_float(r.get("line_value")) or center) - center))


def next_upper_after(rows: List[Dict[str, Any]], line_value: float) -> Optional[Dict[str, Any]]:
    candidates = [
        r for r in rows
        if safe_float(r.get("line_value")) is not None
        and safe_float(r.get("line_value")) > line_value
    ]

    if not candidates:
        return None

    return min(candidates, key=lambda r: safe_float(r.get("line_value")) or 999999)


def next_lower_after(rows: List[Dict[str, Any]], line_value: float) -> Optional[Dict[str, Any]]:
    candidates = [
        r for r in rows
        if safe_float(r.get("line_value")) is not None
        and safe_float(r.get("line_value")) < line_value
    ]

    if not candidates:
        return None

    return max(candidates, key=lambda r: safe_float(r.get("line_value")) or -999999)


def best_high_upper(rows: List[Dict[str, Any]], center: float) -> Optional[Dict[str, Any]]:
    high_candidates = []

    for r in rows:
        p = safe_float(r.get("probability"))
        lv = safe_float(r.get("line_value"))

        if p is None or lv is None:
            continue

        if lv >= center and p >= TOUCH_HIGH_THRESHOLD:
            high_candidates.append(r)

    if high_candidates:
        return sorted(
            high_candidates,
            key=lambda r: (
                -strength_rank(str(r.get("data_strength") or "低")),
                abs((safe_float(r.get("line_value")) or center) - center),
            )
        )[0]

    return nearest_upper_any(rows, center)


def best_high_lower(rows: List[Dict[str, Any]], center: float) -> Optional[Dict[str, Any]]:
    high_candidates = []

    for r in rows:
        p = safe_float(r.get("probability"))
        lv = safe_float(r.get("line_value"))

        if p is None or lv is None:
            continue

        if lv <= center and p >= TOUCH_HIGH_THRESHOLD:
            high_candidates.append(r)

    if high_candidates:
        return sorted(
            high_candidates,
            key=lambda r: (
                -strength_rank(str(r.get("data_strength") or "低")),
                abs((safe_float(r.get("line_value")) or center) - center),
            )
        )[0]

    return nearest_lower_any(rows, center)


# ============================================================
# Wall logic
# ============================================================

def make_wall_result(
    row_a: Optional[Dict[str, Any]],
    row_b: Optional[Dict[str, Any]],
    wall_type: str = "",
) -> Dict[str, Any]:
    if not row_a or not row_b:
        return {
            "label": "壁判定なし",
            "text": "比較ライン不足",
            "is_strong": False,
        }

    p1 = safe_float(row_a.get("probability"))
    p2 = safe_float(row_b.get("probability"))
    e1 = safe_float(row_a.get("error_95"), 0.25) or 0.25
    e2 = safe_float(row_b.get("error_95"), 0.25) or 0.25

    if p1 is None or p2 is None:
        return {
            "label": "壁判定なし",
            "text": "確率不足",
            "is_strong": False,
        }

    diff = abs(p1 - p2)
    combined_error = e1 + e2

    wall_zone = False

    if is_positive_signal(row_a) and is_negative_signal(row_b):
        wall_zone = True
        if not wall_type:
            wall_type = "上値壁ゾーン"

    if is_negative_signal(row_a) and is_positive_signal(row_b):
        wall_zone = True
        if not wall_type:
            wall_type = "下値壁ゾーン"

    if wall_zone:
        label = "強い壁" if diff > combined_error else "壁の可能性"
        is_strong = diff > combined_error
    elif diff >= 0.30 and diff > combined_error:
        label = "強い壁"
        is_strong = True
    elif diff >= 0.30:
        label = "壁の可能性"
        is_strong = False
    else:
        label = "壁なし"
        is_strong = False

    text = f"{row_signal_text(row_a)} → {row_signal_text(row_b)}"

    return {
        "label": label,
        "text": text,
        "is_strong": is_strong,
        "wall_zone": wall_zone,
        "wall_type": wall_type,
        "diff": diff,
        "combined_error": combined_error,
        "row_a": row_a,
        "row_b": row_b,
    }


def wall_judgement(row_a: Optional[Dict[str, Any]], row_b: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return make_wall_result(row_a, row_b)


def find_upper_wall(rows: List[Dict[str, Any]], center: float) -> Optional[Dict[str, Any]]:
    upper_rows = [
        r for r in rows
        if safe_float(r.get("line_value")) is not None
        and safe_float(r.get("line_value")) >= center
    ]
    upper_rows = sort_rows_by_line(upper_rows)

    best = None
    best_score = -999999.0

    for i in range(len(upper_rows) - 1):
        a = upper_rows[i]
        b = upper_rows[i + 1]

        if not is_positive_signal(a):
            continue

        if not is_negative_signal(b):
            continue

        wall = make_wall_result(a, b, wall_type="上値壁ゾーン")

        if wall.get("label") not in ["強い壁", "壁の可能性"]:
            continue

        a_lv = safe_float(a.get("line_value")) or center
        b_lv = safe_float(b.get("line_value")) or a_lv
        distance = abs(a_lv - center)
        width = abs(b_lv - a_lv)
        diff = safe_float(wall.get("diff"), 0) or 0
        strength_bonus = 1.0 if wall.get("label") == "強い壁" else 0.5

        score = (
            strength_bonus * 100
            + diff * 50
            - distance / max(center * 0.01, 1)
            - width / max(center * 0.02, 1)
        )

        if score > best_score:
            best_score = score
            best = wall

    return best


def find_lower_wall(rows: List[Dict[str, Any]], center: float) -> Optional[Dict[str, Any]]:
    lower_rows = [
        r for r in rows
        if safe_float(r.get("line_value")) is not None
        and safe_float(r.get("line_value")) <= center
    ]
    lower_rows = sort_rows_by_line(lower_rows)

    best = None
    best_score = -999999.0

    for i in range(len(lower_rows) - 1, 0, -1):
        a = lower_rows[i]
        b = lower_rows[i - 1]

        if not is_positive_signal(a):
            continue

        if not is_negative_signal(b):
            continue

        wall = make_wall_result(a, b, wall_type="下値壁ゾーン")

        if wall.get("label") not in ["強い壁", "壁の可能性"]:
            continue

        a_lv = safe_float(a.get("line_value")) or center
        b_lv = safe_float(b.get("line_value")) or a_lv
        distance = abs(a_lv - center)
        width = abs(a_lv - b_lv)
        diff = safe_float(wall.get("diff"), 0) or 0
        strength_bonus = 1.0 if wall.get("label") == "強い壁" else 0.5

        score = (
            strength_bonus * 100
            + diff * 50
            - distance / max(center * 0.01, 1)
            - width / max(center * 0.02, 1)
        )

        if score > best_score:
            best_score = score
            best = wall

    return best


def build_wall_map(rows: List[Dict[str, Any]], center: float) -> Dict[str, Any]:
    return {
        "upper_wall_map": find_upper_wall(rows, center),
        "lower_wall_map": find_lower_wall(rows, center),
        "upper_ref": nearest_upper_any(rows, center),
        "lower_ref": nearest_lower_any(rows, center),
        "upper_negative": nearest_upper_negative(rows, center),
        "lower_negative": nearest_lower_negative(rows, center),
    }


def wall_zone_label(wall: Optional[Dict[str, Any]]) -> str:
    if not wall:
        return "なし"

    row_a = wall.get("row_a")
    row_b = wall.get("row_b")

    if not row_a or not row_b:
        return "なし"

    a_lv = safe_float(row_a.get("line_value"))
    b_lv = safe_float(row_b.get("line_value"))

    if a_lv is None or b_lv is None:
        return "なし"

    low = min(a_lv, b_lv)
    high = max(a_lv, b_lv)
    step = 1000 if high >= 10_000 else 1

    return f"{format_line_label(low, step)}〜{format_line_label(high, step)}"


def build_market_reading_data(rows: List[Dict[str, Any]], center: float) -> Dict[str, Any]:
    lower_rows, upper_rows = split_rows_by_side(rows, center)

    lower_touch = best_high_lower(lower_rows, center)
    upper_touch = best_high_upper(upper_rows, center)

    upper_touch_lv = safe_float(upper_touch.get("line_value")) if upper_touch else None
    lower_touch_lv = safe_float(lower_touch.get("line_value")) if lower_touch else None

    upper_break = next_upper_after(upper_rows, upper_touch_lv) if upper_touch_lv is not None else None
    lower_break = next_lower_after(lower_rows, lower_touch_lv) if lower_touch_lv is not None else None

    lower_touch_p = safe_float(lower_touch.get("probability")) if lower_touch else None
    upper_touch_p = safe_float(upper_touch.get("probability")) if upper_touch else None
    upper_break_p = safe_float(upper_break.get("probability")) if upper_break else None
    lower_break_p = safe_float(lower_break.get("probability")) if lower_break else None

    if lower_touch_lv is not None and upper_touch_lv is not None:
        range_low = min(lower_touch_lv, upper_touch_lv)
        range_high = max(lower_touch_lv, upper_touch_lv)
    elif lower_touch_lv is not None:
        range_low = min(lower_touch_lv, center)
        range_high = max(lower_touch_lv, center)
    elif upper_touch_lv is not None:
        range_low = min(center, upper_touch_lv)
        range_high = max(center, upper_touch_lv)
    else:
        range_low = center
        range_high = center

    step_for_label = 1000 if center >= 10_000 else 1
    range_text = f"{format_line_label(range_low, step_for_label)}〜{format_line_label(range_high, step_for_label)}"

    mode = "中立"

    if upper_touch_p is not None and upper_touch_p >= TOUCH_HIGH_THRESHOLD:
        if upper_break_p is not None and upper_break_p <= BREAKOUT_LOW_THRESHOLD:
            mode = "レンジ上限接近・上抜け弱い"
        else:
            mode = "上値タッチ警戒"

    if lower_touch_p is not None and lower_touch_p >= TOUCH_HIGH_THRESHOLD:
        if lower_break_p is not None and lower_break_p <= BREAKOUT_LOW_THRESHOLD:
            if mode == "中立":
                mode = "レンジ下限接近・下抜け弱い"
            else:
                mode = "レンジ想定が強い"
        elif mode == "中立":
            mode = "下値再訪警戒"

    upper_wall = wall_judgement(upper_touch, upper_break)
    lower_wall = wall_judgement(lower_touch, lower_break)
    wall_map = build_wall_map(rows, center)

    if wall_map.get("upper_wall_map"):
        mode = "上値壁ゾーン"
    elif wall_map.get("lower_wall_map"):
        mode = "下値壁ゾーン"
    elif upper_wall.get("wall_zone") and upper_wall.get("label") in ["強い壁", "壁の可能性"]:
        mode = "上値壁ゾーン"
    elif lower_wall.get("wall_zone") and lower_wall.get("label") in ["強い壁", "壁の可能性"]:
        mode = "下値壁ゾーン"

    return {
        "range_text": range_text,
        "mode": mode,
        "upper_touch": upper_touch,
        "upper_break": upper_break,
        "lower_touch": lower_touch,
        "lower_break": lower_break,
        "upper_touch_p": upper_touch_p,
        "upper_break_p": upper_break_p,
        "lower_touch_p": lower_touch_p,
        "lower_break_p": lower_break_p,
        "upper_wall": upper_wall,
        "lower_wall": lower_wall,
        "upper_wall_map": wall_map.get("upper_wall_map"),
        "lower_wall_map": wall_map.get("lower_wall_map"),
        "upper_ref": wall_map.get("upper_ref"),
        "lower_ref": wall_map.get("lower_ref"),
        "upper_negative": wall_map.get("upper_negative"),
        "lower_negative": wall_map.get("lower_negative"),
    }


def find_position_danger_row(
    rows: List[Dict[str, Any]],
    side: str,
    center: float,
    entry: float,
    danger: Optional[float],
) -> Optional[Dict[str, Any]]:
    target = danger if danger is not None else entry
    candidates = []

    for r in rows:
        lv = safe_float(r.get("line_value"))
        p = safe_float(r.get("probability"))
        direction = str(r.get("direction") or "unknown").lower()

        if lv is None or p is None:
            continue

        if side == "long":
            if lv <= target and lv < center and direction in ["down", "unknown"]:
                candidates.append(r)

        if side == "short":
            if lv >= target and lv > center and direction in ["over", "unknown"]:
                candidates.append(r)

    if not candidates:
        return None

    return min(candidates, key=lambda r: abs((safe_float(r.get("line_value")) or target) - target))


# ============================================================
# Logging
# ============================================================

def append_signal_log(
    market: Dict[str, Any],
    center: float,
    selected_group_label: str,
    rows: List[Dict[str, Any]],
    reading: Dict[str, Any],
) -> None:
    try:
        base = {
            "time_utc": utc_now_text(),
            "asset": market.get("key"),
            "display": market.get("display"),
            "center": center,
            "expiry": selected_group_label,
            "market_mode": reading.get("mode"),
            "range_text": reading.get("range_text"),
            "upper_wall": wall_zone_label(reading.get("upper_wall_map")),
            "lower_wall": wall_zone_label(reading.get("lower_wall_map")),
            "upper_ref": row_signal_text(reading.get("upper_ref")),
            "lower_ref": row_signal_text(reading.get("lower_ref")),
        }

        with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
            for row in rows:
                payload = dict(base)
                payload.update({
                    "line_label": row.get("line_label"),
                    "line_value": row.get("line_value"),
                    "direction": row.get("direction"),
                    "signal": signal_word(row),
                    "probability": row.get("probability"),
                    "strength": row.get("data_strength"),
                    "effective_n": row.get("effective_n"),
                    "error_95": row.get("error_95"),
                    "volume": row.get("volume"),
                    "liquidity": row.get("liquidity"),
                    "spread": row.get("spread"),
                    "market_group_key": row.get("market_group_key"),
                    "title": row.get("title"),
                })

                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    except Exception as e:
        print("[WARN] signal log failed:", e)


# ============================================================
# Render blocks
# ============================================================

def build_wall_map_block(reading: Dict[str, Any]) -> List[str]:
    upper = reading.get("upper_wall_map")
    lower = reading.get("lower_wall_map")
    upper_ref = reading.get("upper_ref")
    lower_ref = reading.get("lower_ref")
    upper_negative = reading.get("upper_negative")
    lower_negative = reading.get("lower_negative")

    lines = []
    lines.append("🧱 **壁マップ**")

    if upper:
        lines.append(f"`上の壁: {wall_zone_label(upper)} / {upper.get('label')}`")
        lines.append(f"`上構造: {upper.get('text')}`")
    elif upper_negative:
        lines.append(f"`上の壁: {row_signal_text(upper_negative)} / 否定ライン`")
    elif upper_ref:
        lines.append(f"`上の壁: {row_signal_text(upper_ref)} / 次の確認ライン`")
    else:
        lines.append("`上の壁: なし`")

    if lower:
        lines.append(f"`下の壁: {wall_zone_label(lower)} / {lower.get('label')}`")
        lines.append(f"`下構造: {lower.get('text')}`")
    elif lower_negative:
        lines.append(f"`下の壁: {row_signal_text(lower_negative)} / 否定ライン`")
    elif lower_ref:
        lines.append(f"`下の壁: {row_signal_text(lower_ref)} / 直近確認ライン`")
    else:
        lines.append("`下の壁: なし`")

    return lines


def build_near_zone_block(market: Dict[str, Any], center: float, reading: Dict[str, Any]) -> List[str]:
    if not is_oil_market(market):
        return []

    upper_ref = reading.get("upper_ref")
    lower_ref = reading.get("lower_ref")

    current_rounded = round(center)

    lines = []
    lines.append("📍 **近接ゾーン**")
    lines.append(f"`現在付近: {format_line_label(current_rounded, 1)}`")

    upper_lv = safe_float(upper_ref.get("line_value")) if upper_ref else None
    lower_lv = safe_float(lower_ref.get("line_value")) if lower_ref else None

    if upper_lv is not None and upper_lv > center:
        start = current_rounded + 1
        end = int(round(upper_lv)) - 1

        if start <= end:
            lines.append(f"`上方向: {format_line_label(start, 1)}〜{format_line_label(end, 1)} / {format_line_label(upper_lv, 1)}手前`")

        lines.append(f"`次の上確認: {row_signal_text(upper_ref)}`")

    if lower_lv is not None and lower_lv <= center:
        lines.append(f"`直近下確認: {row_signal_text(lower_ref)}`")

    return lines


def build_wall_block(reading: Dict[str, Any]) -> List[str]:
    lines = []

    upper_wall = reading.get("upper_wall") or {}
    lower_wall = reading.get("lower_wall") or {}

    candidates = []

    if upper_wall.get("label") in ["強い壁", "壁の可能性"]:
        candidates.append(upper_wall)

    if lower_wall.get("label") in ["強い壁", "壁の可能性"]:
        candidates.append(lower_wall)

    if not candidates:
        return lines

    candidates = sorted(
        candidates,
        key=lambda w: (
            1 if w.get("label") == "強い壁" else 0,
            safe_float(w.get("diff"), 0) or 0,
        ),
        reverse=True,
    )

    wall = candidates[0]
    row_a = wall.get("row_a")
    row_b = wall.get("row_b")

    lines.append("🧱 **近接壁**")

    if row_a and row_b:
        a_lv = safe_float(row_a.get("line_value"))
        b_lv = safe_float(row_b.get("line_value"))

        if a_lv is not None and b_lv is not None:
            low = min(a_lv, b_lv)
            high = max(a_lv, b_lv)
            step = 1000 if high >= 10_000 else 1
            lines.append(f"`壁ゾーン: {format_line_label(low, step)}〜{format_line_label(high, step)}`")

    lines.append(f"`判定: {wall.get('label')}`")
    lines.append(f"`構造: {wall.get('text')}`")

    if wall.get("wall_type") == "上値壁ゾーン":
        lines.append("`読み: 手前ラインは意識されるが、次の上抜けは否定。上値が重い可能性。`")
    elif wall.get("wall_type") == "下値壁ゾーン":
        lines.append("`読み: 手前ラインは意識されるが、次の下抜けは否定。下値が堅い可能性。`")

    return lines


def build_conclusion(
    market: Dict[str, Any],
    center: float,
    reading: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> List[str]:
    side = get_position_side(market)
    entry = get_entry_price(market)

    upper_touch = reading.get("upper_touch")
    upper_break = reading.get("upper_break")
    lower_touch = reading.get("lower_touch")
    upper_wall_map = reading.get("upper_wall_map")
    lower_wall_map = reading.get("lower_wall_map")
    upper_ref = reading.get("upper_ref")
    lower_ref = reading.get("lower_ref")
    mode = reading.get("mode", "中立")

    lines = []
    lines.append("✅ **結論**")

    if not side or entry is None:
        lines.append(f"`市場は {mode}。`")

        if upper_wall_map:
            lines.append(f"`上の壁: {wall_zone_label(upper_wall_map)}`")
        elif upper_ref:
            lines.append(f"`上の確認: {row_signal_text(upper_ref)}`")

        if lower_wall_map:
            lines.append(f"`下の壁: {wall_zone_label(lower_wall_map)}`")
        elif lower_ref:
            lines.append(f"`下の確認: {row_signal_text(lower_ref)}`")

        return lines

    if side == "long":
        move = center - entry

        if move > 0:
            lines.append(f"`LONG @ {format_price(entry)} は利益圏。`")
            if upper_wall_map:
                lines.append(f"`上の壁 {wall_zone_label(upper_wall_map)} は利確候補。`")
            elif upper_ref:
                lines.append(f"`次の上確認は {row_signal_text(upper_ref)}。`")
            else:
                lines.append("`主なリスクは利益削り。即危険ではない。`")
        elif move < 0:
            lines.append(f"`LONG @ {format_price(entry)} は逆行中。`")
            if lower_wall_map:
                lines.append(f"`下の壁 {wall_zone_label(lower_wall_map)} を割ると危険。`")
            elif lower_ref:
                lines.append(f"`直近下確認は {row_signal_text(lower_ref)}。`")
            else:
                lines.append("`建値回復までは慎重。危険ライン下抜けに注意。`")
        else:
            lines.append(f"`LONG @ {format_price(entry)} は建値付近。`")

        if upper_touch:
            lines.append(f"`上値: {row_signal_text(upper_touch)}`")
        if upper_break:
            lines.append(f"`上抜け: {row_signal_text(upper_break)}`")
        if lower_touch:
            lines.append(f"`下値再訪: {row_signal_text(lower_touch)}`")

    elif side == "short":
        move = entry - center

        if move > 0:
            lines.append(f"`SHORT @ {format_price(entry)} は利益圏。`")
            if lower_wall_map:
                lines.append(f"`下の壁 {wall_zone_label(lower_wall_map)} は利確候補。`")
            elif lower_ref:
                lines.append(f"`直近下確認は {row_signal_text(lower_ref)}。`")
            else:
                lines.append("`主なリスクは戻り踏み。`")
        elif move < 0:
            lines.append(f"`SHORT @ {format_price(entry)} は逆行中。`")
            if upper_wall_map:
                lines.append(f"`上の壁 {wall_zone_label(upper_wall_map)} を背に戻り売り余地。突破なら危険。`")
            elif upper_ref:
                lines.append(f"`次の上確認は {row_signal_text(upper_ref)}。`")
            else:
                lines.append("`上値タッチ警戒。ただし上抜けが弱いなら戻り売り余地。`")
        else:
            lines.append(f"`SHORT @ {format_price(entry)} は建値付近。`")

        if upper_touch:
            lines.append(f"`上値: {row_signal_text(upper_touch)}`")
        if upper_break:
            lines.append(f"`上抜け: {row_signal_text(upper_break)}`")
        if lower_touch:
            lines.append(f"`下値再訪: {row_signal_text(lower_touch)}`")

    return lines


def build_market_reading_block(reading: Dict[str, Any]) -> List[str]:
    lines = []
    lines.append("🧠 **市場の読み**")
    lines.append(f"`想定レンジ: {reading.get('range_text', 'n/a')}`")

    if reading.get("upper_touch"):
        lines.append(f"`上値タッチ: {row_signal_text(reading.get('upper_touch'))}`")
    if reading.get("upper_break"):
        lines.append(f"`上抜け: {row_signal_text(reading.get('upper_break'))}`")
    if reading.get("lower_touch"):
        lines.append(f"`下値再訪: {row_signal_text(reading.get('lower_touch'))}`")
    if reading.get("lower_break"):
        lines.append(f"`下抜け: {row_signal_text(reading.get('lower_break'))}`")

    lines.append(f"`市場モード: {reading.get('mode', '中立')}`")
    return lines


def build_position_block(
    market: Dict[str, Any],
    center: float,
    rows: List[Dict[str, Any]],
) -> List[str]:
    side = get_position_side(market)
    entry = get_entry_price(market)
    danger = get_danger_price(market)
    cme_last = get_cme_last_price(market)
    memo = get_position_memo(market)

    if not side or entry is None:
        return []

    side_label = "LONG" if side == "long" else "SHORT"
    danger_row = find_position_danger_row(rows, side, center, entry, danger)

    if side == "long":
        move = center - entry
        move_text = f"+${move:,.2f} 有利" if move >= 0 else f"-${abs(move):,.2f} 不利"
    else:
        move = entry - center
        move_text = f"+${move:,.2f} 有利" if move >= 0 else f"-${abs(move):,.2f} 不利"

    lines = []
    lines.append("🎯 **ポジション**")
    lines.append(f"`建値: {side_label} {format_price(entry)}`")
    lines.append(f"`現在: {format_price(center)}`")
    lines.append(f"`含み方向: {move_text}`")

    if danger is not None:
        danger_text = row_signal_text(danger_row) if danger_row else "該当ラインなし"
        lines.append(f"`危険ライン: {format_price(danger)} / {danger_text}`")

    if cme_last is not None:
        gap = center - cme_last
        sign = "+" if gap >= 0 else ""
        lines.append(f"`CME比: {sign}{gap:.2f}`")

    if memo:
        lines.append(f"`メモ: {memo[:80]}`")

    return lines


def build_trade_bias_block(
    market: Dict[str, Any],
    center: float,
    reading: Dict[str, Any],
) -> List[str]:
    side = get_position_side(market)
    entry = get_entry_price(market)

    upper_wall_map = reading.get("upper_wall_map")
    lower_wall_map = reading.get("lower_wall_map")
    upper_ref = reading.get("upper_ref")
    lower_ref = reading.get("lower_ref")

    lines = []
    lines.append("🧭 **Trade Bias**")

    if not side or entry is None:
        if upper_wall_map and lower_wall_map:
            lines.append("`方針: 上下に壁あり。レンジ内は無理に追わず、壁付近の反応を見る。`")
        elif upper_wall_map:
            lines.append("`方針: 上の壁を確認。新規ロング追撃は慎重。壁付近の反応を見る。`")
        elif lower_wall_map:
            lines.append("`方針: 下の壁を確認。新規ショート追撃は慎重。壁付近の反応を見る。`")
        elif upper_ref or lower_ref:
            lines.append("`方針: 明確な壁は薄い。近い確認ラインの反応を見る。`")
        else:
            lines.append("`方針: 方向感は薄い。無理に入らずレンジ端を待つ。`")
        return lines

    if side == "long":
        move = center - entry

        if move > 0:
            if upper_wall_map:
                lines.append("`方針: 利益圏。上の壁は利確候補。追撃より分割利確優先。`")
            elif upper_ref:
                lines.append("`方針: 利益圏。次の上確認ラインまで伸ばせるか確認。`")
            else:
                lines.append("`方針: 利益圏。利確を分けつつ、上抜け評価の上昇を確認。`")
        elif move < 0:
            if lower_wall_map:
                lines.append("`方針: ロング逆行中。下の壁で支えられるか確認。割れたら撤退優先。`")
            elif lower_ref:
                lines.append("`方針: ロング逆行中。直近下確認を割るなら軽くする。`")
            else:
                lines.append("`方針: ロング逆行中。ナンピンより建値回復確認。`")
        else:
            lines.append("`方針: 建値付近。上抜け確認までは無理に伸ばさない。`")

        return lines

    if side == "short":
        move = entry - center

        if move > 0:
            if lower_wall_map:
                lines.append("`方針: ショート利益圏。下の壁は利確候補。欲張りすぎ注意。`")
            elif lower_ref:
                lines.append("`方針: 利益圏。直近下確認では一部利確を検討。`")
            else:
                lines.append("`方針: 利益圏。利確を分けつつ、下抜け評価の上昇を確認。`")
        elif move < 0:
            if upper_wall_map:
                lines.append("`方針: ショート逆行中。ただし上の壁あり。壁突破評価が上がれば撤退優先。`")
            elif upper_ref:
                lines.append("`方針: ショート逆行中。次の上確認ライン突破なら危険。`")
            else:
                lines.append("`方針: ショート逆行中。上値タッチ警戒。`")
        else:
            lines.append("`方針: 建値付近。下抜け確認までは無理に伸ばさない。`")

        return lines

    lines.append("`方針: 判定不能。`")
    return lines


def build_focus_lines(rows: List[Dict[str, Any]], center: float) -> List[str]:
    candidates = []

    for row in rows:
        p = safe_float(row.get("probability"))
        lv = safe_float(row.get("line_value"))

        if p is None or lv is None:
            continue

        distance = abs(lv - center)
        distance_scale = max(center * 0.01, 1)
        battleground = 1 - abs(p - 0.5) * 2
        high_prob_bonus = p if p >= TOUCH_HIGH_THRESHOLD else 0
        strength_bonus = strength_rank(str(row.get("data_strength") or "低")) / 3

        score = (
            battleground * 0.25
            + high_prob_bonus * 0.25
            + math.exp(-distance / distance_scale) * 0.35
            + strength_bonus * 0.15
        )

        candidates.append((score, row))

    candidates = sorted(candidates, key=lambda x: x[0], reverse=True)

    selected = []
    used = set()

    for _, row in candidates:
        label = row.get("line_label")
        if label in used:
            continue

        used.add(label)
        selected.append(row)

        if len(selected) >= MAX_FOCUS_LINES:
            break

    selected = sort_rows_by_line(selected)

    lines = []
    lines.append("🔥 **注目ライン**")

    if not selected:
        lines.append("`なし`")
        return lines

    for row in selected:
        lines.append(f"`{row_signal_text(row)}`")

    return lines


# ============================================================
# Message
# ============================================================

def build_market_message(market: Dict[str, Any], rows: List[Dict[str, Any]], center: float, center_source: str) -> str:
    selected_rows, selected_group_label = choose_best_group(rows, center)

    if selected_rows:
        rows = selected_rows

    grid_rows = prepare_grid_rows(rows, center)
    reading = build_market_reading_data(grid_rows, center)

    append_signal_log(
        market=market,
        center=center,
        selected_group_label=selected_group_label,
        rows=grid_rows,
        reading=reading,
    )

    display = market.get("display") or market.get("key")
    step = int(market.get("grid_step") or 1)

    if is_oil_market(market):
        center_label = format_price(center)
    else:
        center_label = format_line_label(center, step)

    lines = []
    lines.append(f"**{display}**")
    lines.append(f"`現在値: {center_label} / {center_source}`")

    if selected_group_label:
        lines.append(f"`対象期限: {selected_group_label}`")

    if MAX_DAYS_TO_EXPIRY > 0:
        lines.append(f"`期限フィルター: {MAX_DAYS_TO_EXPIRY}日以内`")

    lines.append(f"`市場感: {reading.get('mode', '中立')}`")
    lines.append("")

    wall_map_lines = build_wall_map_block(reading)
    if wall_map_lines:
        lines.extend(wall_map_lines)
        lines.append("")

    near_zone_lines = build_near_zone_block(market, center, reading)
    if near_zone_lines:
        lines.extend(near_zone_lines)
        lines.append("")

    close_wall_lines = build_wall_block(reading)
    if close_wall_lines:
        lines.extend(close_wall_lines)
        lines.append("")

    lines.extend(build_conclusion(market, center, reading, grid_rows))
    lines.append("")

    lines.extend(build_market_reading_block(reading))

    position_lines = build_position_block(market, center, grid_rows)
    if position_lines:
        lines.append("")
        lines.extend(position_lines)

    trade_lines = build_trade_bias_block(market, center, reading)
    if trade_lines:
        lines.append("")
        lines.extend(trade_lines)

    lines.append("")
    lines.extend(build_focus_lines(grid_rows, center))

    return "\n".join(lines)


def build_full_message(market_blocks: List[str]) -> List[str]:
    header = "\n".join([
        "📊 **Market Line Radar**",
        f"`{utc_now_text()}`",
    ])

    chunks = []
    current = header

    for block in market_blocks:
        candidate = current + "\n\n" + block

        if len(candidate) <= DISCORD_MESSAGE_LIMIT - 100:
            current = candidate
        else:
            chunks.append(current)
            current = header + "\n\n" + block

    if current.strip():
        chunks.append(current)

    return chunks


# ============================================================
# Main
# ============================================================

def process_market(market: Dict[str, Any]) -> Optional[str]:
    print("")
    print("=" * 60)
    print(f"[INFO] Processing market: {market.get('key')} / {market.get('display')}")
    print("=" * 60)

    center, center_source = choose_center(market)

    if center is None:
        print(f"[WARN] Center not found for {market.get('key')}")
        return f"**{market.get('display') or market.get('key')}**\n`現在値取得失敗`"

    print(f"[INFO] Center {market.get('key')}: {center} / {center_source}")

    items = fetch_polymarket_items(market, center)
    print(f"[INFO] Polymarket items {market.get('key')}: {len(items)}")

    if not items:
        return f"**{market.get('display') or market.get('key')}**\n`対象Polymarketなし`"

    rows = aggregate_items(items)
    print(f"[INFO] Aggregated rows {market.get('key')}: {len(rows)}")

    if not rows:
        return f"**{market.get('display') or market.get('key')}**\n`集計対象なし`"

    return build_market_message(market, rows, center, center_source)


def run_once() -> None:
    print("[INFO] Loading markets...")
    markets = load_enabled_supported_markets()

    print(f"[INFO] Enabled supported markets: {len(markets)}")
    for m in markets:
        print("[INFO] Market:", m.get("key"), m.get("display"))

    print("[INFO] Loading position_state.json...")
    print("[INFO] Position state:", load_position_state())

    print("[INFO] Settings:")
    print("[INFO] MAX_DAYS_TO_EXPIRY:", MAX_DAYS_TO_EXPIRY)
    print("[INFO] MIN_LIQUIDITY_FOR_HIGH:", MIN_LIQUIDITY_FOR_HIGH)
    print("[INFO] MIN_LIQUIDITY_FOR_MEDIUM:", MIN_LIQUIDITY_FOR_MEDIUM)

    blocks = []

    for market in markets:
        try:
            block = process_market(market)
            if block:
                blocks.append(block)
        except Exception as e:
            print(f"[ERROR] process_market failed key={market.get('key')}: {e}")
            blocks.append(f"**{market.get('display') or market.get('key')}**\n`処理エラー: {e}`")

        time.sleep(0.5)

    if not blocks:
        discord_send("⚠️ **Market Line Radar**\nNo market blocks generated.")
        return

    messages = build_full_message(blocks)

    print(f"[INFO] Discord message chunks: {len(messages)}")

    for i, message in enumerate(messages, start=1):
        if len(messages) > 1:
            message = f"{message}\n\n`part {i}/{len(messages)}`"

        discord_send(message)
        time.sleep(0.7)

    print("[INFO] Finished.")


if __name__ == "__main__":
    run_once()
