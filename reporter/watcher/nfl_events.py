"""
reporter/watcher/nfl_events.py

Fetches NFL events from the Polymarket Gamma API and filters to real game
events (excluding futures and props that have no associated game).

Network access is isolated to fetch_nfl_events(); all filtering is pure
and testable offline.
"""

from __future__ import annotations

import requests

from reporter.config import settings

_EVENTS_ENDPOINT = "/events/keyset"
_PAGE_LIMIT = 100


class GammaAPIError(Exception):
    """Raised when the Gamma API returns an unexpected response."""


def fetch_nfl_events() -> list[dict]:
    """
    Fetch all open NFL events from the Gamma API (games and futures, unfiltered).

    Paginates through /events/keyset using next_cursor until exhausted.
    Uses order=gameId so game events sort before futures — allows early
    termination once only null-gameId events remain.

    Returns:
        List of raw event dicts from the API.

    Raises:
        GammaAPIError: on HTTP errors or malformed responses.
    """
    base = settings.gamma_api_base_url.rstrip("/")
    url = f"{base}{_EVENTS_ENDPOINT}"

    all_events: list[dict] = []
    cursor: str | None = None

    seen_ids: set[str] = set()

    while True:
        params: dict = {
            "tag_id": settings.nfl_tag_id,
            "limit": _PAGE_LIMIT,
            "closed": False,
            "order": "gameId",  # game events (non-null gameId) sort before futures
        }
        if cursor:
            params["next_cursor"] = cursor

        try:
            response = requests.get(url, params=params, timeout=15)
        except requests.RequestException as exc:
            raise GammaAPIError(f"Gamma API request failed: {exc}") from exc

        if response.status_code != 200:
            raise GammaAPIError(
                f"Gamma API returned HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise GammaAPIError(f"Gamma API response is not valid JSON: {exc}") from exc

        if not isinstance(data, dict) or "events" not in data:
            raise GammaAPIError(
                f"Unexpected Gamma API response shape (expected dict with 'events' key): "
                f"{str(data)[:200]}"
            )

        page_events: list[dict] = data["events"]

        # Deduplication: the keyset cursor cycles when combined with a custom
        # sort (order=gameId). Stop as soon as a page adds no new event IDs.
        new_events = [e for e in page_events if e.get("id") not in seen_ids]
        if not new_events:
            break

        for e in new_events:
            seen_ids.add(e.get("id"))
        all_events.extend(new_events)

        cursor = data.get("next_cursor") or None
        if not cursor:
            break

    return all_events


def filter_game_events(events: list[dict]) -> list[dict]:
    """
    Filter a raw event list to real NFL game events only.

    Keeps events where:
      - gameId is not None  (excludes futures/props which have gameId=null)
      - closed is False     (excludes settled/resolved events)

    Args:
        events: Raw event dicts as returned by fetch_nfl_events().

    Returns:
        Filtered list containing only open game events.
    """
    return [
        e for e in events
        if e.get("gameId") is not None and not e.get("closed", True)
    ]


def get_current_nfl_games() -> list[dict]:
    """
    Return all currently open NFL game events from Polymarket.

    Convenience entry point: fetches all NFL events then filters to games.
    Raises GammaAPIError (not an empty list) on network or API failure.

    Returns:
        List of open game event dicts. May be empty if no games are live
        or scheduled (e.g. off-season, bye week).
    """
    events = fetch_nfl_events()
    return filter_game_events(events)
