"""
Unit tests for reporter.orchestrator -- consolidated multi-failure
degradation (Phase 4 closeout).

Every individual enrichment-layer failure mode has already been verified
independently (see reporter/tests/test_orchestrator.py and each adapter's
own test suite). This module verifies they COEXIST correctly within one
reporting cycle: a cycle spanning several games, each hitting a different
failure mode simultaneously, must still complete and write a report for
every game, with each failure rendering its own distinct marker and no
failure leaking across games or across sections within the same game's
report.

All tests run offline -- no network access required (every adapter call
is mocked at the reporter.orchestrator boundary, same pattern as
test_orchestrator.py; write_all_reports / write_game_report run for real
so report content is asserted from the actual written files).
"""

import os

os.environ.setdefault("VOLUME_THRESHOLD", "5000")
os.environ.setdefault("VOLUME_WINDOW", "24hr")
os.environ.setdefault("REPORT_OUTPUT_DIR", "/tmp/reporter_test")
os.environ.setdefault("POLLING_INTERVAL_SECONDS", "300")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

from reporter.enrich.injury_adapter import InjuryRecord, InjuryResult  # noqa: E402
from reporter.enrich.situational_context import (  # noqa: E402
    DivisionalGameResult,
    RestDaysResult,
    SituationalContext,
)
from reporter.enrich.team_stats_adapter import TeamStatsResult, WeekContext, WindowStats  # noqa: E402
from reporter.enrich.weather_adapter import WeatherResult  # noqa: E402
from reporter.orchestrator import run_once  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _game(game_id, away_abbr, home_abbr, away_team, home_team, moneyline=None):
    if moneyline is None:
        moneyline = {
            "prices": {away_team: 0.55, home_team: 0.45},
            "open_interest": 12345.67,
            "open_interest_status": "ok",
            "open_interest_reason": None,
            "recent_trades": [],
        }
    return {
        "game_id": game_id,
        "away_team": away_team,
        "home_team": home_team,
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "game_date": "2026-10-05",
        "event_slug": f"nfl-{away_abbr.lower()}-{home_abbr.lower()}-2026-10-05",
        "event_url": f"https://polymarket.com/event/nfl-{away_abbr.lower()}-{home_abbr.lower()}-2026-10-05",
        "volume": 25000.0,
        "moneyline": moneyline,
        "spreads": [],
        "totals": [],
        "game_status": "scheduled",
        "final_score": None,
        "kickoff_utc": None,
    }


def _oi_failed_moneyline(away_team, home_team):
    return {
        "prices": {away_team: 0.60, home_team: 0.40},
        "open_interest": None,
        "open_interest_status": "unavailable",
        "open_interest_reason": "moneyline OI fetch failed: Polymarket /oi 500",
        "recent_trades": [],
    }


_OK_INJURY = InjuryResult(records=[], source="espn", status="no_designations")

_CAPPED_INJURY = InjuryResult(
    records=[
        InjuryRecord(
            player_name="Test Player", position="RB", designation="Out",
            practice_status="unavailable (ESPN source — no structured practice status)",
            injury_type="Knee", source="espn", updated_at="2026-10-01T07:01Z",
        )
    ],
    source="espn", status="ok", possibly_incomplete=True,
)

_OK_WINDOW = WindowStats(
    status="ok", games_used=3,
    epa_offense=0.093, epa_defense=-0.304,
    points_for_avg=27.0, points_against_avg=7.0,
    wins=3, losses=0, ties=0,
)

_OK_WEEK_CONTEXT = WeekContext(season=2026, current_week=5, game_week=5, found=True)


def _ok_team_stats(abbr):
    return TeamStatsResult(team_abbr=abbr, status="ok", season=2026, last4=_OK_WINDOW, season_to_date=_OK_WINDOW)


def _ok_weather(abbr):
    return WeatherResult(
        team_abbr=abbr, status="ok", stadium_name=f"{abbr} Stadium",
        temperature_f=65.0, wind_speed_mph=8.0, precipitation_in=0.0,
        precipitation_probability_pct=10.0,
    )


