"""
reporter/watcher/market_data.py

Extracts live trading data for individual Polymarket NFL markets:
outcome prices, open interest, and recent trades.

Network is isolated to fetch_open_interest() and fetch_recent_trades().
extract_prices() is a pure function.
"""

from __future__ import annotations

import json
import time

import requests

from reporter.config import settings

_PRICE_SUM_TOLERANCE = 0.02
_RETRY_DELAYS = (1, 3)  # seconds between retries; two attempts after first failure


class PriceDataError(ValueError):
    """Raised when market price data is missing, malformed, or internally inconsistent."""


class OpenInterestError(ValueError):
    """Raised when the OI endpoint returns a GLOBAL aggregate instead of per-market data."""


# ---------------------------------------------------------------------------
# extract_prices — pure, no network
# ---------------------------------------------------------------------------

def extract_prices(market: dict) -> dict[str, float]:
    """
    Parse outcomes and prices from a market dict into a clean label→price mapping.

    outcomePrices and outcomes are JSON-encoded strings on the market object.

    Args:
        market: A raw market dict from a Polymarket event.

    Returns:
        Dict mapping outcome label (str) to price (float), e.g.
        {"Eagles": 0.625, "Bears": 0.375}.

    Raises:
        PriceDataError: if fields are missing, not parseable, counts don't
            match, or prices don't sum to ~1.0 (±0.02 tolerance).
    """
    market_id = market.get("id", "<unknown>")

    raw_outcomes = market.get("outcomes")
    raw_prices   = market.get("outcomePrices")

    if not raw_outcomes or not raw_prices:
        raise PriceDataError(
            f"Market {market_id!r} missing outcomes or outcomePrices fields."
        )

    try:
        outcomes: list[str]   = json.loads(raw_outcomes)
        prices:   list[str]   = json.loads(raw_prices)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PriceDataError(
            f"Market {market_id!r}: could not JSON-decode outcomes/outcomePrices: {exc}"
        ) from exc

    if len(outcomes) != len(prices):
        raise PriceDataError(
            f"Market {market_id!r}: outcomes count ({len(outcomes)}) != "
            f"prices count ({len(prices)})."
        )

    try:
        float_prices = [float(p) for p in prices]
    except (ValueError, TypeError) as exc:
        raise PriceDataError(
            f"Market {market_id!r}: non-numeric price in outcomePrices: {exc}"
        ) from exc

    price_sum = sum(float_prices)
    if abs(price_sum - 1.0) > _PRICE_SUM_TOLERANCE:
        raise PriceDataError(
            f"Market {market_id!r}: prices sum to {price_sum:.4f}, "
            f"expected 1.0 ± {_PRICE_SUM_TOLERANCE}. Possible stale or bad data."
        )

    return dict(zip(outcomes, float_prices))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_with_retry(url: str, params: dict, timeout: int = 10) -> requests.Response:
    """GET with simple retry-on-non-200 using backoff delays."""
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        response = requests.get(url, params=params, timeout=timeout)
        if response.status_code == 200:
            return response
        if delay is not None:
            time.sleep(delay)
    response.raise_for_status()
    return response  # unreachable, but satisfies type checker


# ---------------------------------------------------------------------------
# fetch_open_interest
# ---------------------------------------------------------------------------

def fetch_open_interest(condition_id: str) -> float:
    """
    Fetch per-market open interest (USD) from the Data API.

    Args:
        condition_id: The market's conditionId (0x-prefixed hex string).

    Returns:
        Open interest as a float.

    Raises:
        OpenInterestError: if the response market field is "GLOBAL" —
            meaning the conditionId wasn't recognised and the endpoint
            returned the platform-wide aggregate instead of this market's OI.
        requests.HTTPError: on persistent HTTP failures.
    """
    base = settings.data_api_base_url.rstrip("/")
    response = _get_with_retry(f"{base}/oi", params={"market": condition_id})

    data = response.json()

    # Response is a list: [{"market": "<conditionId>|GLOBAL", "value": <float>}]
    if not isinstance(data, list) or not data:
        raise OpenInterestError(
            f"Unexpected /oi response shape for conditionId={condition_id!r}: {data!r}"
        )

    record = data[0]
    returned_market = record.get("market", "")

    if returned_market == "GLOBAL":
        raise OpenInterestError(
            f"OI endpoint returned GLOBAL aggregate for conditionId={condition_id!r}. "
            f"The conditionId was not recognised — value would be the platform total, "
            f"not this market's open interest."
        )

    return float(record["value"])


# ---------------------------------------------------------------------------
# fetch_recent_trades
# ---------------------------------------------------------------------------

def fetch_recent_trades(condition_id: str, limit: int = 20) -> list[dict]:
    """
    Fetch recent trades for a market from the Data API.

    Args:
        condition_id: The market's conditionId (0x-prefixed hex string).
        limit: Maximum number of trades to return (default 20).

    Returns:
        List of raw trade dicts (keys: side, size, price, timestamp,
        title, slug, conditionId, etc.).

    Raises:
        requests.HTTPError: on persistent HTTP failures.
    """
    base = settings.data_api_base_url.rstrip("/")
    response = _get_with_retry(
        f"{base}/trades",
        params={"market": condition_id, "limit": limit},
    )
    return response.json()


# ---------------------------------------------------------------------------
# extract_market_data — orchestrator
# ---------------------------------------------------------------------------

def extract_market_data(market: dict) -> dict:
    """
    Extract all live trading data for one market.

    Combines extract_prices(), fetch_open_interest(), and
    fetch_recent_trades() into a single result dict.

    Args:
        market: A raw market dict from a Polymarket event.

    Returns:
        {
            "prices":        {outcome_label: price, ...},
            "open_interest": float,
            "recent_trades": [trade_dict, ...],
        }

    Raises:
        PriceDataError: if prices are missing or don't sum to ~1.0.
        OpenInterestError: if OI returns GLOBAL aggregate.
        requests.HTTPError: on persistent API failures.
    """
    condition_id = market.get("conditionId", "")

    prices        = extract_prices(market)
    open_interest = fetch_open_interest(condition_id)
    recent_trades = fetch_recent_trades(condition_id)

    return {
        "prices":        prices,
        "open_interest": open_interest,
        "recent_trades": recent_trades,
    }
