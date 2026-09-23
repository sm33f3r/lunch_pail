"""
reporter/watcher/game_assembly.py

Consolidates raw Polymarket NFL events into one clean structured object
per real game, ready for Step 9's report writer.

Multiple events can share the same gameId (moneyline event + prop siblings
like "Highest Scoring Quarter"). Volume filtering is applied only to the
moneyline-bearing (representative) event, not prop siblings, so real
games are not incorrectly excluded because a low-volume prop event shares
their gameId.
"""

from __future__ import annotations

from reporter.watcher.market_data import extract_market_data, extract_prices
from reporter.watcher.market_grouping import group_markets_for_event
from reporter.watcher.nfl_events import get_current_nfl_games
from reporter.watcher.team_mapping import parse_teams_from_event
from reporter.watcher.volume_filter import get_event_volume, meets_volume_threshold
from reporter.config import settings

_POLYMARKET_EVENT_URL = "https://polymarket.com/event/{slug}"


def consolidate_events_by_game_id(events: list[dict]) -> dict[int, list[dict]]:
    """
    Group a flat list of events by their gameId.

    Args:
        events: Raw event dicts from get_current_nfl_games() (all have non-null gameId).

    Returns:
        Dict mapping gameId (int) → list of event dicts for that game.
    """
    result: dict[int, list[dict]] = {}
    for event in events:
        gid = event.get("gameId")
        if gid is None:
            continue
        result.setdefault(gid, []).append(event)
    return result


def select_representative_event(events_for_game: list[dict]) -> dict | None:
    """
    Pick the event that carries the moneyline market for this game.

    If multiple events for the same gameId have a moneyline (shouldn't happen
    in practice), the one with the highest volume in the configured window wins.
    If none have a moneyline, returns None.

    Args:
        events_for_game: All events sharing one gameId.

    Returns:
        The representative event dict, or None if no event has a moneyline.
    """
    candidates = [
        e for e in events_for_game
        if group_markets_for_event(e)["moneyline"] is not None
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Tiebreak by volume in the configured window (highest wins)
    return max(candidates, key=lambda e: get_event_volume(e, settings.volume_window))


def build_game_report_data(game_id: int, events_for_game: list[dict]) -> dict | None:
    """
    Build the structured report data object for one game.

    Steps:
      1. Select the moneyline-bearing representative event.
      2. Apply volume threshold to that event (not prop siblings).
      3. Parse teams, group markets, extract market data.

    Args:
        game_id:          The integer gameId.
        events_for_game:  All events sharing this gameId.

    Returns:
        Structured dict with game identity, volume, and grouped market data;
        or None if there is no moneyline event or the event fails the threshold.
    """
    rep = select_representative_event(events_for_game)
    if rep is None:
        return None

    if not meets_volume_threshold(rep):
        return None

    teams      = parse_teams_from_event(rep)
    grouped    = group_markets_for_event(rep)
    volume     = get_event_volume(rep, settings.volume_window)
    slug       = rep.get("slug", "")
    event_url  = _POLYMARKET_EVENT_URL.format(slug=slug)

    moneyline_data = None
    if grouped["moneyline"]:
        moneyline_data = extract_market_data(grouped["moneyline"])

    # Spreads and totals are secondary; only prices are needed (pure, no network call).
    spreads_data = [{"prices": extract_prices(m)} for m in grouped["spreads"]]
    totals_data  = [{"prices": extract_prices(m)} for m in grouped["totals"]]

    return {
        "game_id":   game_id,
        "away_team": teams["away_team"],
        "home_team": teams["home_team"],
        "away_abbr": teams["away_abbr"],
        "home_abbr": teams["home_abbr"],
        "game_date": teams["game_date"],
        "event_slug": slug,
        "event_url":  event_url,
        "volume":     volume,
        "moneyline":  moneyline_data,
        "spreads":    spreads_data,
        "totals":     totals_data,
    }


def get_reportable_games() -> list[dict]:
    """
    Fetch all current NFL games and return structured report-ready data.

    Consolidates events by gameId, applies volume filtering on the
    representative (moneyline-bearing) event, and returns the final list
    Step 9 will consume directly.

    Volume filtering happens before any market-data network calls:
    games that fail the threshold are skipped without touching OI/trades.

    Returns:
        List of game report dicts, one per qualifying game. May be empty
        if no games meet the threshold.
    """
    events  = get_current_nfl_games()
    by_game = consolidate_events_by_game_id(events)
    total   = len(by_game)
    print(f"Fetched {len(events)} events across {total} distinct games; fetching market data for qualifying games...")

    results = []
    for i, (game_id, game_events) in enumerate(by_game.items(), 1):
        data = build_game_report_data(game_id, game_events)
        if data is not None:
            results.append(data)
            print(f"  [{i}/{total}] {data['away_abbr']} @ {data['home_abbr']}  vol=${data['volume']:,.0f}  OK ({len(results)} reportable)")
        else:
            print(f"  [{i}/{total}] game_id={game_id}  skipped")

    print(f"Done. {len(results)}/{total} games reportable.")
    return results