def _ok_situational(away_abbr, home_abbr):
    return SituationalContext(
        status="ok", away_abbr=away_abbr, home_abbr=home_abbr, season=2026,
        week=5, week_found=True,
        away_rest=RestDaysResult(status="ok", days=7),
        home_rest=RestDaysResult(status="ok", days=7),
        divisional=DivisionalGameResult(status="ok", is_divisional=False),
    )


# ---------------------------------------------------------------------------
# Scenario: 8 games, one failure mode each, plus one clean control
# ---------------------------------------------------------------------------

# Teams carrying each failure mode -- each abbreviation appears in exactly
# one game, so side_effect routing below is unambiguous.
_INJURY_FAIL_TEAM = "PHI"          # both ESPN and nflverse fail
_ESPN_CAP_TEAM = "CHI"             # hits the 25-record league-feed cap
_STATS_FAIL_TEAM = "NYG"           # nflreadpy rolling-stats fetch fails
_WEATHER_HORIZON_HOME = "SEA"      # beyond Open-Meteo's ~14-day horizon
_WEATHER_FAIL_HOME = "ARI"         # actual Open-Meteo fetch error
_SITUATIONAL_FAIL_AWAY = "CIN"     # team not found in schedule data
_SITUATIONAL_FAIL_HOME = "PIT"

_GAMES = [
    _game(1, "KC", "MIA", "Kansas City Chiefs", "Miami Dolphins"),                 # clean control
    _game(2, "BUF", "NYJ", "Buffalo Bills", "New York Jets",
          moneyline=_oi_failed_moneyline("Buffalo Bills", "New York Jets")),       # OI fetch failure
    _game(3, "DAL", _INJURY_FAIL_TEAM, "Dallas Cowboys", "Philadelphia Eagles"),   # injury fetch failure
    _game(4, "GB", _ESPN_CAP_TEAM, "Green Bay Packers", "Chicago Bears"),         # ESPN 25-record cap
    _game(5, "SF", _WEATHER_HORIZON_HOME, "San Francisco 49ers", "Seattle Seahawks"),  # weather horizon
    _game(6, "LAR", _WEATHER_FAIL_HOME, "Los Angeles Rams", "Arizona Cardinals"), # weather fetch failure
    _game(7, "NE", _STATS_FAIL_TEAM, "New England Patriots", "New York Giants"), # team-stats fetch failure
    _game(8, _SITUATIONAL_FAIL_AWAY, _SITUATIONAL_FAIL_HOME, "Cincinnati Bengals", "Pittsburgh Steelers"),  # situational failure
]


def _fake_get_team_injuries(abbr):
    if abbr == _INJURY_FAIL_TEAM:
        raise RuntimeError("ESPN and nflverse both down for PHI")
    if abbr == _ESPN_CAP_TEAM:
        return _CAPPED_INJURY
    return _OK_INJURY


def _fake_get_team_rolling_stats(abbr, before_date):
    if abbr == _STATS_FAIL_TEAM:
        raise RuntimeError("nflreadpy down for NYG")
    return _ok_team_stats(abbr)


def _fake_get_game_weather(home_abbr, game_date, kickoff_utc):
    if home_abbr == _WEATHER_HORIZON_HOME:
        return WeatherResult(
            team_abbr=home_abbr, status="forecast_not_yet_available",
            stadium_name="Lumen Field", days_out=20, days_until_available=6,
        )
    if home_abbr == _WEATHER_FAIL_HOME:
        raise RuntimeError("Open-Meteo down for ARI")
    return _ok_weather(home_abbr)


