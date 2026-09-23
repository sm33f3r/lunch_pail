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

from reporter.report.report_writer import (  # noqa: E402
    format_game_report,
    write_all_reports,
    write_game_report,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

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
    return {
        "game_id":   game_id,
        "away_team": away_team,
        "home_team": home_team,
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "game_date": game_date,
        "event_slug": f"nfl-{away_abbr.lower()}-{home_abbr.lower()}-{game_date}",
        "event_url":  f"https://polymarket.com/event/nfl-{away_abbr.lower()}-{home_abbr.lower()}-{game_date}",
        "volume":     volume,
        "moneyline":  moneyline,
        "spreads":    spreads,
        "totals":     totals,
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

    def test_injury_disclaimer_present(self):
        report = format_game_report(_GAME)
        assert "INJURY GATE NOT CLEARED" in report

    def test_injury_disclaimer_near_top(self):
        report = format_game_report(_GAME)
        header_pos = report.index("# Baltimore Ravens")
        disclaimer_pos = report.index("INJURY GATE NOT CLEARED")
        # Disclaimer must appear within the first 600 characters after the header
        assert disclaimer_pos - header_pos < 600, (
            "Injury disclaimer is too far from the top of the report"
        )

    def test_injury_disclaimer_non_empty(self):
        report = format_game_report(_GAME)
        # Extract the disclaimer block (between first "> ### ⚠" and next non-"> " line)
        disclaimer_lines = [
            line for line in report.splitlines()
            if line.startswith(">")
        ]
        assert len(disclaimer_lines) >= 3, "Disclaimer block should have at least 3 lines"

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
        report = format_game_report(_GAME)
        assert "Team stats" in report or "team stats" in report.lower()

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
        assert "INJURY GATE NOT CLEARED" in content
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

    def test_injury_disclaimer_uses_ascii_warning(self):
        report = format_game_report(_GAME)
        assert "WARNING: INJURY GATE NOT CLEARED" in report

    def test_phase4_header_uses_ascii_dash(self):
        report = format_game_report(_GAME)
        assert "NOT YET AVAILABLE -- Phase 4" in report
