"""
Unit tests for reporter.enrich.situational_context.

All tests run offline -- no live nflreadpy/network calls. Fixture data
loaded from reporter/enrich/fixtures/nflreadpy_schedules_situational_sample.json
(committed JSON, captured live during Phase 4 situational-context recon --
see reporter/enrich/SITUATIONAL_RECON.md).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest

from reporter.enrich.situational_context import (
    DivisionalGameResult,
    RestDaysResult,
    SituationalContext,
    _divisional_flag,
    _parse_iso_date,
    _team_rest_days,
    get_situational_context,
)
from reporter.enrich.team_stats_adapter import NflreadpyError, WeekContext

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _load_schedules_fixture() -> pl.DataFrame:
    rows = json.loads((_FIXTURES / "nflreadpy_schedules_situational_sample.json").read_text())
    return pl.DataFrame(rows)


# ---------------------------------------------------------------------------
# _parse_iso_date
# ---------------------------------------------------------------------------

class TestParseIsoDate:
    def test_parses_valid_string(self):
        assert _parse_iso_date("2026-09-28") == date(2026, 9, 28)

    def test_passes_through_date_object(self):
        assert _parse_iso_date(date(2026, 9, 28)) == date(2026, 9, 28)

    def test_none_returns_none(self):
        assert _parse_iso_date(None) is None

    def test_garbage_string_returns_none(self):
        assert _parse_iso_date("not-a-date") is None


# ---------------------------------------------------------------------------
# _team_rest_days -- verified live against real 2026 schedule data (see
# SITUATIONAL_RECON.md): PHI's week-3 game (2026-09-28) is 8 days after its
# week-2 game (2026-09-20), matching nflreadpy's own away_rest=8 for that row.
# ---------------------------------------------------------------------------

class TestTeamRestDays:
    def test_real_prior_game_matches_hand_calculation(self):
        sched = _load_schedules_fixture()
        result = _team_rest_days(sched, "PHI", 2026, week=3, game_date_obj=date(2026, 9, 28))
        assert result == RestDaysResult(status="ok", days=8)

    def test_home_team_real_prior_game(self):
        sched = _load_schedules_fixture()
        # CHI's week-2 game (vs MIN) was 2026-09-20; its week-3 game (vs PHI)
        # is 2026-09-28 -- 8 days, same gap as PHI's side of this matchup.
        result = _team_rest_days(sched, "CHI", 2026, week=3, game_date_obj=date(2026, 9, 28))
        assert result == RestDaysResult(status="ok", days=8)

    def test_season_opener_has_no_prior_game(self):
        """Week 1 has no prior game this season -- must be an explicit
        season_opener state, never a 0 or any numeric default (nflreadpy's
        own away_rest/home_rest columns default to a misleading flat 7
        here -- see SITUATIONAL_RECON.md)."""
        sched = _load_schedules_fixture()
        result = _team_rest_days(sched, "SEA", 2026, week=1, game_date_obj=date(2026, 9, 9))
        assert result.status == "season_opener"
        assert result.days is None

    def test_week_not_resolved_is_unavailable(self):
        sched = _load_schedules_fixture()
        result = _team_rest_days(sched, "PHI", 2026, week=None, game_date_obj=date(2026, 9, 28))
        assert result.status == "unavailable"
        assert result.days is None

    def test_unparsable_game_date_is_unavailable(self):
        sched = _load_schedules_fixture()
        result = _team_rest_days(sched, "PHI", 2026, week=3, game_date_obj=None)
        assert result.status == "unavailable"

    def test_unparsable_prior_gameday_is_unavailable(self):
        bad_row = pl.DataFrame([{
            "game_id": "2026_01_XXX_ZZZ", "season": 2026, "game_type": "REG",
            "week": 1, "gameday": "not-a-real-date", "weekday": "Sunday",
            "away_team": "XXX", "away_score": None, "home_team": "ZZZ", "home_score": None,
            "div_game": 0, "away_rest": None, "home_rest": None,
        }])
        result = _team_rest_days(bad_row, "ZZZ", 2026, week=2, game_date_obj=date(2026, 9, 28))
        assert result.status == "unavailable"
        assert "could not parse" in result.error


# ---------------------------------------------------------------------------
# _divisional_flag -- verified by hand against known 2026 divisions (see
# SITUATIONAL_RECON.md): CIN/PIT (AFC North), ARI/SF (NFC West) are
# divisional; SEA/WAS are not.
# ---------------------------------------------------------------------------

class TestDivisionalFlag:
    def test_real_divisional_matchup(self):
        sched = _load_schedules_fixture()
        result = _divisional_flag(sched, "CIN", "PIT", 2026, week=3)
        assert result == DivisionalGameResult(status="ok", is_divisional=True)

    def test_real_non_divisional_matchup(self):
        sched = _load_schedules_fixture()
        result = _divisional_flag(sched, "SEA", "WAS", 2026, week=3)
        assert result == DivisionalGameResult(status="ok", is_divisional=False)

    def test_another_real_divisional_matchup(self):
        sched = _load_schedules_fixture()
        result = _divisional_flag(sched, "ARI", "SF", 2026, week=3)
        assert result == DivisionalGameResult(status="ok", is_divisional=True)

    def test_week_not_resolved_is_unavailable(self):
        sched = _load_schedules_fixture()
        result = _divisional_flag(sched, "CIN", "PIT", 2026, week=None)
        assert result.status == "unavailable"
        assert result.is_divisional is None

    def test_no_matching_row_is_unavailable(self):
        sched = _load_schedules_fixture()
        # Teams swapped relative to the real home/away orientation --
        # no row matches this exact ordering.
        result = _divisional_flag(sched, "PIT", "CIN", 2026, week=3)
        assert result.status == "unavailable"

    def test_missing_div_game_column_is_unavailable(self):
        sched = _load_schedules_fixture().drop("div_game")
        result = _divisional_flag(sched, "CIN", "PIT", 2026, week=3)
        assert result.status == "unavailable"
        assert "div_game" in result.error


# ---------------------------------------------------------------------------
# get_situational_context -- composite entry point
# ---------------------------------------------------------------------------

_WEEK3_CTX = WeekContext(season=2026, current_week=3, game_week=3, found=True)
_WEEK1_CTX = WeekContext(season=2026, current_week=1, game_week=1, found=True)
_NOT_FOUND_CTX = WeekContext(season=2026, current_week=3, game_week=None, found=False)


class TestGetSituationalContext:
    def test_happy_path_real_matchup(self):
        sched = _load_schedules_fixture()
        with patch("reporter.enrich.situational_context.get_game_week_context", return_value=_WEEK3_CTX), \
             patch("reporter.enrich.situational_context.fetch_schedules_df", return_value=sched):
            result = get_situational_context("PHI", "CHI", "2026-09-28")

        assert result.status == "ok"
        assert result.week == 3
        assert result.week_found is True
        assert result.away_rest == RestDaysResult(status="ok", days=8)
        assert result.home_rest == RestDaysResult(status="ok", days=8)
        assert result.divisional == DivisionalGameResult(status="ok", is_divisional=False)

    def test_season_opener_matchup(self):
        sched = _load_schedules_fixture()
        with patch("reporter.enrich.situational_context.get_game_week_context", return_value=_WEEK1_CTX), \
             patch("reporter.enrich.situational_context.fetch_schedules_df", return_value=sched):
            result = get_situational_context("NE", "SEA", "2026-09-09")

        assert result.status == "ok"
        assert result.away_rest.status == "season_opener"
        assert result.home_rest.status == "season_opener"
        assert result.divisional.status == "ok"
        assert result.divisional.is_divisional is False

    def test_week_not_found_marks_pieces_unavailable_but_top_level_ok(self):
        """A schedule-match miss is a per-piece failure, not a fetch
        failure -- the overall fetch succeeded, so status stays 'ok' and
        each sub-piece independently reports why it couldn't resolve."""
        sched = _load_schedules_fixture()
        with patch("reporter.enrich.situational_context.get_game_week_context", return_value=_NOT_FOUND_CTX), \
             patch("reporter.enrich.situational_context.fetch_schedules_df", return_value=sched):
            result = get_situational_context("PHI", "CHI", "2026-09-28")

        assert result.status == "ok"
        assert result.week is None
        assert result.week_found is False
        assert result.away_rest.status == "unavailable"
        assert result.home_rest.status == "unavailable"
        assert result.divisional.status == "unavailable"

    def test_week_context_fetch_failure_is_top_level_unavailable(self):
        with patch("reporter.enrich.situational_context.get_game_week_context",
                   side_effect=NflreadpyError("nflreadpy down")):
            result = get_situational_context("PHI", "CHI", "2026-09-28")

        assert result.status == "unavailable"
        assert result.away_rest is None
        assert result.home_rest is None
        assert result.divisional is None
        assert result.error == "nflreadpy down"

    def test_schedules_fetch_failure_is_top_level_unavailable(self):
        with patch("reporter.enrich.situational_context.get_game_week_context", return_value=_WEEK3_CTX), \
             patch("reporter.enrich.situational_context.fetch_schedules_df",
                   side_effect=NflreadpyError("nflreadpy down")):
            result = get_situational_context("PHI", "CHI", "2026-09-28")

        assert result.status == "unavailable"
        assert result.away_rest is None

    def test_result_is_situational_context_instance(self):
        sched = _load_schedules_fixture()
        with patch("reporter.enrich.situational_context.get_game_week_context", return_value=_WEEK3_CTX), \
             patch("reporter.enrich.situational_context.fetch_schedules_df", return_value=sched):
            result = get_situational_context("PHI", "CHI", "2026-09-28")
        assert isinstance(result, SituationalContext)
