"""
Unit tests for reporter.watcher.team_mapping.parse_teams_from_event().

All tests run offline using fixture files — no network access required.
"""

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("VOLUME_THRESHOLD", "1000")
os.environ.setdefault("VOLUME_WINDOW", "24hr")
os.environ.setdefault("REPORT_OUTPUT_DIR", "/tmp/reporter_test")
os.environ.setdefault("POLLING_INTERVAL_SECONDS", "300")

from reporter.watcher.team_mapping import (  # noqa: E402
    ABBR_TO_NAME,
    TeamParseError,
    parse_teams_from_event,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _load(name: str):
    with open(_FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(slug: str, title: str = "Away vs. Home") -> dict:
    return {"id": "test", "slug": slug, "title": title}


# ---------------------------------------------------------------------------
# Known slug examples from recon
# ---------------------------------------------------------------------------

KNOWN_SLUG_CASES = [
    # (slug, expected_away_abbr, expected_home_abbr, expected_date)
    ("nfl-lac-buf-2026-09-25",   "LAC", "BUF", "2026-09-25"),
    ("nfl-ari-sf-2026-09-27",    "ARI", "SF",  "2026-09-27"),
    ("nfl-sea-was-2026-09-27",   "SEA", "WAS", "2026-09-27"),
    ("nfl-bal-dal-2026-09-27",   "BAL", "DAL", "2026-09-27"),
    ("nfl-kc-mia-2026-09-27",    "KC",  "MIA", "2026-09-27"),
    # Prop slug with suffix — base pattern still parses correctly
    ("nfl-atl-gb-2026-09-25-highest-scoring-quarter", "ATL", "GB",  "2026-09-25"),
    ("nfl-atl-gb-2026-09-25-first-td-scorer",         "ATL", "GB",  "2026-09-25"),
]


class TestKnownSlugs:
    @pytest.mark.parametrize("slug,away,home,date", KNOWN_SLUG_CASES)
    def test_slug_parses_correctly(self, slug, away, home, date):
        result = parse_teams_from_event(_make_event(slug))
        assert result["away_abbr"] == away
        assert result["home_abbr"] == home
        assert result["game_date"] == date

    @pytest.mark.parametrize("slug,away,home,date", KNOWN_SLUG_CASES)
    def test_abbrs_resolve_to_full_names(self, slug, away, home, date):
        result = parse_teams_from_event(_make_event(slug))
        assert result["away_team"] == ABBR_TO_NAME[away]
        assert result["home_team"] == ABBR_TO_NAME[home]

    def test_away_is_first_named_in_title(self):
        # "Ravens vs. Cowboys" — Ravens are away (slug: nfl-bal-dal-*)
        result = parse_teams_from_event(_make_event("nfl-bal-dal-2026-09-27"))
        assert result["away_abbr"] == "BAL"
        assert result["home_abbr"] == "DAL"


# ---------------------------------------------------------------------------
# Fixture: Eagles vs. Bears game event
# ---------------------------------------------------------------------------

class TestFixtureGameEvent:
    def test_game_event_fixture_parses(self):
        event = _load("events_nfl_sample.json")
        result = parse_teams_from_event(event)
        # slug: nfl-phi-chi-2026-09-29 → PHI away, CHI home
        assert result["away_abbr"] == "PHI"
        assert result["home_abbr"] == "CHI"
        assert result["away_team"] == "Philadelphia Eagles"
        assert result["home_team"] == "Chicago Bears"
        assert result["game_date"] == "2026-09-29"


# ---------------------------------------------------------------------------
# Title fallback
# ---------------------------------------------------------------------------

class TestTitleFallback:
    def test_title_fallback_used_when_slug_missing(self):
        event = {"id": "x", "slug": "", "title": "Baltimore Ravens vs. Dallas Cowboys"}
        result = parse_teams_from_event(event)
        assert result["away_abbr"] == "BAL"
        assert result["home_abbr"] == "DAL"
        assert result["away_team"] == "Baltimore Ravens"
        assert result["home_team"] == "Dallas Cowboys"

    def test_title_fallback_used_when_slug_malformed(self):
        event = {"id": "x", "slug": "some-random-slug", "title": "Kansas City Chiefs vs. Miami Dolphins"}
        result = parse_teams_from_event(event)
        assert result["away_abbr"] == "KC"
        assert result["home_abbr"] == "MIA"

    def test_title_with_prop_suffix_stripped(self):
        # Titles like "Ravens vs. Cowboys - Highest Scoring Quarter"
        event = _make_event(
            slug="nfl-bal-dal-2026-09-27-highest-scoring-quarter",
            title="Ravens vs. Cowboys - Highest Scoring Quarter",
        )
        # Slug is primary — should still parse correctly
        result = parse_teams_from_event(event)
        assert result["away_abbr"] == "BAL"
        assert result["home_abbr"] == "DAL"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestParseErrors:
    def test_malformed_slug_and_title_raises(self):
        event = {"id": "bad", "slug": "rugby-nz-aus-2026-09-01", "title": "No Teams Here"}
        with pytest.raises(TeamParseError, match="Cannot parse team identity"):
            parse_teams_from_event(event)

    def test_unknown_abbreviation_raises(self):
        event = _make_event("nfl-xyz-dal-2026-09-27")
        with pytest.raises(TeamParseError, match="Unknown team abbreviation"):
            parse_teams_from_event(event)

    def test_empty_event_raises(self):
        with pytest.raises(TeamParseError):
            parse_teams_from_event({})

    def test_slug_only_no_date_raises(self):
        event = _make_event("nfl-bal-dal")  # missing date segment
        with pytest.raises(TeamParseError):
            parse_teams_from_event(event)


# ---------------------------------------------------------------------------
# Coverage: all 32 abbreviations are in the mapping
# ---------------------------------------------------------------------------

class TestMapping:
    ALL_32 = [
        "ARI","ATL","BAL","BUF","CAR","CHI","CIN","CLE",
        "DAL","DEN","DET","GB","HOU","IND","JAX","KC",
        "LA","LAC","LV","MIA","MIN","NE","NO","NYG",
        "NYJ","PHI","PIT","SEA","SF","TB","TEN","WAS",
    ]

    def test_all_32_teams_present(self):
        for abbr in self.ALL_32:
            assert abbr in ABBR_TO_NAME, f"{abbr} missing from ABBR_TO_NAME"

    def test_mapping_has_exactly_32_entries(self):
        assert len(ABBR_TO_NAME) == 32
