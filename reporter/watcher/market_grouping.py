"""
reporter/watcher/market_grouping.py

Groups the individual markets within a Polymarket NFL event into
moneyline / spread / total buckets.

Each NFL event contains ~50 markets (moneyline, multiple spread and total
lines, plus props/half-markets). This module classifies and buckets only
the three types the reporter cares about; props and other types are
silently skipped during grouping.

All functions are pure (no network calls).
"""

from __future__ import annotations

import re
from typing import TypedDict


class MarketClassifyError(ValueError):
    """Raised when a market's type cannot be determined."""


class GroupedMarkets(TypedDict):
    moneyline: dict | None          # the single moneyline market, or None
    spreads: list[dict]             # zero or more spread markets (distinct lines)
    totals: list[dict]              # zero or more total markets (distinct lines)


# ---------------------------------------------------------------------------
# sportsMarketType → canonical type (primary path)
# ---------------------------------------------------------------------------

_SPORTS_MARKET_TYPE_MAP: dict[str, str] = {
    "moneyline": "moneyline",
    "spreads":   "spread",
    "totals":    "total",
}

# groupItemTitle patterns (fallback when sportsMarketType is absent)
_SPREAD_TITLE_RE = re.compile(r"^Spread\s+[-+]?\d", re.IGNORECASE)
_TOTAL_TITLE_RE  = re.compile(r"^O/U\s+\d",         re.IGNORECASE)

# question text patterns (last resort fallback)
_SPREAD_Q_RE = re.compile(r"\bSpread\b",      re.IGNORECASE)
_TOTAL_Q_RE  = re.compile(r"\bO/U\b",         re.IGNORECASE)


def classify_market(market: dict) -> str:
    """
    Classify a market dict as "moneyline", "spread", or "total".

    Classification order:
      1. sportsMarketType field (typed, present on all live NFL markets)
      2. groupItemTitle patterns (null → moneyline; "Spread X" / "O/U X")
      3. question text patterns

    Returns:
        One of "moneyline", "spread", "total".

    Raises:
        MarketClassifyError: if none of the three approaches can classify
            the market. Callers that want to skip unclassifiable markets
            (e.g. props) should catch this exception.
    """
    market_id = market.get("id", "<unknown>")

    # 1. sportsMarketType — most reliable
    smt = market.get("sportsMarketType")
    if smt is not None:
        canonical = _SPORTS_MARKET_TYPE_MAP.get(smt)
        if canonical is not None:
            return canonical

    # 2. groupItemTitle — only when the key is actually present in the dict
    if "groupItemTitle" in market:
        git = market["groupItemTitle"]
        if git is None:
            # Explicit null → moneyline (confirmed by recon: the game-winner
            # market carries groupItemTitle=null; all props have a string value)
            return "moneyline"
        if isinstance(git, str):
            if _SPREAD_TITLE_RE.match(git):
                return "spread"
            if _TOTAL_TITLE_RE.match(git):
                return "total"
            # Non-null string that doesn't match spread/total → prop; fall through

    # 3. question text
    question = market.get("question", "")
    if _SPREAD_Q_RE.search(question):
        return "spread"
    if _TOTAL_Q_RE.search(question):
        return "total"

    git = market.get("groupItemTitle", "<absent>")
    raise MarketClassifyError(
        f"Cannot classify market id={market_id!r} "
        f"sportsMarketType={smt!r} "
        f"groupItemTitle={git!r} "
        f"question={question[:80]!r}"
    )


def group_markets_for_event(event: dict) -> GroupedMarkets:
    """
    Group the markets of an NFL event into moneyline / spreads / totals.

    Markets that cannot be classified (props, half-markets, etc.) are
    silently skipped — they are not dropped from any count because
    they were never meant to be included.

    Per Phase 3 design: missing moneyline is not an error here. The
    report writer (Step 9) decides what to do with moneyline=None.

    Args:
        event: A raw event dict as returned by get_current_nfl_games().

    Returns:
        GroupedMarkets with moneyline (dict | None), spreads (list),
        totals (list).
    """
    markets: list[dict] = event.get("markets", [])

    moneyline: dict | None = None
    spreads: list[dict] = []
    totals: list[dict] = []

    for market in markets:
        try:
            mtype = classify_market(market)
        except MarketClassifyError:
            continue  # prop / half / other — intentionally skipped

        if mtype == "moneyline":
            # Keep the first moneyline if multiple appear (should be exactly one)
            if moneyline is None:
                moneyline = market
        elif mtype == "spread":
            spreads.append(market)
        elif mtype == "total":
            totals.append(market)

    return GroupedMarkets(moneyline=moneyline, spreads=spreads, totals=totals)