def _fake_get_situational_context(away_abbr, home_abbr, game_date):
    if away_abbr == _SITUATIONAL_FAIL_AWAY and home_abbr == _SITUATIONAL_FAIL_HOME:
        return SituationalContext(
            status="ok", away_abbr=away_abbr, home_abbr=home_abbr, season=2026,
            week=None, week_found=False,
            away_rest=RestDaysResult(status="unavailable", error="game week not resolved; cannot locate prior game"),
            home_rest=RestDaysResult(status="unavailable", error="game week not resolved; cannot locate prior game"),
            divisional=DivisionalGameResult(status="unavailable", error="game week not resolved; cannot match schedule row"),
        )
    return _ok_situational(away_abbr, home_abbr)


@pytest.fixture
def _report_files(tmp_path, capsys):
    """Run one full orchestrator cycle over the 8-game mixed-failure
    scenario, with every adapter mocked at the orchestrator boundary and
    report writing left to run for real against tmp_path. Returns a dict
    of {filename: content}."""
    with patch("reporter.orchestrator.get_reportable_games", return_value=list(_GAMES)), \
         patch("reporter.orchestrator.get_team_injuries", side_effect=_fake_get_team_injuries), \
         patch("reporter.orchestrator.get_team_rolling_stats", side_effect=_fake_get_team_rolling_stats), \
         patch("reporter.orchestrator.get_game_week_context", return_value=_OK_WEEK_CONTEXT), \
         patch("reporter.orchestrator.get_game_weather", side_effect=_fake_get_game_weather), \
         patch("reporter.orchestrator.get_situational_context", side_effect=_fake_get_situational_context), \
         patch("reporter.report.report_writer.settings") as mock_settings:
        mock_settings.report_output_dir = tmp_path
        run_once()

    out = capsys.readouterr().out
    assert "[reporter] ERROR in cycle" not in out

    files = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file()}
    return files


