"""
Unit tests for reporter.watcher.nfl_events.filter_game_events().

All tests run offline using fixture files — no network access required.
"""

import json
import os
import sys
from pathlib import Path

import pytest

# Set required env vars before importing reporter.config
os.environ.setdefault("VOLUME_THRESHOLD", "1000")
os.environ.setdefault("VOLUME_WINDOW", "24hr")
os.environ.setdefault("REPORT_OUTPUT_DIR", "/tmp/reporter_test")
os.environ.setdefault("POLLING_INTERVAL_SECONDS", "300")

from reporter.watcher.nfl_events import filter_game_events  # noqa: E402

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _load(name: str) -> dict | list:
    with open(_FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def game_event() -> dict:
    """Single real game event (Eagles vs. Bears) from the recon fixture."""
    return _load("events_nfl_sample.json")


@pytest.fixture
def futures_events() -> list[dict]:
    """Three futures events (Super Bowl / conf champions) from recon fixture."""
    return _load("futures_market_sample.json")


# ---------------------------------------------------------------------------
# filter_game_events — core logic
# ---------------------------------------------------------------------------

class TestFilterGameEvents:
    def test_game_event_passes(self, game_event):
        result = filter_game_events([game_event])
        assert len(result) == 1
        assert result[0]["gameId"] == game_event["gameId"]

    def test_futures_are_excluded(self, futures_events):
        result = filter_game_events(futures_events)
        assert result == [], (
            f"Expected no futures to pass, got: {[e.get('title') for e in result]}"
        )

    def test_futures_have_null_game_id(self, futures_events):
        for ev in futures_events:
            assert ev.get("gameId") is None, (
                f"Fixture futures event '{ev.get('title')}' unexpectedly has gameId={ev.get('gameId')}"
            )

    def test_closed_game_event_excluded(self, game_event):
        closed = {**game_event, "closed": True}
        assert filter_game_events([closed]) == []

    def test_open_game_event_included(self, game_event):
        open_event = {**game_event, "closed": False}
        assert len(filter_game_events([open_event])) == 1

    def test_mixed_list_returns_only_games(self, game_event, futures_events):
        mixed = futures_events + [game_event]
        result = filter_game_events(mixed)
        assert len(result) == 1
        assert result[0]["gameId"] is not None

    def test_empty_input_returns_empty(self):
        assert filter_game_events([]) == []

    def test_all_returned_events_have_game_id(self, game_event, futures_events):
        mixed = futures_events + [game_event]
        for ev in filter_game_events(mixed):
            assert ev.get("gameId") is not None

    def test_all_returned_events_are_open(self, game_event, futures_events):
        closed_game = {**game_event, "closed": True}
        mixed = futures_events + [game_event, closed_game]
        for ev in filter_game_events(mixed):
            assert ev.get("closed") is False
