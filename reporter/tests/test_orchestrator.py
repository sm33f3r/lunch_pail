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
from reporter.enrich.team_stats_adapter import TeamStatsResult, WindowStats  # noqa: E402
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
