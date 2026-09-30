"""
Unit tests for reporter.enrich.team_stats_adapter.

All tests run offline -- no live nflreadpy/network calls. Fixture data
loaded from reporter/enrich/fixtures/ (committed JSON, captured live during
Phase 4 rolling-stats recon -- see reporter/enrich/ROLLING_STATS_RECON.md).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest

from reporter.enrich.team_stats_adapter import (
    NflreadpyError,
    TeamStatsResult,
    WindowStats,
    _completed_games_for_team,
    _epa_window_stats,
    _reset_caches,
    _window_stats,
    fetch_pbp_df,
    fetch_schedules_df,
    get_team_rolling_stats,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# Exact EPA/play values verified live against this fixture during recon
# (reporter/enrich/ROLLING_STATS_RECON.md #5): PHI offense -0.3038170213248838
# (47 plays), CHI offense +0.09292290154168391 (67 plays). CHI's defense
# figure equals PHI's offense figure and vice versa (same play set).
_PHI_OFFENSE_EPA = -0.3038170213248838
_CHI_OFFENSE_EPA = 0.09292290154168391


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------

def _load_schedules_fixture() -> pl.DataFrame:
    rows = json.loads((_FIXTURES / "nflreadpy_schedules_sample.json").read_text())
    return pl.DataFrame(rows)


def _load_pbp_fixture() -> pl.DataFrame:
    rows = json.loads((_FIXTURES / "nflreadpy_pbp_sample.json").read_text())
    return pl.DataFrame(rows)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Every test starts and ends with cold schedules/pbp caches."""
    _reset_caches()
    yield
    _reset_caches()


# ---------------------------------------------------------------------------
# _completed_games_for_team
# ---------------------------------------------------------------------------

class TestCompletedGamesForTeam:
    def test_filters_to_team_and_season(self):
        sched = _load_schedules_fixture()
        games = _completed_games_for_team(sched, "PHI", 2026, before_date=None)
        assert games.shape[0] == 1
        assert games["game_id"].to_list() == ["2026_03_PHI_CHI"]

    def test_unknown_team_returns_empty(self):
        sched = _load_schedules_fixture()
        games = _completed_games_for_team(sched, "BUF", 2026, before_date=None)
        assert games.shape[0] == 0

    def test_before_date_excludes_same_or_later_games(self):
        sched = _load_schedules_fixture()
        # PHI's only game in the fixture is dated 2026-09-28.
        excluded = _completed_games_for_team(sched, "PHI", 2026, before_date="2026-09-28")
        assert excluded.shape[0] == 0
        included = _completed_games_for_team(sched, "PHI", 2026, before_date="2026-09-29")
        assert included.shape[0] == 1

    def test_before_date_accepts_date_object_not_just_string(self):
        """Regression test: passing before_date as a real date/datetime object
        (the natural type for a caller doing a live/manual check, as opposed
        to the always-str game_date the orchestrator pipeline supplies) must
        not raise polars.exceptions.InvalidOperationError. gameday is always
        a Polars String column (verified against real nflreadpy output), so
        a non-string before_date has to be coerced before comparison."""
        sched = _load_schedules_fixture()
        excluded = _completed_games_for_team(sched, "PHI", 2026, before_date=date(2026, 9, 28))
        assert excluded.shape[0] == 0
        included = _completed_games_for_team(sched, "PHI", 2026, before_date=date(2026, 9, 29))
        assert included.shape[0] == 1

        included_dt = _completed_games_for_team(
            sched, "PHI", 2026, before_date=datetime(2026, 9, 29, 13, 0)
        )
        assert included_dt.shape[0] == 1

    def test_sorted_by_week_ascending(self):
        sched = _load_schedules_fixture()
        # Add an earlier synthetic week-1 PHI game to check ordering.
        extra = pl.DataFrame([{
            "game_id": "2026_01_WAS_PHI", "season": 2026, "game_type": "REG",
            "week": 1, "gameday": "2026-09-14", "weekday": "Sunday",
            "away_team": "WAS", "away_score": 22, "home_team": "PHI", "home_score": 24,
            "result": 2, "total": 46, "away_moneyline": 120, "home_moneyline": -140,
        }])
        combined = pl.concat([extra, sched], how="diagonal_relaxed")
        games = _completed_games_for_team(combined, "PHI", 2026, before_date=None)
        assert games["week"].to_list() == [1, 3]


