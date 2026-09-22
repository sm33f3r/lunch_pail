"""
reporter/watcher/volume_filter.py

Filters NFL game events by trading volume threshold.

Volume and window are read from config.py (VOLUME_THRESHOLD, VOLUME_WINDOW).
Threshold comparison uses >= (at-threshold events are included).
All functions are pure (no network calls).
"""

from __future__ import annotations

from reporter.config import settings

# Maps VOLUME_WINDOW config values to the corresponding event field name.
_WINDOW_TO_FIELD: dict[str, str] = {
    "24hr":     "volume24hr",
    "1wk":      "volume1wk",
    "1mo":      "volume1mo",
    "1yr":      "volume1yr",
    "lifetime": "volume",
}


def get_event_volume(event: dict, window: str) -> float:
    """
    Return the event's volume for the given time window.

    Args:
        event:  A raw event dict as returned by get_current_nfl_games().
        window: One of "24hr", "1wk", "1mo", "1yr", "lifetime".

    Returns:
        Volume in USD as a float. Returns 0.0 if the field is absent
        (e.g. very new event with no recorded volume yet).

    Raises:
        ValueError: if window is not one of the five recognised values.
    """
    if window not in _WINDOW_TO_FIELD:
        raise ValueError(
            f"Unrecognised volume window {window!r}. "
            f"Must be one of: {sorted(_WINDOW_TO_FIELD)}."
        )
    field = _WINDOW_TO_FIELD[window]
    return float(event.get(field) or 0.0)


def meets_volume_threshold(event: dict) -> bool:
    """
    Return True if the event's volume meets or exceeds the configured threshold.

    Uses settings.volume_threshold and settings.volume_window from config.py.
    Threshold comparison is >= (inclusive: at-threshold events pass).
    """
    vol = get_event_volume(event, settings.volume_window)
    return vol >= settings.volume_threshold


def filter_events_by_volume(events: list[dict]) -> list[dict]:
    """
    Return only the events that meet the configured volume threshold.

    Args:
        events: List of raw event dicts (e.g. from get_current_nfl_games()).

    Returns:
        Filtered list; order preserved.
    """
    return [e for e in events if meets_volume_threshold(e)]