class TestConsolidatedDegradation:
    def test_all_eight_reports_written(self, _report_files):
        assert len(_report_files) == 8

    # ------------------------------------------------------------------
    # Each failure mode shows its own distinct marker
    # ------------------------------------------------------------------

    def test_oi_failure_marker(self, _report_files):
        content = _report_files["2026-10-05_BUF_at_NYJ.md"]
        assert "**Open Interest:** UNAVAILABLE -- moneyline OI fetch failed: Polymarket /oi 500" in content

    def test_injury_fetch_failure_marker(self, _report_files):
        content = _report_files["2026-10-05_DAL_at_PHI.md"]
        assert "**UNAVAILABLE** -- both ESPN and nflverse injury fetches failed for this team." in content

    def test_espn_cap_marker(self, _report_files):
        content = _report_files["2026-10-05_GB_at_CHI.md"]
        assert "this team's injury list may be incomplete" in content

    def test_weather_horizon_marker(self, _report_files):
        content = _report_files["2026-10-05_SF_at_SEA.md"]
        assert "**FORECAST NOT YET AVAILABLE**" in content

    def test_weather_fetch_failure_marker(self, _report_files):
        content = _report_files["2026-10-05_LAR_at_ARI.md"]
        assert "**UNAVAILABLE** -- weather data could not be fetched (unexpected exception in orchestrator)." in content

    def test_team_stats_failure_marker(self, _report_files):
        content = _report_files["2026-10-05_NE_at_NYG.md"]
        assert "**UNAVAILABLE** -- nflreadpy fetch failed for this team (unexpected exception in orchestrator)." in content

    def test_situational_context_failure_marker(self, _report_files):
        content = _report_files["2026-10-05_CIN_at_PIT.md"]
        assert "**Week of season:** UNAVAILABLE -- could not match this game to schedule data." in content
        assert "UNAVAILABLE -- game week not resolved; cannot locate prior game" in content
        assert "**Divisional matchup:** UNAVAILABLE -- game week not resolved; cannot match schedule row" in content

    # ------------------------------------------------------------------
    # Clean control: zero spurious failure markers
    # ------------------------------------------------------------------

    def test_clean_control_has_no_failure_markers(self, _report_files):
        content = _report_files["2026-10-05_KC_at_MIA.md"]
        assert "UNAVAILABLE" not in content
        assert "FORECAST NOT YET AVAILABLE" not in content
        assert "may be incomplete" not in content

    # ------------------------------------------------------------------
    # Cross-game isolation: a failure in one game's report does not
    # appear in any other game's report.
    # ------------------------------------------------------------------

    def test_oi_failure_isolated_to_its_own_game(self, _report_files):
        for name, content in _report_files.items():
            if name != "2026-10-05_BUF_at_NYJ.md":
                assert "Open Interest:** UNAVAILABLE" not in content

    def test_injury_failure_isolated_to_its_own_game(self, _report_files):
        for name, content in _report_files.items():
            if name != "2026-10-05_DAL_at_PHI.md":
                assert "both ESPN and nflverse injury fetches failed" not in content

    def test_espn_cap_isolated_to_its_own_game(self, _report_files):
        for name, content in _report_files.items():
            if name != "2026-10-05_GB_at_CHI.md":
                assert "may be incomplete" not in content

    def test_weather_horizon_isolated_to_its_own_game(self, _report_files):
        for name, content in _report_files.items():
            if name != "2026-10-05_SF_at_SEA.md":
                assert "FORECAST NOT YET AVAILABLE" not in content

    def test_weather_failure_isolated_to_its_own_game(self, _report_files):
        for name, content in _report_files.items():
            if name != "2026-10-05_LAR_at_ARI.md":
                assert "weather data could not be fetched" not in content

    def test_team_stats_failure_isolated_to_its_own_game(self, _report_files):
        for name, content in _report_files.items():
            if name != "2026-10-05_NE_at_NYG.md":
                assert "nflreadpy fetch failed for this team" not in content

    def test_situational_failure_isolated_to_its_own_game(self, _report_files):
        for name, content in _report_files.items():
            if name != "2026-10-05_CIN_at_PIT.md":
                assert "could not match this game to schedule data" not in content

    # ------------------------------------------------------------------
    # Cross-layer isolation within one game: a failure in one
    # enrichment layer does not affect other sections of the SAME
    # game's report.
    # ------------------------------------------------------------------

    def test_injury_failure_does_not_affect_other_sections_of_its_game(self, _report_files):
        content = _report_files["2026-10-05_DAL_at_PHI.md"]
        # Injury section fails for PHI, but OI, weather, team stats, and
        # situational context for this same game must still be clean.
        assert "Open Interest:** UNAVAILABLE" not in content
        assert "nflreadpy fetch failed for this team" not in content
        assert "weather data could not be fetched" not in content
        assert "FORECAST NOT YET AVAILABLE" not in content
        assert "could not match this game to schedule data" not in content

    def test_weather_horizon_does_not_affect_other_sections_of_its_game(self, _report_files):
        content = _report_files["2026-10-05_SF_at_SEA.md"]
        assert "both ESPN and nflverse injury fetches failed" not in content
        assert "Open Interest:** UNAVAILABLE" not in content
        assert "nflreadpy fetch failed for this team" not in content
        assert "could not match this game to schedule data" not in content

    def test_team_stats_failure_does_not_affect_other_sections_of_its_game(self, _report_files):
        content = _report_files["2026-10-05_NE_at_NYG.md"]
        assert "both ESPN and nflverse injury fetches failed" not in content
        assert "Open Interest:** UNAVAILABLE" not in content
        assert "weather data could not be fetched" not in content
        assert "FORECAST NOT YET AVAILABLE" not in content
        assert "could not match this game to schedule data" not in content

    def test_situational_failure_does_not_affect_other_sections_of_its_game(self, _report_files):
        content = _report_files["2026-10-05_CIN_at_PIT.md"]
        assert "both ESPN and nflverse injury fetches failed" not in content
        assert "Open Interest:** UNAVAILABLE" not in content
        assert "weather data could not be fetched" not in content
        assert "FORECAST NOT YET AVAILABLE" not in content
        assert "nflreadpy fetch failed for this team" not in content