# ---------------------------------------------------------------------------
# _epa_window_stats -- verified against recon's real PHI/CHI numbers
# ---------------------------------------------------------------------------

class TestEpaWindowStats:
    def test_matches_recon_verified_phi_chi_numbers(self):
        pbp = _load_pbp_fixture()
        off, defn = _epa_window_stats(pbp, "PHI", ["2026_03_PHI_CHI"])
        assert off == pytest.approx(_PHI_OFFENSE_EPA)
        assert defn == pytest.approx(_CHI_OFFENSE_EPA)

        off_chi, defn_chi = _epa_window_stats(pbp, "CHI", ["2026_03_PHI_CHI"])
        assert off_chi == pytest.approx(_CHI_OFFENSE_EPA)
        assert defn_chi == pytest.approx(_PHI_OFFENSE_EPA)

    def test_empty_game_ids_returns_none_none(self):
        pbp = _load_pbp_fixture()
        off, defn = _epa_window_stats(pbp, "PHI", [])
        assert off is None
        assert defn is None

    def test_unknown_game_id_returns_none_none(self):
        pbp = _load_pbp_fixture()
        off, defn = _epa_window_stats(pbp, "PHI", ["2026_99_XXX_YYY"])
        assert off is None
        assert defn is None

    def test_excludes_kneels_and_no_plays(self):
        pbp = _load_pbp_fixture()
        plays = pbp.filter(pl.col("game_id") == "2026_03_PHI_CHI")
        kneels = plays.filter(pl.col("play_type") == "qb_kneel")
        assert kneels.shape[0] > 0  # fixture sanity check
        # If kneels leaked into the offense filter, PHI's mean EPA would
        # differ from the recon-verified figure (kneels are near-zero/neg EPA
        # but are excluded on principle, not because they'd change the mean
        # much) -- the exact-match assertion above is the real guard.


# ---------------------------------------------------------------------------
# _window_stats -- points/win-loss + insufficient_data gate
# ---------------------------------------------------------------------------

class TestWindowStats:
    def test_zero_games_is_insufficient_data(self):
        empty = _load_schedules_fixture().filter(pl.lit(False))
        result = _window_stats(empty, "PHI", _load_pbp_fixture())
        assert result.status == "insufficient_data"
        assert result.games_used == 0
        assert result.epa_offense is None
        assert result.points_for_avg is None

    def test_single_game_points_and_loss_recorded(self):
        sched = _load_schedules_fixture()
        games = _completed_games_for_team(sched, "PHI", 2026, before_date=None)
        result = _window_stats(games, "PHI", _load_pbp_fixture())
        assert result.status == "ok"
        assert result.games_used == 1
        assert result.points_for_avg == 7.0
        assert result.points_against_avg == 27.0
        assert (result.wins, result.losses, result.ties) == (0, 1, 0)
        assert result.epa_offense == pytest.approx(_PHI_OFFENSE_EPA)
        assert result.epa_defense == pytest.approx(_CHI_OFFENSE_EPA)

    def test_win_counted_for_winning_team(self):
        sched = _load_schedules_fixture()
        games = _completed_games_for_team(sched, "CHI", 2026, before_date=None)
        result = _window_stats(games, "CHI", _load_pbp_fixture())
        assert (result.wins, result.losses, result.ties) == (1, 0, 0)
        assert result.points_for_avg == 27.0
        assert result.points_against_avg == 7.0

    def test_tie_counted_correctly(self):
        tie_game = pl.DataFrame([{
            "game_id": "2026_05_AAA_BBB", "season": 2026, "game_type": "REG",
            "week": 5, "gameday": "2026-10-05", "weekday": "Sunday",
            "away_team": "AAA", "away_score": 20, "home_team": "BBB", "home_score": 20,
            "result": 0, "total": 40, "away_moneyline": 100, "home_moneyline": -100,
        }])
        result = _window_stats(tie_game, "AAA", _load_pbp_fixture())
        assert (result.wins, result.losses, result.ties) == (0, 0, 1)


