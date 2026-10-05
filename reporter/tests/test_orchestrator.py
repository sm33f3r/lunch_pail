"""
Unit tests for reporter.orchestrator.

All tests run offline -- no network access required.
"""

import os
from unittest.mock import MagicMock, call, patch

import pytest

os.environ.setdefault("VOLUME_THRESHOLD", "5000")
os.environ.setdefault("VOLUME_WINDOW", "24hr")
os.environ.setdefault("REPORT_OUTPUT_DIR", "/tmp/reporter_test")
os.environ.setdefault("POLLING_INTERVAL_SECONDS", "300")

from reporter.enrich.injury_adapter import InjuryResult  # noqa: E402
from reporter.enrich.situational_context import (  # noqa: E402
    DivisionalGameResult,
    RestDaysResult,
    SituationalContext,
)
from reporter.enrich.team_stats_adapter import (  # noqa: E402
    NflreadpyError,
    TeamStatsResult,
    WeekContext,
    WindowStats,
)
from reporter.enrich.weather_adapter import WeatherResult  # noqa: E402
from reporter.orchestrator import run_forever, run_once  # noqa: E402


def _game(away_abbr="KC", home_abbr="MIA", game_date="2026-09-28"):
    return {"away_abbr": away_abbr, "home_abbr": home_abbr, "game_date": game_date}


_OK_RESULT = InjuryResult(records=[], source="espn", status="no_designations")

_OK_WINDOW = WindowStats(
    status="ok", games_used=3,
    epa_offense=0.093, epa_defense=-0.304,
    points_for_avg=27.0, points_against_avg=7.0,
    wins=3, losses=0, ties=0,
)
_OK_TEAM_STATS = TeamStatsResult(
    team_abbr="KC", status="ok", season=2026,
    last4=_OK_WINDOW, season_to_date=_OK_WINDOW,
)

# Current-week game by default so existing tests (which don't care about
# week context) don't pick up an unexpected staleness note.
_OK_WEEK_CONTEXT = WeekContext(season=2026, current_week=3, game_week=3, found=True)

_OK_WEATHER = WeatherResult(
    team_abbr="MIA", status="ok", stadium_name="Hard Rock Stadium",
    temperature_f=78.0, wind_speed_mph=6.0, precipitation_in=0.0,
    precipitation_probability_pct=5.0,
)

_OK_SITUATIONAL = SituationalContext(
    status="ok", away_abbr="KC", home_abbr="MIA", season=2026,
    week=3, week_found=True,
    away_rest=RestDaysResult(status="ok", days=7),
    home_rest=RestDaysResult(status="ok", days=7),
    divisional=DivisionalGameResult(status="ok", is_divisional=False),
)


@pytest.fixture(autouse=True)
def _patch_week_context():
    """
    Every test in this module gets a real-game, non-stale week context by
    default -- this stage makes a schedule (nflreadpy) call in production,
    and tests must stay offline. Tests that specifically exercise the
    week-context wiring override this with their own nested patch.
    """
    with patch("reporter.orchestrator.get_game_week_context", return_value=_OK_WEEK_CONTEXT):
        yield


@pytest.fixture(autouse=True)
def _patch_weather():
    """
    Every test in this module gets a successful weather fetch by default --
    this stage makes a live Open-Meteo call in production, and tests must
    stay offline. Tests that specifically exercise the weather wiring
    override this with their own nested patch.
    """
    with patch("reporter.orchestrator.get_game_weather", return_value=_OK_WEATHER):
        yield


@pytest.fixture(autouse=True)
def _patch_situational_context():
    """
    Every test in this module gets a successful situational-context fetch
    by default -- this stage makes a schedule (nflreadpy) call in
    production, and tests must stay offline. Tests that specifically
    exercise the situational-context wiring override this with their own
    nested patch.
    """
    with patch("reporter.orchestrator.get_situational_context", return_value=_OK_SITUATIONAL):
        yield


