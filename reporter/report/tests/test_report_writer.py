"""
Unit tests for reporter.report.report_writer.

All tests run offline — no network access required.
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("VOLUME_THRESHOLD", "5000")
os.environ.setdefault("VOLUME_WINDOW", "24hr")
os.environ.setdefault("REPORT_OUTPUT_DIR", "/tmp/reporter_test")
os.environ.setdefault("POLLING_INTERVAL_SECONDS", "300")

from reporter.enrich.injury_adapter import InjuryRecord, InjuryResult  # noqa: E402
from reporter.enrich.team_stats_adapter import TeamStatsResult, WeekContext, WindowStats  # noqa: E402
from reporter.report.report_writer import (  # noqa: E402
    format_game_report,
    write_all_reports,
    write_game_report,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _ok_injury_result(source="espn") -> InjuryResult:
    rec = InjuryRecord(
        player_name="Test Player",
        position="WR",
        designation="Questionable",
        practice_status="unavailable (ESPN source — no structured practice status)" if source == "espn" else "Limited Participation in Practice",
        injury_type="Ankle",
        source=source,
        updated_at="2026-09-24T07:01Z" if source == "espn" else None,
        short_comment="Test Player (ankle) did not participate in Wednesday's practice." if source == "espn" else None,
        location="Leg" if source == "espn" else None,
        side=None,
        return_date="2026-09-27" if source == "espn" else None,
    )
    return InjuryResult(records=[rec], source=source, status="ok")


def _ok_window_stats(games_used=4) -> WindowStats:
    return WindowStats(
        status="ok", games_used=games_used,
        epa_offense=0.093, epa_defense=-0.304,
        points_for_avg=27.0, points_against_avg=7.0,
        wins=games_used, losses=0, ties=0,
    )


def _ok_team_stats(team_abbr="BAL") -> TeamStatsResult:
    return TeamStatsResult(
        team_abbr=team_abbr, status="ok", season=2026,
        last4=_ok_window_stats(3),
        season_to_date=_ok_window_stats(3),
    )


def _sample_game(
    game_id=101,
    away_abbr="BAL",
    home_abbr="DAL",
    away_team="Baltimore Ravens",
    home_team="Dallas Cowboys",
    game_date="2026-09-27",
    volume=25000.0,
    moneyline=None,
    spreads=None,
    totals=None,
    away_injuries=None,
    home_injuries=None,
    away_team_stats=None,
    home_team_stats=None,
    week_context=None,
    game_status=None,
    final_score=None,
) -> dict:
    if moneyline is None:
        moneyline = {
            "prices": {"Baltimore Ravens": 0.48, "Dallas Cowboys": 0.52},
            "open_interest": 12345.67,
            "recent_trades": [
                {"side": "BUY",  "size": "200", "price": "0.48", "timestamp": "2026-09-23T10:00:00Z"},
                {"side": "SELL", "size": "100", "price": "0.52", "timestamp": "2026-09-23T09:55:00Z"},
            ],
        }
    if spreads is None:
        spreads = [{"prices": {"Away": 0.48, "Home": 0.52}, "line": "-3.5"}]
    if totals is None:
        totals = [{"prices": {"Over": 0.51, "Under": 0.49}, "line": "44.5"}]
    if away_injuries is None:
        away_injuries = _ok_injury_result("espn")
    if home_injuries is None:
        home_injuries = _ok_injury_result("espn")
    if away_team_stats is None:
        away_team_stats = _ok_team_stats(away_abbr)
    if home_team_stats is None:
        home_team_stats = _ok_team_stats(home_abbr)
    return {
        "game_id":   game_id,
        "away_team": away_team,
        "home_team": home_team,
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "game_date": game_date,
        "away_injuries": away_injuries,
        "home_injuries": home_injuries,
        "away_team_stats": away_team_stats,
        "home_team_stats": home_team_stats,
        "week_context": week_context,
        "event_slug": f"nfl-{away_abbr.lower()}-{home_abbr.lower()}-{game_date}",
        "event_url":  f"https://polymarket.com/event/nfl-{away_abbr.lower()}-{home_abbr.lower()}-{game_date}",
        "volume":     volume,
        "moneyline":  moneyline,
        "spreads":    spreads,
        "totals":     totals,
        "game_status": game_status,
        "final_score": final_score,
    }


_GAME = _sample_game()


# ---------------------------------------------------------------------------
# format_game_report — content tests
# ---------------------------------------------------------------------------

class TestFormatGameReport:
    def test_returns_string(self):
        assert isinstance(format_game_report(_GAME), str)

    def test_h1_contains_team_names(self):
        report = format_game_report(_GAME)
        assert "# Baltimore Ravens @ Dallas Cowboys" in report

    def test_date_present(self):
        report = format_game_report(_GAME)
        assert "2026-09-27" in report

    def test_injury_report_section_present(self):
        report = format_game_report(_GAME)
        assert "## Injury Report" in report

    def test_injury_report_near_top(self):
        report = format_game_report(_GAME)
        header_pos = report.index("# Baltimore Ravens")
        injury_pos = report.index("## Injury Report")
        # Injury report must appear within the first 600 characters after the header
        assert injury_pos - header_pos < 600, (
            "Injury report section is too far from the top of the report"
        )

    def test_injury_report_both_teams_present(self):
        report = format_game_report(_GAME)
        assert "### Baltimore Ravens (BAL)" in report
        assert "### Dallas Cowboys (DAL)" in report

    def test_injury_report_player_designation_present(self):
        report = format_game_report(_GAME)
        assert "Test Player" in report
        assert "Questionable" in report

    def test_injury_report_source_tagged_per_team(self):
        report = format_game_report(_GAME)
        assert report.count("**Source:** espn") == 2

    def test_injury_report_espn_practice_status_note_present(self):
        report = format_game_report(_GAME)
        assert report.count("Practice status is not available from this source") == 2

    def test_injury_report_espn_no_practice_status_per_player(self):
        report = format_game_report(_GAME)
        assert "Practice status:" not in report

    def test_injury_report_nflverse_practice_status_per_player(self):
        game = _sample_game(
            away_injuries=_ok_injury_result("nflverse"),
            home_injuries=_ok_injury_result("nflverse"),
        )
        report = format_game_report(game)
        assert "Practice status: Limited Participation in Practice" in report

    def test_injury_report_no_designations_shows_no_injuries_line(self):
        game = _sample_game(
            away_injuries=InjuryResult(records=[], source="espn", status="no_designations"),
        )
        report = format_game_report(game)
        assert "No injuries reported." in report

    def test_injury_report_unavailable_shows_marker(self):
        game = _sample_game(
            away_injuries=InjuryResult(records=[], source="unavailable", status="unavailable"),
        )
        report = format_game_report(game)
        assert "UNAVAILABLE" in report

    def test_injury_report_espn_as_of_uses_payload_timestamp(self):
        result = InjuryResult(
            records=[], source="espn", status="no_designations",
            as_of="2026-09-28T14:32:00Z",
        )
        game = _sample_game(away_injuries=result)
        report = format_game_report(game)
        assert "As of: 2026-09-28T14:32:00Z" in report

    def test_injury_report_failed_count_renders_and_is_not_silent(self):
        rec = InjuryRecord(
            player_name="Test Player", position="WR", designation="Questionable",
            practice_status=None, injury_type=None, source="espn", updated_at=None,
        )
        game = _sample_game(
            away_injuries=InjuryResult(records=[rec], source="espn", status="ok", failed_count=3),
        )
        report = format_game_report(game)
        assert "3 injury record(s) for this team could not be parsed." in report

    def test_injury_report_no_failed_count_line_when_zero(self):
        report = format_game_report(_GAME)
        assert "could not be parsed" not in report

    def test_injury_report_failed_team_differs_from_complete_team(self):
        """A team with parse failures must never render identically to a complete one."""
        rec = InjuryRecord(
            player_name="Test Player", position="WR", designation="Questionable",
            practice_status=None, injury_type=None, source="espn", updated_at=None,
        )
        complete = InjuryResult(records=[rec], source="espn", status="ok", failed_count=0)
        with_failures = InjuryResult(records=[rec], source="espn", status="ok", failed_count=1)
        game_complete = _sample_game(away_injuries=complete)
        game_failed = _sample_game(away_injuries=with_failures)
        assert format_game_report(game_complete) != format_game_report(game_failed)

    def test_injury_report_possibly_incomplete_renders_note(self):
        rec = InjuryRecord(
            player_name="Test Player", position="WR", designation="Active",
            practice_status=None, injury_type=None, source="espn", updated_at=None,
        )
        result = InjuryResult(
            records=[rec] * 25, source="espn", status="ok",
            possibly_incomplete=True,
        )
        game = _sample_game(away_injuries=result)
        report = format_game_report(game)
        assert "may be incomplete" in report
        assert "25 records" in report

    def test_injury_report_not_possibly_incomplete_no_note(self):
        rec = InjuryRecord(
            player_name="Test Player", position="WR", designation="Active",
            practice_status=None, injury_type=None, source="espn", updated_at=None,
        )
        result = InjuryResult(
            records=[rec], source="espn", status="ok",
            possibly_incomplete=False,
        )
        game = _sample_game(away_injuries=result)
        report = format_game_report(game)
        assert "may be incomplete" not in report

    def test_injury_report_possibly_incomplete_distinct_from_unavailable_and_no_designations(self):
        unavailable = InjuryResult(records=[], source="unavailable", status="unavailable")
        no_designations = InjuryResult(records=[], source="espn", status="no_designations")
        incomplete = InjuryResult(
            records=[InjuryRecord("A", "WR", "Active", None, None, "espn", None)] * 25,
            source="espn", status="ok", possibly_incomplete=True,
        )
        report_unavailable = format_game_report(_sample_game(away_injuries=unavailable))
        report_no_designations = format_game_report(_sample_game(away_injuries=no_designations))
        report_incomplete = format_game_report(_sample_game(away_injuries=incomplete))
        assert "may be incomplete" not in report_unavailable
        assert "may be incomplete" not in report_no_designations
        assert "may be incomplete" in report_incomplete

    def test_injury_report_nflverse_states_season_and_week(self):
        result = InjuryResult(
            records=[], source="nflverse", status="no_designations",
            nflverse_season=2026, nflverse_week=3,
        )
        game = _sample_game(away_injuries=result)
        report = format_game_report(game)
        assert "nflverse, 2026 week 3" in report

    def test_injury_report_nflverse_no_generation_time_as_of_line(self):
        result = InjuryResult(
            records=[], source="nflverse", status="no_designations",
            nflverse_season=2026, nflverse_week=3,
        )
        game = _sample_game(away_injuries=result)
        report = format_game_report(game)
        # nflverse sections must not carry a misleading "As of: <generation time>" line.
        assert "As of:" not in report

    def test_injury_report_nflverse_stale_shows_warning(self):
        result = InjuryResult(
            records=[], source="nflverse", status="no_designations",
            nflverse_season=2026, nflverse_week=3, stale=True,
        )
        game = _sample_game(away_injuries=result)
        report = format_game_report(game)
        assert "WARNING" in report
        assert "NOT current-week data" in report

    def test_injury_report_nflverse_not_stale_no_warning(self):
        result = InjuryResult(
            records=[], source="nflverse", status="no_designations",
            nflverse_season=2026, nflverse_week=3, stale=False,
        )
        game = _sample_game(away_injuries=result)
        report = format_game_report(game)
        assert "WARNING" not in report

    # -- Game-relative staleness note (schedule-driven, source-independent) --

    def test_game_relative_note_fires_for_future_game(self):
        """Game is week 7; current real week is 3 -- ESPN data (always
        current-week) may be stale relative to this specific game."""
        ctx = WeekContext(season=2026, current_week=3, game_week=7, found=True)
        game = _sample_game(week_context=ctx)  # default injuries are ESPN, not stale
        report = format_game_report(game)
        assert "NOTE" in report
        assert "4 week(s) out" in report
        assert "season 2026 week 7" in report
        assert "current week 3" in report

    def test_game_relative_note_absent_for_current_week_game(self):
        ctx = WeekContext(season=2026, current_week=3, game_week=3, found=True)
        game = _sample_game(week_context=ctx)
        report = format_game_report(game)
        assert "NOTE" not in report
        assert "week(s) out" not in report

    def test_game_relative_note_absent_when_week_context_missing(self):
        """No silent defaults: when the schedule lookup couldn't produce an
        answer (week_context is None), no claim is rendered either way."""
        game = _sample_game(week_context=None)
        report = format_game_report(game)
        assert "NOTE" not in report
        assert "week(s) out" not in report

    def test_game_relative_note_does_not_replace_nflverse_warning(self):
        """Future game (ESPN designations) + nflverse fallback stale data on
        the OTHER team: both warnings must render, distinctly -- neither
        replaces or merges with the other."""
        ctx = WeekContext(season=2026, current_week=3, game_week=7, found=True)
        nflverse_stale = InjuryResult(
            records=[], source="nflverse", status="no_designations",
            nflverse_season=2026, nflverse_week=2, stale=True,
        )
        game = _sample_game(
            week_context=ctx,
            away_injuries=_ok_injury_result("espn"),
            home_injuries=nflverse_stale,
        )
        report = format_game_report(game)
        # The game-relative note (game-level, source-independent).
        assert "4 week(s) out" in report
        assert "ESPN designations reflect the current week" in report
        # The existing nflverse-specific warning (team-level, source-specific).
        assert "NOT current-week data" in report
        # They are two distinct lines, not merged into one message.
        note_line = next(l for l in report.splitlines() if "week(s) out" in l)
        warning_line = next(l for l in report.splitlines() if "NOT current-week data" in l)
        assert note_line != warning_line
        assert "NOT current-week data" not in note_line
        assert "week(s) out" not in warning_line

    def test_game_relative_note_and_current_week_espn_means_no_warnings_at_all(self):
        """Current-week game + ESPN (the common case) -- no warning of
        either kind."""
        ctx = WeekContext(season=2026, current_week=3, game_week=3, found=True)
        game = _sample_game(week_context=ctx)  # default injuries are ESPN
        report = format_game_report(game)
        assert "NOTE" not in report
        assert "WARNING" not in report

    def test_injury_report_shortcomment_skipped_when_bare_repeat(self):
        rec = InjuryRecord(
            player_name="Bare Repeat",
            position="WR",
            designation="Questionable",
            practice_status="unavailable (ESPN source -- no structured practice status)",
            injury_type="Ankle",
            source="espn",
            updated_at=None,
            short_comment="questionable",
        )
        game = _sample_game(away_injuries=InjuryResult(records=[rec], source="espn", status="ok"))
        report = format_game_report(game)
        # The bare "questionable" shortComment must not be rendered as a sub-bullet.
        assert "  - questionable" not in report

    def test_injury_report_shortcomment_included_when_informative(self):
        rec = InjuryRecord(
            player_name="Informative Case",
            position="WR",
            designation="Questionable",
            practice_status="unavailable (ESPN source -- no structured practice status)",
            injury_type="Hamstring",
            source="espn",
            updated_at=None,
            short_comment="did not participate in Wednesday's practice",
        )
        game = _sample_game(away_injuries=InjuryResult(records=[rec], source="espn", status="ok"))
        report = format_game_report(game)
        assert "did not participate in Wednesday's practice" in report

    # ------------------------------------------------------------------
    # Team performance (rolling stats) section
    # ------------------------------------------------------------------

    def test_team_performance_section_present(self):
        report = format_game_report(_GAME)
        assert "## Team Performance" in report

    def test_team_performance_both_teams_present(self):
        report = format_game_report(_GAME)
        assert "### Baltimore Ravens (BAL)" in report
        assert "### Dallas Cowboys (DAL)" in report

    def test_team_performance_shows_last4_and_season_to_date(self):
        report = format_game_report(_GAME)
        assert "Last 4 Games" in report
        assert "Season-to-Date" in report

    def test_team_performance_epa_values_rendered(self):
        report = format_game_report(_GAME)
        assert "+0.093" in report
        assert "-0.304" in report

    def test_team_performance_points_and_record_rendered(self):
        report = format_game_report(_GAME)
        assert "Points for (avg): 27.0" in report
        assert "Points against (avg): 7.0" in report
        assert "Record: 3-0" in report

    def test_team_performance_insufficient_data_shows_marker_not_zero(self):
        insufficient = TeamStatsResult(
            team_abbr="BAL", status="ok", season=2026,
            last4=WindowStats(
                status="insufficient_data", games_used=0,
                epa_offense=None, epa_defense=None,
                points_for_avg=None, points_against_avg=None,
                wins=0, losses=0, ties=0,
            ),
            season_to_date=WindowStats(
                status="insufficient_data", games_used=0,
                epa_offense=None, epa_defense=None,
                points_for_avg=None, points_against_avg=None,
                wins=0, losses=0, ties=0,
            ),
        )
        game = _sample_game(away_team_stats=insufficient, home_team_stats=insufficient)
        report = format_game_report(game)
        assert "INSUFFICIENT DATA" in report
        team_perf = report.split("## Team Performance")[1].split("## NOT YET AVAILABLE")[0]
        assert "0.0" not in team_perf
        assert "EPA/play" not in team_perf

    def test_team_performance_unavailable_shows_marker(self):
        unavailable = TeamStatsResult(
            team_abbr="BAL", status="unavailable", season=None,
            last4=None, season_to_date=None, error="nflreadpy down",
        )
        game = _sample_game(away_team_stats=unavailable)
        report = format_game_report(game)
        assert "UNAVAILABLE" in report
        assert "nflreadpy down" in report

    def test_team_performance_game_count_stated_explicitly(self):
        game = _sample_game(
            away_team_stats=_ok_team_stats("BAL"),  # games_used=3 in both windows
        )
        report = format_game_report(game)
        assert "n=3 completed game(s)" in report

    def test_team_performance_not_wired_defaults_to_unavailable(self):
        game = _sample_game()
        del game["away_team_stats"]
        report = format_game_report(game)
        assert "UNAVAILABLE" in report

    def test_moneyline_section_present(self):
        report = format_game_report(_GAME)
        assert "## Moneyline" in report

    def test_moneyline_prices_displayed(self):
        report = format_game_report(_GAME)
        assert "0.480" in report
        assert "0.520" in report

    def test_moneyline_implied_probability(self):
        report = format_game_report(_GAME)
        assert "48.0%" in report
        assert "52.0%" in report

    def test_moneyline_open_interest(self):
        report = format_game_report(_GAME)
        assert "12,345.67" in report

    def test_moneyline_open_interest_unavailable_shows_marker(self):
        game = _sample_game()
        game["moneyline"]["open_interest_status"] = "unavailable"
        game["moneyline"]["open_interest_reason"] = "Unexpected /oi response shape for conditionId='0xabc': []"
        game["moneyline"]["open_interest"] = None
        report = format_game_report(game)
        assert "**Open Interest:** UNAVAILABLE -- Unexpected /oi response shape" in report
        assert "$0.00" not in report

    def test_moneyline_open_interest_missing_key_shows_marker_not_zero(self):
        # No open_interest key at all (e.g. an older caller) must never render $0.00.
        game = _sample_game()
        del game["moneyline"]["open_interest"]
        report = format_game_report(game)
        assert "**Open Interest:** UNAVAILABLE" in report
        assert "$0.00" not in report

    def test_moneyline_recent_trades(self):
        report = format_game_report(_GAME)
        assert "BUY" in report
        assert "SELL" in report

    def test_spreads_section_present(self):
        report = format_game_report(_GAME)
        assert "## Spreads" in report

    def test_totals_section_present(self):
        report = format_game_report(_GAME)
        assert "## Totals" in report

    def test_phase4_section_present(self):
        report = format_game_report(_GAME)
        assert "NOT YET AVAILABLE" in report
        assert "Phase 4" in report

    def test_phase4_lists_injury_designations(self):
        report = format_game_report(_GAME)
        assert "Injury designations" in report or "injury" in report.lower()

    def test_phase4_lists_team_stats(self):
        # Team stats moved out of the Phase 4 placeholder into a real
        # rendered section (Phase 4 Step 6b) -- this now checks for that
        # section directly, mirroring how the injury-designations test
        # above checks the real Injury Report section rather than a
        # placeholder bullet.
        report = format_game_report(_GAME)
        assert "## Team Performance" in report

    def test_phase4_lists_weather(self):
        report = format_game_report(_GAME)
        assert "Weather" in report or "weather" in report.lower()

    def test_footer_contains_source_url(self):
        report = format_game_report(_GAME)
        assert _GAME["event_url"] in report

    def test_footer_contains_generated_timestamp(self):
        report = format_game_report(_GAME)
        assert "Generated:" in report

    def test_no_moneyline_handled_gracefully(self):
        game = _sample_game()
        game["moneyline"] = None
        report = format_game_report(game)
        assert "No moneyline market available" in report

    def test_empty_spreads_handled_gracefully(self):
        game = _sample_game()
        game["spreads"] = []
        report = format_game_report(game)
        assert "No spread markets available" in report

    def test_empty_totals_handled_gracefully(self):
        game = _sample_game()
        game["totals"] = []
        report = format_game_report(game)
        assert "No totals markets available" in report

    def test_no_recent_trades_handled_gracefully(self):
        game = _sample_game()
        game["moneyline"]["recent_trades"] = []
        report = format_game_report(game)
        assert "none recorded yet" in report

    def test_trades_capped_at_five(self):
        game = _sample_game()
        game["moneyline"]["recent_trades"] = [
            {"side": "BUY", "size": str(i), "price": "0.5"} for i in range(10)
        ]
        report = format_game_report(game)
        # Each trade line starts with "  - BUY"; count them
        trade_lines = [l for l in report.splitlines() if l.strip().startswith("- BUY")]
        assert len(trade_lines) <= 5


# ---------------------------------------------------------------------------
# Game status note -- completed/in-progress games must be labeled
# prominently, never silently rendered as an ambiguous upcoming-game
# report. See reporter.watcher.game_assembly.get_game_status().
# ---------------------------------------------------------------------------

class TestGameStatusNote:
    def test_upcoming_game_unaffected_no_note(self):
        game = _sample_game(game_status="scheduled", final_score=None)
        report = format_game_report(game)
        assert "GAME ALREADY COMPLETED" not in report
        assert "GAME IN PROGRESS" not in report

    def test_missing_game_status_key_unaffected(self):
        # Callers that haven't wired this stage in (game_status absent
        # entirely, not just None) must render exactly as before.
        game = _sample_game()
        del game["game_status"]
        del game["final_score"]
        report = format_game_report(game)
        assert "GAME ALREADY COMPLETED" not in report
        assert "GAME IN PROGRESS" not in report

    def test_completed_game_labeled_with_score(self):
        game = _sample_game(
            game_status="completed",
            final_score={"away_abbr": "BAL", "away_score": 17, "home_abbr": "DAL", "home_score": 24},
        )
        report = format_game_report(game)
        assert "GAME ALREADY COMPLETED" in report
        assert "BAL 17 - DAL 24" in report

    def test_completed_game_missing_score_still_labeled(self):
        game = _sample_game(game_status="completed", final_score=None)
        report = format_game_report(game)
        assert "GAME ALREADY COMPLETED" in report
        assert "unavailable" in report

    def test_in_progress_game_labeled(self):
        game = _sample_game(game_status="in_progress", final_score=None)
        report = format_game_report(game)
        assert "GAME IN PROGRESS" in report
        assert "GAME ALREADY COMPLETED" not in report

    def test_completed_note_is_near_top_not_buried(self):
        game = _sample_game(
            game_status="completed",
            final_score={"away_abbr": "BAL", "away_score": 17, "home_abbr": "DAL", "home_score": 24},
        )
        report = format_game_report(game)
        header_pos = report.index("# Baltimore Ravens")
        note_pos = report.index("GAME ALREADY COMPLETED")
        injury_pos = report.index("## Injury Report")
        # The status note must come before the injury report section, and
        # close to the header -- "prominent," not buried after other content.
        assert header_pos < note_pos < injury_pos
        assert note_pos - header_pos < 300


# ---------------------------------------------------------------------------
# write_game_report — filename and path tests
# ---------------------------------------------------------------------------

class TestWriteGameReport:
    def test_filename_format(self, tmp_path):
        path = write_game_report(_GAME, tmp_path)
        assert path.name == "2026-09-27_BAL_at_DAL.md"

    def test_uses_abbreviations_not_full_names(self, tmp_path):
        path = write_game_report(_GAME, tmp_path)
        assert "Baltimore" not in path.name
        assert "Dallas" not in path.name
        assert "BAL" in path.name
        assert "DAL" in path.name

    def test_file_written_to_output_dir(self, tmp_path):
        path = write_game_report(_GAME, tmp_path)
        assert path.parent == tmp_path
        assert path.exists()

    def test_file_content_is_report(self, tmp_path):
        path = write_game_report(_GAME, tmp_path)
        content = path.read_text(encoding="utf-8")
        assert "## Injury Report" in content
        assert "## Moneyline" in content

    def test_creates_output_dir_if_missing(self, tmp_path):
        new_dir = tmp_path / "nested" / "output"
        write_game_report(_GAME, new_dir)
        assert new_dir.exists()

    def test_returns_path_object(self, tmp_path):
        result = write_game_report(_GAME, tmp_path)
        assert isinstance(result, Path)

    def test_overwrites_existing_file(self, tmp_path):
        write_game_report(_GAME, tmp_path)
        path = write_game_report(_GAME, tmp_path)
        assert path.exists()
        # Should not raise; just overwrites


# ---------------------------------------------------------------------------
# write_all_reports — stale file cleanup
# ---------------------------------------------------------------------------

class TestWriteAllReports:
    def _make_report_file(self, directory: Path, name: str) -> Path:
        """Create a dummy report file in directory."""
        p = directory / name
        p.write_text("old report", encoding="utf-8")
        return p

    def test_writes_one_file_per_game(self, tmp_path):
        games = [_sample_game(game_id=1, away_abbr="KC",  home_abbr="MIA", game_date="2026-09-28"),
                 _sample_game(game_id=2, away_abbr="PHI", home_abbr="NYG", game_date="2026-09-28")]
        with patch("reporter.report.report_writer.get_reportable_games", return_value=games), \
             patch("reporter.report.report_writer.settings") as mock_settings:
            mock_settings.report_output_dir = tmp_path
            written = write_all_reports()
        assert len(written) == 2
        assert all(p.exists() for p in written)

    def test_one_games_oi_failure_does_not_block_other_reports(self, tmp_path):
        # Reproduces the DEN @ SF production incident: one game's moneyline
        # has open_interest_status="unavailable" (empty /oi response) while
        # the rest of the slate is healthy. The whole cycle must still write
        # a report for every game.
        ok_game = _sample_game(game_id=1, away_abbr="KC", home_abbr="MIA", game_date="2026-09-28")
        degraded_game = _sample_game(game_id=2, away_abbr="DEN", home_abbr="SF", game_date="2026-09-28")
        degraded_game["moneyline"]["open_interest"] = None
        degraded_game["moneyline"]["open_interest_status"] = "unavailable"
        degraded_game["moneyline"]["open_interest_reason"] = (
            "Unexpected /oi response shape for conditionId='0xdensf': []"
        )
        games = [ok_game, degraded_game]

        with patch("reporter.report.report_writer.get_reportable_games", return_value=games), \
             patch("reporter.report.report_writer.settings") as mock_settings:
            mock_settings.report_output_dir = tmp_path
            written = write_all_reports()

        assert len(written) == 2
        assert all(p.exists() for p in written)

        den_sf_path = next(p for p in written if "DEN_at_SF" in p.name)
        kc_mia_path = next(p for p in written if "KC_at_MIA" in p.name)

        den_sf_content = den_sf_path.read_text(encoding="utf-8")
        kc_mia_content = kc_mia_path.read_text(encoding="utf-8")

        assert "**Open Interest:** UNAVAILABLE" in den_sf_content
        assert "$0.00" not in den_sf_content
        assert "**Open Interest:** $12,345.67" in kc_mia_content

    def test_returns_list_of_paths(self, tmp_path):
        games = [_GAME]
        with patch("reporter.report.report_writer.get_reportable_games", return_value=games), \
             patch("reporter.report.report_writer.settings") as mock_settings:
            mock_settings.report_output_dir = tmp_path
            result = write_all_reports()
        assert isinstance(result, list)
        assert all(isinstance(p, Path) for p in result)

    def test_stale_report_deleted(self, tmp_path):
        # Simulate a stale report from a prior run
        stale = self._make_report_file(tmp_path, "2026-09-21_NE_at_TEN.md")
        assert stale.exists()

        current_game = _sample_game(game_id=1, away_abbr="KC", home_abbr="MIA", game_date="2026-09-28")
        with patch("reporter.report.report_writer.get_reportable_games", return_value=[current_game]), \
             patch("reporter.report.report_writer.settings") as mock_settings:
            mock_settings.report_output_dir = tmp_path
            write_all_reports()

        assert not stale.exists(), "Stale report from prior run should have been deleted"

    def test_current_report_not_deleted(self, tmp_path):
        game = _sample_game(game_id=1, away_abbr="KC", home_abbr="MIA", game_date="2026-09-28")
        with patch("reporter.report.report_writer.get_reportable_games", return_value=[game]), \
             patch("reporter.report.report_writer.settings") as mock_settings:
            mock_settings.report_output_dir = tmp_path
            written = write_all_reports()

        assert all(p.exists() for p in written)

    def test_non_report_files_not_deleted(self, tmp_path):
        # A file that doesn't match the naming pattern should be left alone
        other = tmp_path / "README.md"
        other.write_text("readme", encoding="utf-8")

        with patch("reporter.report.report_writer.get_reportable_games", return_value=[]), \
             patch("reporter.report.report_writer.settings") as mock_settings:
            mock_settings.report_output_dir = tmp_path
            write_all_reports()

        assert other.exists(), "Non-report files must not be deleted"

    def test_empty_game_list_clears_all_stale_reports(self, tmp_path):
        stale1 = self._make_report_file(tmp_path, "2026-09-21_NE_at_TEN.md")
        stale2 = self._make_report_file(tmp_path, "2026-09-21_CIN_at_PIT.md")

        with patch("reporter.report.report_writer.get_reportable_games", return_value=[]), \
             patch("reporter.report.report_writer.settings") as mock_settings:
            mock_settings.report_output_dir = tmp_path
            written = write_all_reports()

        assert written == []
        assert not stale1.exists()
        assert not stale2.exists()

    # ------------------------------------------------------------------
    # Edge cases: Step 10 lifecycle hardening
    # ------------------------------------------------------------------

    def test_creates_output_dir_on_first_run(self, tmp_path):
        # write_all_reports() must not crash when REPORT_OUTPUT_DIR doesn't exist yet.
        new_dir = tmp_path / "brand" / "new" / "dir"
        assert not new_dir.exists()
        with patch("reporter.report.report_writer.get_reportable_games", return_value=[_sample_game()]), \
             patch("reporter.report.report_writer.settings") as mock_settings:
            mock_settings.report_output_dir = new_dir
            write_all_reports()
        assert new_dir.exists()

    def test_zero_games_returns_empty_list_no_crash(self, tmp_path):
        # write_all_reports() must return [] cleanly when no games are reportable.
        with patch("reporter.report.report_writer.get_reportable_games", return_value=[]), \
             patch("reporter.report.report_writer.settings") as mock_settings:
            mock_settings.report_output_dir = tmp_path
            result = write_all_reports()
        assert result == []

    def test_no_temp_files_remain_after_write(self, tmp_path):
        # Atomic write must not leave .tmp files behind on success.
        write_game_report(_GAME, tmp_path)
        assert list(tmp_path.glob("*.tmp")) == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not enforced on Windows")
    def test_written_file_permissions_are_0644(self, tmp_path):
        import stat
        path = write_game_report(_GAME, tmp_path)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o644, f"Expected 0o644, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Line column, placeholder filtering, ASCII-only content
# ---------------------------------------------------------------------------

class TestLineLabelAndFiltering:
    def test_spreads_table_has_line_column(self):
        report = format_game_report(_GAME)
        assert "| Line |" in report

    def test_totals_table_has_line_column(self):
        report = format_game_report(_GAME)
        # Line column appears in both spread and total tables
        assert report.count("| Line |") >= 2

    def test_spread_line_value_in_table(self):
        report = format_game_report(_GAME)
        assert "-3.5" in report

    def test_total_line_value_in_table(self):
        report = format_game_report(_GAME)
        assert "44.5" in report

    def test_placeholder_spreads_excluded(self):
        game = _sample_game()
        # Two spread entries: one real, one placeholder (0.500/0.500)
        game["spreads"] = [
            {"prices": {"Away": 0.48, "Home": 0.52}, "line": "-3.5"},
            {"prices": {"Away": 0.500, "Home": 0.500}, "line": "-1.5"},  # placeholder
        ]
        report = format_game_report(game)
        # The real line appears; the placeholder is excluded
        assert "-3.5" in report
        assert "-1.5" not in report

    def test_all_placeholder_spreads_kept_when_nothing_else(self):
        # If every spread is a placeholder, show them all (no empty table)
        game = _sample_game()
        game["spreads"] = [
            {"prices": {"Away": 0.500, "Home": 0.500}, "line": "-1.5"},
        ]
        report = format_game_report(game)
        # Must not produce an empty table
        assert "No spread markets available" not in report
        assert "## Spreads" in report
        assert "-1.5" in report

    def test_placeholder_totals_excluded(self):
        game = _sample_game()
        game["totals"] = [
            {"prices": {"Over": 0.52, "Under": 0.48}, "line": "44.5"},
            {"prices": {"Over": 0.500, "Under": 0.500}, "line": "47.5"},  # placeholder
        ]
        report = format_game_report(game)
        assert "44.5" in report
        assert "47.5" not in report

    def test_ascii_only_in_generated_report(self):
        report = format_game_report(_GAME)
        non_ascii = [(i, ch) for i, ch in enumerate(report) if ord(ch) > 127]
        assert non_ascii == [], (
            f"Non-ASCII characters found: "
            + ", ".join(f"pos={i} char={ch!r} (U+{ord(ch):04X})" for i, ch in non_ascii[:5])
        )

    def test_no_em_dash_in_generated_report(self):
        report = format_game_report(_GAME)
        assert "—" not in report, "Em dash (--) found; use ASCII '--' instead"

    def test_no_warning_emoji_in_generated_report(self):
        report = format_game_report(_GAME)
        assert "⚠" not in report, "Warning symbol found; use ASCII 'WARNING:' instead"

    def test_injury_report_unavailable_uses_ascii_marker(self):
        game = _sample_game(
            away_injuries=InjuryResult(records=[], source="unavailable", status="unavailable"),
        )
        report = format_game_report(game)
        assert "**UNAVAILABLE**" in report

    def test_phase4_header_uses_ascii_dash(self):
        report = format_game_report(_GAME)
        assert "NOT YET AVAILABLE -- Phase 4" in report