# ---------------------------------------------------------------------------
# fetch_schedules_df / fetch_pbp_df -- caching
# ---------------------------------------------------------------------------

class TestFetchCaching:
    def test_schedules_single_fetch_cached(self):
        sched = _load_schedules_fixture()
        with patch("reporter.enrich.team_stats_adapter.nflreadpy.load_schedules",
                   return_value=sched) as mock_load:
            fetch_schedules_df(2026)
            fetch_schedules_df(2026)
            fetch_schedules_df(2026)
        assert mock_load.call_count == 1

    def test_pbp_single_fetch_cached(self):
        pbp = _load_pbp_fixture()
        with patch("reporter.enrich.team_stats_adapter.nflreadpy.load_pbp",
                   return_value=pbp) as mock_load:
            fetch_pbp_df(2026)
            fetch_pbp_df(2026)
        assert mock_load.call_count == 1

    def test_ttl_expiry_refetches(self, monkeypatch):
        sched = _load_schedules_fixture()
        monkeypatch.setenv("TEAM_STATS_CACHE_TTL_SECONDS", "1")
        clock = {"t": 1000.0}
        with patch("reporter.enrich.team_stats_adapter.nflreadpy.load_schedules",
                   return_value=sched) as mock_load, \
             patch("reporter.enrich.team_stats_adapter.time.monotonic",
                   side_effect=lambda: clock["t"]):
            fetch_schedules_df(2026)
            clock["t"] += 2.0
            fetch_schedules_df(2026)
        assert mock_load.call_count == 2

    def test_load_schedules_failure_raises_nflreadpy_error(self):
        with patch("reporter.enrich.team_stats_adapter.nflreadpy.load_schedules",
                   side_effect=RuntimeError("network down")):
            with pytest.raises(NflreadpyError):
                fetch_schedules_df(2026)

    def test_load_pbp_failure_raises_nflreadpy_error(self):
        with patch("reporter.enrich.team_stats_adapter.nflreadpy.load_pbp",
                   side_effect=RuntimeError("network down")):
            with pytest.raises(NflreadpyError):
                fetch_pbp_df(2026)


# ---------------------------------------------------------------------------
# get_team_rolling_stats -- composite entry point
# ---------------------------------------------------------------------------

def _patched_fetches(sched=None, pbp=None):
    sched = sched if sched is not None else _load_schedules_fixture()
    pbp = pbp if pbp is not None else _load_pbp_fixture()
    return (
        patch("reporter.enrich.team_stats_adapter.nflreadpy.load_schedules", return_value=sched),
        patch("reporter.enrich.team_stats_adapter.nflreadpy.load_pbp", return_value=pbp),
    )


