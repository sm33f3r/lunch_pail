"""
Unit tests for reporter.watcher.volume_filter.

All tests run offline — no network access required.
"""

import json
import os
from pathlib import Path

import pytest

# Set required env vars before importing reporter.config.
# Tests that need a specific threshold override these per-test via monkeypatch.
os.environ["VOLUME_THRESHOLD"] = "5000"
os.environ["VOLUME_WINDOW"] = "24hr"
os.environ.setdefault("REPORT_OUTPUT_DIR", "/tmp/reporter_test")
os.environ.setdefault("POLLING_INTERVAL_SECONDS", "300")

from reporter.watcher.volume_filter import (  # noqa: E402
    filter_events_by_volume,
    get_event_volume,
    meets_volume_threshold,
)
from reporter.config import settings  # noqa: E402

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _load(name: str):
    with open(_FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def _make_event(**volume_fields) -> dict:
    """Build a minimal event dict with the given volume fields."""
    return {"id": "test", **volume_fields}


# ---------------------------------------------------------------------------
# get_event_volume — all 5 windows against the fixture
# ---------------------------------------------------------------------------

class TestGetEventVolume:
    def setup_method(self):
        self.event = _load("events_nfl_sample.json")

    def test_24hr(self):
        vol = get_event_volume(self.event, "24hr")
        assert vol == pytest.approx(self.event["volume24hr"])

    def test_1wk(self):
        vol = get_event_volume(self.event, "1wk")
        assert vol == pytest.approx(self.event["volume1wk"])

    def test_1mo(self):
        vol = get_event_volume(self.event, "1mo")
        assert vol == pytest.approx(self.event["volume1mo"])

    def test_1yr(self):
        vol = get_event_volume(self.event, "1yr")
        assert vol == pytest.approx(self.event["volume1yr"])

    def test_lifetime(self):
        vol = get_event_volume(self.event, "lifetime")
        assert vol == pytest.approx(self.event["volume"])

    def test_unrecognised_window_raises(self):
        with pytest.raises(ValueError, match="Unrecognised volume window"):
            get_event_volume(self.event, "weekly")

    def test_missing_field_returns_zero(self):
        # Event without any volume fields (e.g. brand-new event)
        assert get_event_volume({}, "24hr") == 0.0
        assert get_event_volume({}, "lifetime") == 0.0

    def test_returns_float(self):
        assert isinstance(get_event_volume(self.event, "24hr"), float)

    def test_all_windows_return_positive(self):
        for window in ["24hr", "1wk", "1mo", "1yr", "lifetime"]:
            assert get_event_volume(self.event, window) > 0


# ---------------------------------------------------------------------------
# meets_volume_threshold — boundary cases
# ---------------------------------------------------------------------------

class TestMeetsVolumeThreshold:
    """
    Settings are fixed at VOLUME_THRESHOLD=5000, VOLUME_WINDOW=24hr
    (set at module level above). Tests construct explicit events.
    Threshold convention: >= (at-threshold passes).
    """

    def test_above_threshold_passes(self):
        e = _make_event(volume24hr=10000.0)
        assert meets_volume_threshold(e) is True

    def test_below_threshold_fails(self):
        e = _make_event(volume24hr=4999.99)
        assert meets_volume_threshold(e) is False

    def test_exactly_at_threshold_passes(self):
        # >= convention: exactly at threshold must pass
        e = _make_event(volume24hr=5000.0)
        assert meets_volume_threshold(e) is True

    def test_zero_volume_fails(self):
        e = _make_event(volume24hr=0.0)
        assert meets_volume_threshold(e) is False

    def test_missing_volume_field_fails(self):
        # No volume24hr key → treated as 0.0 → below threshold
        assert meets_volume_threshold({}) is False

    def test_fixture_event_with_current_settings(self):
        # Eagles vs. Bears fixture has volume24hr ~7773; threshold=5000 → passes
        e = _load("events_nfl_sample.json")
        assert meets_volume_threshold(e) is True


# ---------------------------------------------------------------------------
# filter_events_by_volume
# ---------------------------------------------------------------------------

class TestFilterEventsByVolume:
    def test_empty_list_returns_empty(self):
        assert filter_events_by_volume([]) == []

    def test_all_above_threshold_pass(self):
        events = [_make_event(volume24hr=6000.0), _make_event(volume24hr=9000.0)]
        assert len(filter_events_by_volume(events)) == 2

    def test_all_below_threshold_removed(self):
        events = [_make_event(volume24hr=100.0), _make_event(volume24hr=200.0)]
        assert filter_events_by_volume(events) == []

    def test_mixed_list_filters_correctly(self):
        above = _make_event(volume24hr=10000.0)
        below = _make_event(volume24hr=1000.0)
        at    = _make_event(volume24hr=5000.0)
        result = filter_events_by_volume([above, below, at])
        assert len(result) == 2
        assert below not in result

    def test_order_preserved(self):
        events = [
            _make_event(volume24hr=8000.0, id="a"),
            _make_event(volume24hr=7000.0, id="b"),
            _make_event(volume24hr=6000.0, id="c"),
        ]
        result = filter_events_by_volume(events)
        assert [e["id"] for e in result] == ["a", "b", "c"]

    def test_fixture_event_passes(self):
        e = _load("events_nfl_sample.json")
        result = filter_events_by_volume([e])
        assert len(result) == 1
