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

from reporter.orchestrator import run_forever, run_once  # noqa: E402


class TestRunOnce:
    def test_calls_write_all_reports(self):
        with patch("reporter.orchestrator.write_all_reports", return_value=[]) as mock_write:
            run_once()
        mock_write.assert_called_once()

    def test_returns_none(self):
        with patch("reporter.orchestrator.write_all_reports", return_value=[]):
            result = run_once()
        assert result is None

    def test_exception_does_not_propagate(self):
        # A transient error in write_all_reports must not crash run_once.
        with patch("reporter.orchestrator.write_all_reports", side_effect=RuntimeError("API timeout")):
            run_once()  # must not raise

    def test_exception_from_connection_error_does_not_propagate(self):
        import requests
        with patch("reporter.orchestrator.write_all_reports", side_effect=requests.ConnectionError("network down")):
            run_once()  # must not raise

    def test_prints_summary_with_games(self, capsys):
        from pathlib import Path
        paths = [Path("/tmp/2026-09-28_KC_at_MIA.md"), Path("/tmp/2026-09-28_PHI_at_NYG.md")]
        with patch("reporter.orchestrator.write_all_reports", return_value=paths):
            run_once()
        out = capsys.readouterr().out
        assert "2" in out
        assert "KC_at_MIA" in out

    def test_prints_zero_games_message(self, capsys):
        with patch("reporter.orchestrator.write_all_reports", return_value=[]):
            run_once()
        out = capsys.readouterr().out
        assert "0" in out


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