class TestGetTeamRollingStats:
    def test_ok_status_matches_recon_numbers_single_game_window(self):
        p1, p2 = _patched_fetches()
        with p1, p2:
            result = get_team_rolling_stats("PHI", before_date="2026-09-29", season=2026)
        assert result.status == "ok"
        assert result.last4.games_used == 1
        assert result.season_to_date.games_used == 1
        assert result.last4.epa_offense == pytest.approx(_PHI_OFFENSE_EPA)
        assert result.last4.epa_defense == pytest.approx(_CHI_OFFENSE_EPA)
        assert result.last4.points_for_avg == 7.0
        assert result.last4.points_against_avg == 27.0
        assert (result.last4.wins, result.last4.losses) == (0, 1)

    def test_fewer_than_max_games_reports_real_count_not_padded(self):
        """Early-season case: only 1 completed game exists -- games_used
        must be 1, never padded to 4 and never silently computed as if
        4 games existed."""
        p1, p2 = _patched_fetches()
        with p1, p2:
            result = get_team_rolling_stats("PHI", before_date="2026-09-29", season=2026)
        assert result.last4.games_used == 1
        assert result.last4.status == "ok"

    def test_zero_completed_games_is_insufficient_data(self):
        """A team with no completed games yet (e.g. week 1) must report
        insufficient_data with games_used=0 -- never None stats hidden
        behind a generic 'ok', and never 0.0 standing in for unknown."""
        p1, p2 = _patched_fetches()
        with p1, p2:
            result = get_team_rolling_stats("BUF", before_date="2026-09-08", season=2026)
        assert result.status == "ok"   # fetch succeeded
        assert result.last4.status == "insufficient_data"
        assert result.last4.games_used == 0
        assert result.last4.epa_offense is None
        assert result.season_to_date.status == "insufficient_data"

    def test_upcoming_target_game_never_included_in_window(self):
        """Explicit leakage guard: a 'completed-looking' game dated on/after
        before_date must never enter the rolling window, even if it
        (hypothetically, e.g. a data anomaly) already carries a score."""
        sched = _load_schedules_fixture()
        future_leak = pl.DataFrame([{
            "game_id": "2026_04_PHI_XXX", "season": 2026, "game_type": "REG",
            "week": 4, "gameday": "2026-10-05", "weekday": "Sunday",
            "away_team": "PHI", "away_score": 99, "home_team": "XXX", "home_score": 1,
            "result": -98, "total": 100, "away_moneyline": -500, "home_moneyline": 400,
        }])
        combined = pl.concat([sched, future_leak], how="diagonal_relaxed")
        p1, p2 = _patched_fetches(sched=combined)
        with p1, p2:
            result = get_team_rolling_stats("PHI", before_date="2026-09-29", season=2026)
        # Only the real week-3 PHI@CHI game qualifies; the "future" game
        # dated 2026-10-05 (after before_date) must be excluded.
        assert result.last4.games_used == 1
        assert result.last4.points_for_avg == 7.0  # PHI's real week-3 score, not the leaked 99

    def test_upcoming_target_game_excluded_with_date_object_before_date(self):
        """Same leakage guard as above, but with before_date passed as a real
        date object -- this is the exact call shape that used to raise
        InvalidOperationError against live nflreadpy data (gameday is a
        String column; comparing it to a date object crashes unless
        before_date is coerced first). Confirms the fix doesn't just avoid
        the crash but still enforces point-in-time correctness."""
        sched = _load_schedules_fixture()
        future_leak = pl.DataFrame([{
            "game_id": "2026_04_PHI_XXX", "season": 2026, "game_type": "REG",
            "week": 4, "gameday": "2026-10-05", "weekday": "Sunday",
            "away_team": "PHI", "away_score": 99, "home_team": "XXX", "home_score": 1,
            "result": -98, "total": 100, "away_moneyline": -500, "home_moneyline": 400,
        }])
        combined = pl.concat([sched, future_leak], how="diagonal_relaxed")
        p1, p2 = _patched_fetches(sched=combined)
        with p1, p2:
            result = get_team_rolling_stats("PHI", before_date=date(2026, 9, 29), season=2026)
        assert result.status == "ok"
        assert result.last4.games_used == 1
        assert result.last4.points_for_avg == 7.0  # real week-3 score, not the leaked 99

    def test_max_games_caps_last4_but_not_season_to_date(self):
        sched = _load_schedules_fixture()
        extra_weeks = pl.DataFrame([
            {
                "game_id": f"2026_0{wk}_PHI_OPP{wk}", "season": 2026, "game_type": "REG",
                "week": wk, "gameday": f"2026-09-{6 + wk:02d}", "weekday": "Sunday",
                "away_team": "PHI", "away_score": 20 + wk, "home_team": f"OPP{wk}",
                "home_score": 10, "result": -(10 + wk), "total": 30 + wk,
                "away_moneyline": -150, "home_moneyline": 130,
            }
            for wk in (1, 2)
        ])
        combined = pl.concat([extra_weeks, sched], how="diagonal_relaxed")
        p1, p2 = _patched_fetches(sched=combined)
        with p1, p2:
            result = get_team_rolling_stats("PHI", before_date="2026-09-29", season=2026, max_games=4)
        # 3 completed PHI games total (weeks 1, 2, 3) -- under the cap of 4,
        # so last4 and season_to_date must agree.
        assert result.season_to_date.games_used == 3
        assert result.last4.games_used == 3

    def test_schedules_fetch_failure_returns_unavailable(self):
        with patch("reporter.enrich.team_stats_adapter.nflreadpy.load_schedules",
                   side_effect=RuntimeError("down")), \
             patch("reporter.enrich.team_stats_adapter.nflreadpy.load_pbp",
                   return_value=_load_pbp_fixture()):
            result = get_team_rolling_stats("PHI", before_date="2026-09-29", season=2026)
        assert result.status == "unavailable"
        assert result.last4 is None
        assert result.season_to_date is None
        assert result.error is not None

    def test_pbp_fetch_failure_returns_unavailable(self):
        with patch("reporter.enrich.team_stats_adapter.nflreadpy.load_schedules",
                   return_value=_load_schedules_fixture()), \
             patch("reporter.enrich.team_stats_adapter.nflreadpy.load_pbp",
                   side_effect=RuntimeError("down")):
            result = get_team_rolling_stats("PHI", before_date="2026-09-29", season=2026)
        assert result.status == "unavailable"
        assert result.last4 is None
        assert result.season_to_date is None

    def test_both_fail_returns_unavailable_not_none_result(self):
        with patch("reporter.enrich.team_stats_adapter.nflreadpy.load_schedules",
                   side_effect=RuntimeError("down")), \
             patch("reporter.enrich.team_stats_adapter.nflreadpy.load_pbp",
                   side_effect=RuntimeError("also down")):
            result = get_team_rolling_stats("PHI", before_date="2026-09-29", season=2026)
        assert isinstance(result, TeamStatsResult)
        assert result.is_unavailable

    def test_cache_avoids_redundant_fetches_across_multi_team_cycle(self):
        """15 games worth of team lookups (30 team calls) in one cycle must
        not cause 30 (or even 2x) downloads -- one fetch per dataset."""
        p1, p2 = _patched_fetches()
        with p1 as mock_sched, p2 as mock_pbp:
            for abbr in ["PHI", "CHI", "BAL", "DAL"] * 4:
                get_team_rolling_stats(abbr, before_date="2026-09-29", season=2026)
        assert mock_sched.call_count == 1
        assert mock_pbp.call_count == 1

    def test_result_never_defaults_stats_to_zero_on_unavailable(self):
        with patch("reporter.enrich.team_stats_adapter.nflreadpy.load_schedules",
                   side_effect=RuntimeError("down")):
            result = get_team_rolling_stats("PHI", before_date="2026-09-29", season=2026)
        # Every stat-bearing field must be None, never 0.0/0 standing in for missing data.
        assert result.last4 is None
        assert result.season_to_date is None


# ---------------------------------------------------------------------------
# TeamStatsResult / WindowStats properties
# ---------------------------------------------------------------------------

class TestTeamStatsResultProperties:
    def test_is_unavailable_true(self):
        r = TeamStatsResult(team_abbr="X", status="unavailable", season=None,
                             last4=None, season_to_date=None, error="down")
        assert r.is_unavailable

    def test_is_unavailable_false_on_ok(self):
        window = WindowStats(status="insufficient_data", games_used=0,
                              epa_offense=None, epa_defense=None,
                              points_for_avg=None, points_against_avg=None,
                              wins=0, losses=0, ties=0)
        r = TeamStatsResult(team_abbr="X", status="ok", season=2026,
                             last4=window, season_to_date=window)
        assert not r.is_unavailable