class TestRunOnce:
    def test_calls_write_all_reports(self):
        with patch("reporter.orchestrator.get_reportable_games", return_value=[]), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()
        mock_write.assert_called_once()

    def test_returns_none(self):
        with patch("reporter.orchestrator.get_reportable_games", return_value=[]), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]):
            result = run_once()
        assert result is None

    def test_exception_does_not_propagate(self):
        # A transient error in write_all_reports must not crash run_once.
        with patch("reporter.orchestrator.get_reportable_games", return_value=[]), \
             patch("reporter.orchestrator.write_all_reports", side_effect=RuntimeError("API timeout")):
            run_once()  # must not raise

    def test_exception_from_connection_error_does_not_propagate(self):
        import requests
        with patch("reporter.orchestrator.get_reportable_games", return_value=[]), \
             patch("reporter.orchestrator.write_all_reports", side_effect=requests.ConnectionError("network down")):
            run_once()  # must not raise

    def test_prints_summary_with_games(self, capsys):
        from pathlib import Path
        paths = [Path("/tmp/2026-09-28_KC_at_MIA.md"), Path("/tmp/2026-09-28_PHI_at_NYG.md")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=[]), \
             patch("reporter.orchestrator.write_all_reports", return_value=paths):
            run_once()
        out = capsys.readouterr().out
        assert "2" in out
        assert "KC_at_MIA" in out

    def test_prints_zero_games_message(self, capsys):
        with patch("reporter.orchestrator.get_reportable_games", return_value=[]), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]):
            run_once()
        out = capsys.readouterr().out
        assert "0" in out

    def test_prints_warning_for_failed_reports(self, capsys):
        # write_all_reports.failed carries (filename, exception) tuples for
        # games that failed to write -- run_once must surface them as a
        # final WARNING line.
        from pathlib import Path
        written = [Path("/tmp/2026-09-28_KC_at_MIA.md")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=[]), \
             patch(
                 "reporter.orchestrator.write_all_reports",
                 return_value=written,
             ) as mock_write:
            mock_write.failed = [("2026-09-28_PHI_at_NYG.md", ValueError("boom"))]
            run_once()
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "1" in out
        assert "2026-09-28_PHI_at_NYG.md" in out

    def test_no_warning_line_when_nothing_failed(self, capsys):
        with patch("reporter.orchestrator.get_reportable_games", return_value=[]), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            mock_write.failed = []
            run_once()
        out = capsys.readouterr().out
        assert "WARNING" not in out

    # ------------------------------------------------------------------
    # Injury-fetch wiring (Phase 4 Step 3 closeout)
    # ------------------------------------------------------------------

    def test_fetches_injuries_for_both_teams_of_every_game(self):
        games = [_game("KC", "MIA"), _game("PHI", "NYG")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT) as mock_fetch, \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]):
            run_once()
        mock_fetch.assert_has_calls(
            [call("KC"), call("MIA"), call("PHI"), call("NYG")], any_order=True
        )
        assert mock_fetch.call_count == 4

    def test_injuries_attached_to_games_passed_to_write_all_reports(self):
        games = [_game("KC", "MIA")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()
        written_games = mock_write.call_args[0][0]
        assert written_games[0]["away_injuries"] is _OK_RESULT
        assert written_games[0]["home_injuries"] is _OK_RESULT

    def test_one_team_injury_failure_does_not_abort_game_report(self):
        games = [_game("KC", "MIA")]

        def flaky(abbr):
            if abbr == "MIA":
                raise RuntimeError("ESPN and nflverse both down")
            return _OK_RESULT

        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", side_effect=flaky), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()  # must not raise

        written_games = mock_write.call_args[0][0]
        assert written_games[0]["away_injuries"] is _OK_RESULT
        assert written_games[0]["home_injuries"].status == "unavailable"

    # ------------------------------------------------------------------
    # Team-stats-fetch wiring (Phase 4 Step 6b)
    # ------------------------------------------------------------------

    def test_fetches_team_stats_for_both_teams_of_every_game(self):
        games = [_game("KC", "MIA"), _game("PHI", "NYG")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS) as mock_fetch, \
             patch("reporter.orchestrator.write_all_reports", return_value=[]):
            run_once()
        mock_fetch.assert_has_calls(
            [call("KC", "2026-09-28"), call("MIA", "2026-09-28"),
             call("PHI", "2026-09-28"), call("NYG", "2026-09-28")],
            any_order=True,
        )
        assert mock_fetch.call_count == 4

    def test_team_stats_attached_to_games_passed_to_write_all_reports(self):
        games = [_game("KC", "MIA")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()
        written_games = mock_write.call_args[0][0]
        assert written_games[0]["away_team_stats"] is _OK_TEAM_STATS
        assert written_games[0]["home_team_stats"] is _OK_TEAM_STATS

    def test_one_team_stats_failure_does_not_abort_game_report(self):
        games = [_game("KC", "MIA")]

        def flaky(abbr, before_date):
            if abbr == "MIA":
                raise RuntimeError("nflreadpy down")
            return _OK_TEAM_STATS

        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", side_effect=flaky), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()  # must not raise

        written_games = mock_write.call_args[0][0]
        assert written_games[0]["away_team_stats"] is _OK_TEAM_STATS
        assert written_games[0]["home_team_stats"].status == "unavailable"

    def test_one_game_team_stats_failure_does_not_abort_other_games(self):
        games = [_game("KC", "MIA"), _game("PHI", "NYG")]

        def flaky(abbr, before_date):
            if abbr in ("KC", "MIA"):
                raise RuntimeError("nflreadpy down")
            return _OK_TEAM_STATS

        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", side_effect=flaky), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()  # must not raise

        written_games = mock_write.call_args[0][0]
        assert written_games[0]["away_team_stats"].status == "unavailable"
        assert written_games[0]["home_team_stats"].status == "unavailable"
        assert written_games[1]["away_team_stats"] is _OK_TEAM_STATS
        assert written_games[1]["home_team_stats"] is _OK_TEAM_STATS

    def test_game_assembly_failure_does_not_crash_run_once(self):
        with patch("reporter.orchestrator.get_reportable_games", side_effect=RuntimeError("assembly down")):
            run_once()  # must not raise

    # ------------------------------------------------------------------
    # Week-context wiring (game-relative injury staleness check)
    # ------------------------------------------------------------------

    def test_fetches_week_context_for_every_game(self):
        games = [_game("KC", "MIA"), _game("PHI", "NYG")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_game_week_context", return_value=_OK_WEEK_CONTEXT) as mock_fetch, \
             patch("reporter.orchestrator.write_all_reports", return_value=[]):
            run_once()
        mock_fetch.assert_has_calls(
            [call("KC", "2026-09-28"), call("PHI", "2026-09-28")], any_order=True
        )
        assert mock_fetch.call_count == 2

    def test_week_context_attached_to_games_passed_to_write_all_reports(self):
        games = [_game("KC", "MIA")]
        future_ctx = WeekContext(season=2026, current_week=3, game_week=7, found=True)
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_game_week_context", return_value=future_ctx), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()
        written_games = mock_write.call_args[0][0]
        assert written_games[0]["week_context"] is future_ctx

    def test_week_context_none_when_schedule_fetch_fails(self):
        """No silent defaults: a schedule fetch failure must attach None,
        never a guessed/default WeekContext -- must not crash the cycle."""
        games = [_game("KC", "MIA")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_game_week_context",
                   side_effect=NflreadpyError("nflreadpy down")), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()  # must not raise
        written_games = mock_write.call_args[0][0]
        assert written_games[0]["week_context"] is None

    def test_week_context_none_when_game_not_found_in_schedule(self):
        """found=False (e.g. bye-week/date mismatch) must attach None, not
        a WeekContext that looks like a real answer."""
        games = [_game("KC", "MIA")]
        not_found_ctx = WeekContext(season=2026, current_week=3, game_week=None, found=False)
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_game_week_context", return_value=not_found_ctx), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()
        written_games = mock_write.call_args[0][0]
        assert written_games[0]["week_context"] is None

    # ------------------------------------------------------------------
    # Weather-fetch wiring (Phase 4 Step 6c)
    # ------------------------------------------------------------------

    def test_fetches_weather_for_home_team_only(self):
        games = [_game("KC", "MIA"), _game("PHI", "NYG")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_game_weather", return_value=_OK_WEATHER) as mock_fetch, \
             patch("reporter.orchestrator.write_all_reports", return_value=[]):
            run_once()
        # Weather is a venue property -- only the HOME team's stadium is
        # looked up, never the away team's.
        mock_fetch.assert_has_calls(
            [call("MIA", "2026-09-28", None), call("NYG", "2026-09-28", None)],
            any_order=True,
        )
        assert mock_fetch.call_count == 2

    def test_weather_attached_to_games_passed_to_write_all_reports(self):
        games = [_game("KC", "MIA")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_game_weather", return_value=_OK_WEATHER), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()
        written_games = mock_write.call_args[0][0]
        assert written_games[0]["weather"] is _OK_WEATHER

    def test_weather_passes_kickoff_utc_when_present(self):
        games = [_game("KC", "MIA")]
        games[0]["kickoff_utc"] = "2026-09-28T17:00:00Z"
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_game_weather", return_value=_OK_WEATHER) as mock_fetch, \
             patch("reporter.orchestrator.write_all_reports", return_value=[]):
            run_once()
        mock_fetch.assert_called_once_with("MIA", "2026-09-28", "2026-09-28T17:00:00Z")

    def test_one_game_weather_failure_does_not_abort_other_games(self):
        games = [_game("KC", "MIA"), _game("PHI", "NYG")]

        def flaky(abbr, game_date, kickoff_utc):
            if abbr == "MIA":
                raise RuntimeError("Open-Meteo down")
            return _OK_WEATHER

        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_game_weather", side_effect=flaky), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()  # must not raise

        written_games = mock_write.call_args[0][0]
        assert written_games[0]["weather"].status == "unavailable"
        assert written_games[1]["weather"] is _OK_WEATHER

    # ------------------------------------------------------------------
    # Situational-context-fetch wiring (Phase 4 final enrichment layer)
    # ------------------------------------------------------------------

    def test_fetches_situational_context_for_every_game(self):
        games = [_game("KC", "MIA"), _game("PHI", "NYG")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_situational_context", return_value=_OK_SITUATIONAL) as mock_fetch, \
             patch("reporter.orchestrator.write_all_reports", return_value=[]):
            run_once()
        mock_fetch.assert_has_calls(
            [call("KC", "MIA", "2026-09-28"), call("PHI", "NYG", "2026-09-28")],
            any_order=True,
        )
        assert mock_fetch.call_count == 2

    def test_situational_context_attached_to_games_passed_to_write_all_reports(self):
        games = [_game("KC", "MIA")]
        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_situational_context", return_value=_OK_SITUATIONAL), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()
        written_games = mock_write.call_args[0][0]
        assert written_games[0]["situational_context"] is _OK_SITUATIONAL

    def test_one_game_situational_context_failure_does_not_abort_other_games(self):
        games = [_game("KC", "MIA"), _game("PHI", "NYG")]

        def flaky(away_abbr, home_abbr, game_date):
            if away_abbr == "KC":
                raise RuntimeError("nflreadpy down")
            return _OK_SITUATIONAL

        with patch("reporter.orchestrator.get_reportable_games", return_value=games), \
             patch("reporter.orchestrator.get_team_injuries", return_value=_OK_RESULT), \
             patch("reporter.orchestrator.get_team_rolling_stats", return_value=_OK_TEAM_STATS), \
             patch("reporter.orchestrator.get_situational_context", side_effect=flaky), \
             patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()  # must not raise

        written_games = mock_write.call_args[0][0]
        assert written_games[0]["situational_context"].status == "unavailable"
        assert written_games[1]["situational_context"] is _OK_SITUATIONAL


class TestRunForever:
    def test_calls_run_once_in_loop(self):
        # Stop the loop after 3 sleep calls by raising KeyboardInterrupt.
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 3:
                raise KeyboardInterrupt

        with patch("reporter.orchestrator.run_once") as mock_run_once, \
             patch("reporter.orchestrator.time.sleep", side_effect=fake_sleep):
            with pytest.raises(KeyboardInterrupt):
                run_forever()

        assert mock_run_once.call_count == 3

    def test_sleeps_with_configured_interval(self):
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 2:
                raise KeyboardInterrupt

        with patch("reporter.orchestrator.run_once"), \
             patch("reporter.orchestrator.time.sleep", side_effect=fake_sleep):
            with pytest.raises(KeyboardInterrupt):
                run_forever()

        from reporter.config import settings
        assert all(s == settings.polling_interval_seconds for s in sleep_calls)

    def test_keyboard_interrupt_propagates(self):
        with patch("reporter.orchestrator.run_once"), \
             patch("reporter.orchestrator.time.sleep", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                run_forever()
