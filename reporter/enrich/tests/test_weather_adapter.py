"""
Unit tests for reporter.enrich.weather_adapter.

All tests run offline -- no live Open-Meteo calls. Fixture data loaded
from reporter/enrich/fixtures/ (committed JSON, captured live during the
Phase 4 weather recon -- see reporter/enrich/WEATHER_RECON.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reporter.enrich.weather_adapter import (
    ForecastHorizonError,
    StadiumInfo,
    WeatherFetchError,
    WeatherResult,
    _days_out,
    _load_stadiums,
    _nearest_hour_index,
    _reset_forecast_cache,
    _reset_stadium_cache,
    fetch_forecast_day,
    get_game_weather,
    get_stadium_info,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _clear_caches():
    """Every test starts and ends with cold stadium/forecast caches."""
    _reset_stadium_cache()
    _reset_forecast_cache()
    yield
    _reset_stadium_cache()
    _reset_forecast_cache()


def _resp(status: int, body: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


# ---------------------------------------------------------------------------
# Stadium seed lookup
# ---------------------------------------------------------------------------

class TestStadiumLookup:
    def test_all_32_franchises_covered(self):
        stadiums = _load_stadiums()
        assert len(stadiums) == 32

    def test_outdoor_team_flagged_true(self):
        gb = get_stadium_info("GB")
        assert gb is not None
        assert gb.is_outdoor is True

    def test_dome_team_flagged_false(self):
        det = get_stadium_info("DET")
        assert det is not None
        assert det.is_outdoor is False

    def test_retractable_team_flagged_false(self):
        ari = get_stadium_info("ARI")
        assert ari is not None
        assert ari.is_outdoor is False

    def test_unknown_team_returns_none(self):
        assert get_stadium_info("ZZZ") is None

    def test_lowercase_abbr_normalised(self):
        assert get_stadium_info("gb") is not None

    def test_former_venue_rows_excluded(self):
        # ATL has two rows (Georgia Dome, former; Mercedes-Benz, current).
        # Only one (the current one) should be loaded.
        stadiums = _load_stadiums()
        assert stadiums["ATL"].stadium_name == "Mercedes-Benz Stadium"

    def test_missing_csv_raises_weather_fetch_error(self, tmp_path):
        missing = tmp_path / "nope.csv"
        with pytest.raises(WeatherFetchError):
            _load_stadiums(missing)


# ---------------------------------------------------------------------------
# fetch_forecast_day -- using committed fixtures as the mocked response body
# ---------------------------------------------------------------------------

class TestFetchForecastDay:
    def test_near_term_returns_hourly_dict(self):
        fixture = _load_fixture("open_meteo_forecast_near_term.json")
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(200, fixture)
            hourly = fetch_forecast_day(39.9007, -75.1675, "2026-10-04", "America/New_York")
        assert "time" in hourly
        assert len(hourly["time"]) == 24

    def test_far_out_raises_forecast_horizon_error(self):
        fixture = _load_fixture("open_meteo_forecast_far_out_error.json")
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(400, fixture)
            with pytest.raises(ForecastHorizonError):
                fetch_forecast_day(39.9007, -75.1675, "2026-10-27", "America/New_York")

    def test_boundary_date_returns_real_data(self):
        fixture = _load_fixture("open_meteo_forecast_horizon_boundary.json")
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(200, fixture)
            hourly = fetch_forecast_day(39.9007, -75.1675, "2026-10-16", "America/New_York")
        assert len(hourly["time"]) == 24

    def test_network_failure_raises_weather_fetch_error(self):
        import requests
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("boom")
            with pytest.raises(WeatherFetchError):
                fetch_forecast_day(39.9007, -75.1675, "2026-10-04", "America/New_York")

    def test_unexpected_shape_raises_weather_fetch_error(self):
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(200, {"no_hourly_key": True})
            with pytest.raises(WeatherFetchError):
                fetch_forecast_day(39.9007, -75.1675, "2026-10-04", "America/New_York")

    def test_other_400_without_horizon_wording_is_weather_fetch_error(self):
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(400, {"error": True, "reason": "latitude must be in range of -90 to 90"})
            with pytest.raises(WeatherFetchError):
                fetch_forecast_day(999.0, -75.1675, "2026-10-04", "America/New_York")

    def test_repeated_call_within_ttl_uses_cache(self):
        fixture = _load_fixture("open_meteo_forecast_near_term.json")
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(200, fixture)
            fetch_forecast_day(39.9007, -75.1675, "2026-10-04", "America/New_York")
            fetch_forecast_day(39.9007, -75.1675, "2026-10-04", "America/New_York")
        assert mock_get.call_count == 1

    def test_different_date_bypasses_cache(self):
        fixture = _load_fixture("open_meteo_forecast_near_term.json")
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(200, fixture)
            fetch_forecast_day(39.9007, -75.1675, "2026-10-04", "America/New_York")
            fetch_forecast_day(39.9007, -75.1675, "2026-10-05", "America/New_York")
        assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# _nearest_hour_index / _days_out -- pure helpers
# ---------------------------------------------------------------------------

class TestNearestHourIndex:
    def test_exact_kickoff_match(self):
        times = [f"2026-10-04T{h:02d}:00" for h in range(24)]
        idx = _nearest_hour_index(times, "2026-10-04T17:00:00Z", "UTC")
        assert times[idx] == "2026-10-04T17:00"

    def test_missing_kickoff_defaults_to_noon(self):
        times = [f"2026-10-04T{h:02d}:00" for h in range(24)]
        idx = _nearest_hour_index(times, None, "UTC")
        assert idx == 12

    def test_unparseable_kickoff_degrades_to_noon(self):
        times = [f"2026-10-04T{h:02d}:00" for h in range(24)]
        idx = _nearest_hour_index(times, "not-a-timestamp", "UTC")
        assert idx == 12


class TestDaysOut:
    def test_future_date(self):
        from datetime import date
        assert _days_out("2026-10-10", today=date(2026, 10, 2)) == 8

    def test_unparseable_date_returns_none(self):
        from datetime import date
        assert _days_out("not-a-date", today=date(2026, 10, 2)) is None


# ---------------------------------------------------------------------------
# get_game_weather -- composite entry point
# ---------------------------------------------------------------------------

class TestGetGameWeather:
    def test_indoor_stadium_never_fetches(self):
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            result = get_game_weather("DET", "2026-10-04")
        mock_get.assert_not_called()
        assert result.status == "indoor"
        assert result.stadium_name == "Ford Field"

    def test_unknown_team_is_unavailable_no_fetch(self):
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            result = get_game_weather("ZZZ", "2026-10-04")
        mock_get.assert_not_called()
        assert result.status == "unavailable"
        assert "ZZZ" in result.error

    def test_outdoor_within_horizon_renders_real_values(self):
        fixture = _load_fixture("open_meteo_forecast_near_term.json")
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(200, fixture)
            result = get_game_weather("PHI", "2026-10-04", kickoff_utc="2026-10-04T17:00:00Z")
        assert result.status == "ok"
        assert result.temperature_f is not None
        assert result.wind_speed_mph is not None
        assert result.precipitation_in is not None

    def test_outdoor_beyond_horizon_is_forecast_not_yet_available(self):
        fixture = _load_fixture("open_meteo_forecast_far_out_error.json")
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(400, fixture)
            result = get_game_weather("PHI", "2026-10-27", kickoff_utc="2026-10-27T17:00:00Z")
        assert result.status == "forecast_not_yet_available"
        assert result.temperature_f is None
        assert result.wind_speed_mph is None

    def test_beyond_horizon_days_until_available_computed(self):
        fixture = _load_fixture("open_meteo_forecast_far_out_error.json")
        from datetime import date
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get, \
             patch("reporter.enrich.weather_adapter.datetime") as mock_dt:
            mock_get.return_value = _resp(400, fixture)
            mock_dt.now.return_value.date.return_value = date(2026, 10, 2)
            mock_dt.strptime = __import__("datetime").datetime.strptime
            result = get_game_weather("PHI", "2026-10-27", kickoff_utc="2026-10-27T17:00:00Z")
        assert result.days_out == 25
        assert result.days_until_available == 11

    def test_network_failure_is_unavailable_with_reason(self):
        import requests
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("connection refused")
            result = get_game_weather("PHI", "2026-10-04", kickoff_utc="2026-10-04T17:00:00Z")
        assert result.status == "unavailable"
        assert result.error is not None
        assert result.stadium_name == "Lincoln Financial Field"

    def test_forecast_not_yet_available_distinct_from_unavailable(self):
        """
        The two 'no numbers to show' states must never be confusable:
        one is a known limitation, the other is an actual failure.
        """
        fixture = _load_fixture("open_meteo_forecast_far_out_error.json")
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get:
            mock_get.return_value = _resp(400, fixture)
            horizon_result = get_game_weather("PHI", "2026-10-27")

        import requests
        with patch("reporter.enrich.weather_adapter.requests.get") as mock_get2:
            mock_get2.side_effect = requests.RequestException("boom")
            failure_result = get_game_weather("PHI", "2026-10-04")

        assert horizon_result.status != failure_result.status
        assert horizon_result.status == "forecast_not_yet_available"
        assert failure_result.status == "unavailable"

    def test_stadium_lookup_uses_home_team_only(self):
        # get_game_weather's team_abbr arg is documented as the HOME team;
        # confirm the stadium resolved is that team's own venue (an indoor
        # team is used here so no network call is attempted at all).
        result = get_game_weather("DET", "2026-10-04")
        assert result.stadium_name == "Ford Field"
