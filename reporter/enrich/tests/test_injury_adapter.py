"""
Unit tests for reporter.enrich.injury_adapter.

All tests run offline — no live network calls.
Fixture data loaded from reporter/enrich/fixtures/ (committed JSON).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reporter.enrich.injury_adapter import (
    PRACTICE_STATUS_UNAVAILABLE,
    ESPNError,
    InjuryRecord,
    InjuryResult,
    NflverseError,
    _ABBR_TO_ESPN_ID,
    _UNAVAILABLE,
    _parse_espn_record,
    _parse_league_record,
    _reset_league_cache,
    fetch_espn_injuries,
    fetch_espn_league_injuries,
    fetch_espn_league_payload,
    fetch_nflverse_injuries,
    fetch_nflverse_injuries_with_meta,
    get_team_injuries,
)
from reporter.enrich.team_stats_adapter import NflreadpyError

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------

def _load_espn_fixture(abbr: str) -> dict:
    return json.loads((_FIXTURES / f"espn_injuries_{abbr}.json").read_text())


def _load_league_fixture() -> dict:
    return json.loads((_FIXTURES / "espn_injuries_league.json").read_text())


def _load_nflverse_fixture() -> list[dict]:
    return json.loads((_FIXTURES / "nflverse_injuries_sample.json").read_text())


@pytest.fixture(autouse=True)
def _clear_league_cache():
    """Every test starts and ends with a cold league-payload cache."""
    _reset_league_cache()
    yield
    _reset_league_cache()


def _nflverse_csv_text(rows: list[dict]) -> str:
    """Convert a list of dicts to a CSV string matching the nflverse schema."""
    import csv, io
    cols = [
        "season", "season_type", "game_type", "team", "week", "gsis_id",
        "position", "full_name", "first_name", "last_name",
        "report_primary_injury", "report_secondary_injury", "report_status",
        "practice_primary_injury", "practice_secondary_injury", "practice_status",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _parse_espn_record — unit tests (pure, no mocking)
# ---------------------------------------------------------------------------

class TestParseEspnRecord:
    def _bal_record(self, idx: int = 0) -> dict:
        return _load_espn_fixture("BAL")["injuries"][idx]

    def test_maps_player_name(self):
        rec = _parse_espn_record(self._bal_record(0))
        assert rec.player_name == "Chris Moore"

    def test_maps_position(self):
        rec = _parse_espn_record(self._bal_record(0))
        assert rec.position == "WR"

    def test_maps_designation(self):
        rec = _parse_espn_record(self._bal_record(0))
        assert rec.designation == "Questionable"

    def test_source_is_espn(self):
        rec = _parse_espn_record(self._bal_record(0))
        assert rec.source == "espn"

    def test_practice_status_is_unavailable_sentinel(self):
        rec = _parse_espn_record(self._bal_record(0))
        assert rec.practice_status == PRACTICE_STATUS_UNAVAILABLE

    def test_injury_type_populated(self):
        rec = _parse_espn_record(self._bal_record(0))
        assert rec.injury_type == "Ankle"

    def test_updated_at_iso8601(self):
        rec = _parse_espn_record(self._bal_record(0))
        assert rec.updated_at == "2026-09-24T07:01Z"

    def test_active_record_no_details(self):
        # Active records (e.g. Buchanan) have no "details" key — should not raise.
        active_idx = next(
            i for i, r in enumerate(_load_espn_fixture("BAL")["injuries"])
            if r.get("status") == "Active"
        )
        rec = _parse_espn_record(_load_espn_fixture("BAL")["injuries"][active_idx])
        assert rec.designation == "Active"
        assert rec.injury_type is None

    def test_missing_athlete_returns_none(self):
        assert _parse_espn_record({}) is None
        assert _parse_espn_record({"status": "Out"}) is None


# ---------------------------------------------------------------------------
# _parse_league_record + league payload parsing — trimmed real-shape fixture
# ---------------------------------------------------------------------------

def _bal_league_block() -> dict:
    payload = _load_league_fixture()
    return next(b for b in payload["injuries"] if b["id"] == "33")


class TestParseLeagueRecord:
    def test_injured_record_maps_all_fields(self):
        raw = next(r for r in _bal_league_block()["injuries"] if r["status"] == "Questionable" and r["athlete"]["displayName"] == "Ronnie Stanley")
        rec = _parse_league_record(raw)
        assert rec.player_name == "Ronnie Stanley"
        assert rec.position == "OT"
        assert rec.designation == "Questionable"
        assert rec.injury_type == "Toe"
        assert rec.location == "Foot"
        assert rec.return_date == "2026-09-28"
        assert rec.source == "espn"
        assert rec.practice_status == PRACTICE_STATUS_UNAVAILABLE

    def test_active_record_with_no_details_block_does_not_raise(self):
        raw = next(r for r in _bal_league_block()["injuries"] if r["status"] == "Active")
        assert "details" not in raw   # fixture sanity check
        rec = _parse_league_record(raw)
        assert rec is not None
        assert rec.designation == "Active"
        assert rec.injury_type is None
        assert rec.location is None
        assert rec.return_date is None

    def test_missing_display_name_returns_none(self):
        assert _parse_league_record({"status": "Out", "athlete": {"displayName": ""}}) is None
        assert _parse_league_record({}) is None

    def test_not_specified_side_normalised_to_none(self):
        raw = next(r for r in _bal_league_block()["injuries"] if r["athlete"]["displayName"] == "Chris Moore")
        rec = _parse_league_record(raw)
        assert rec.side is None   # fixture has side="Not Specified"


# ---------------------------------------------------------------------------
# fetch_espn_league_payload / fetch_espn_league_injuries — league-wide fetch
# ---------------------------------------------------------------------------

class TestFetchEspnLeaguePayload:
    def test_single_request_returns_payload(self):
        payload = _load_league_fixture()
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            result = fetch_espn_league_payload()
        assert result == payload
        assert mock_get.call_count == 1

    def test_cache_hit_avoids_second_fetch(self):
        payload = _load_league_fixture()
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            fetch_espn_league_payload()
            fetch_espn_league_payload()
            fetch_espn_league_payload()
        # 15 games / many team lookups in one cycle must not cause 15 downloads.
        assert mock_get.call_count == 1

    def test_ttl_expiry_refetches(self, monkeypatch):
        payload = _load_league_fixture()
        monkeypatch.setenv("ESPN_INJURY_CACHE_TTL_SECONDS", "1")
        clock = {"t": 1000.0}
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get, \
             patch("reporter.enrich.injury_adapter.time.monotonic", side_effect=lambda: clock["t"]):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            fetch_espn_league_payload()
            clock["t"] += 2.0   # advance past the 1s TTL
            fetch_espn_league_payload()
        assert mock_get.call_count == 2

    def test_non_200_raises_espn_error(self):
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=403)
            with pytest.raises(ESPNError):
                fetch_espn_league_payload()

    def test_network_error_raises_espn_error(self):
        import requests as req_lib
        with patch("reporter.enrich.injury_adapter.requests.get",
                   side_effect=req_lib.ConnectionError("down")):
            with pytest.raises(ESPNError):
                fetch_espn_league_payload()


class TestFetchEspnLeagueInjuries:
    def test_bal_team_lookup_by_id(self):
        payload = _load_league_fixture()
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            records, failed, as_of = fetch_espn_league_injuries("BAL")
        assert {r.player_name for r in records} == {"Chris Moore", "Ronnie Stanley", "Marcus Buchanan"}
        assert failed == 0
        assert as_of == payload["timestamp"]

    def test_failed_record_is_counted_not_fatal(self):
        payload = _load_league_fixture()
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            # CLE fixture block has one record with an empty displayName.
            records, failed, as_of = fetch_espn_league_injuries("CLE")
        assert failed == 1
        assert records == []

    def test_unknown_team_block_returns_empty(self):
        payload = {"timestamp": "2026-09-28T14:32:00Z", "injuries": []}
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            records, failed, as_of = fetch_espn_league_injuries("BAL")
        assert records == []
        assert failed == 0
        assert as_of == payload["timestamp"]

    def test_team_lookups_share_one_cached_fetch(self):
        """15 games worth of team lookups must not cause 15 downloads."""
        payload = _load_league_fixture()
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            for abbr in ["BAL", "PHI", "DAL", "CLE"] * 4:
                fetch_espn_league_injuries(abbr)
        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# fetch_espn_injuries — mock _fetch_espn_ref_list + _fetch_one_ref
# ---------------------------------------------------------------------------

def _make_ref_urls(n: int) -> list[str]:
    return [f"http://fake.espn.com/injury/{i}" for i in range(n)]


def _bal_records_as_refs() -> tuple[list[str], dict[str, dict]]:
    """Return (ref_url_list, url→raw_record dict) for BAL fixture records."""
    records = _load_espn_fixture("BAL")["injuries"]
    urls = _make_ref_urls(len(records))
    url_to_record = {url: records[i] for i, url in enumerate(urls)}
    return urls, url_to_record


class TestFetchEspnInjuries:
    def test_returns_list_of_injury_records(self):
        urls, url_to_record = _bal_records_as_refs()
        with patch("reporter.enrich.injury_adapter._fetch_espn_ref_list", return_value=urls), \
             patch("reporter.enrich.injury_adapter._fetch_one_ref", side_effect=url_to_record.get):
            result = fetch_espn_injuries("BAL")
        assert isinstance(result, list)
        assert all(isinstance(r, InjuryRecord) for r in result)

    def test_count_matches_fixture(self):
        urls, url_to_record = _bal_records_as_refs()
        with patch("reporter.enrich.injury_adapter._fetch_espn_ref_list", return_value=urls), \
             patch("reporter.enrich.injury_adapter._fetch_one_ref", side_effect=url_to_record.get):
            result = fetch_espn_injuries("BAL")
        # All 10 fixture records should be parsed
        assert len(result) == 10

    def test_all_records_source_espn(self):
        urls, url_to_record = _bal_records_as_refs()
        with patch("reporter.enrich.injury_adapter._fetch_espn_ref_list", return_value=urls), \
             patch("reporter.enrich.injury_adapter._fetch_one_ref", side_effect=url_to_record.get):
            result = fetch_espn_injuries("BAL")
        assert all(r.source == "espn" for r in result)

    def test_practice_status_unavailable_on_all_records(self):
        urls, url_to_record = _bal_records_as_refs()
        with patch("reporter.enrich.injury_adapter._fetch_espn_ref_list", return_value=urls), \
             patch("reporter.enrich.injury_adapter._fetch_one_ref", side_effect=url_to_record.get):
            result = fetch_espn_injuries("BAL")
        assert all(r.practice_status == PRACTICE_STATUS_UNAVAILABLE for r in result)

    def test_failed_ref_is_skipped_not_fatal(self):
        urls, url_to_record = _bal_records_as_refs()

        def flaky_ref(url):
            if url == urls[2]:
                return None   # simulate failed ref
            return url_to_record.get(url)

        with patch("reporter.enrich.injury_adapter._fetch_espn_ref_list", return_value=urls), \
             patch("reporter.enrich.injury_adapter._fetch_one_ref", side_effect=flaky_ref):
            result = fetch_espn_injuries("BAL")
        # One skipped, rest should be present
        assert len(result) == 9

    def test_empty_ref_list_returns_empty(self):
        with patch("reporter.enrich.injury_adapter._fetch_espn_ref_list", return_value=[]):
            result = fetch_espn_injuries("BAL")
        assert result == []

    def test_espn_error_propagates(self):
        with patch("reporter.enrich.injury_adapter._fetch_espn_ref_list",
                   side_effect=ESPNError("timeout")):
            with pytest.raises(ESPNError):
                fetch_espn_injuries("BAL")

    def test_known_designations_present(self):
        urls, url_to_record = _bal_records_as_refs()
        with patch("reporter.enrich.injury_adapter._fetch_espn_ref_list", return_value=urls), \
             patch("reporter.enrich.injury_adapter._fetch_one_ref", side_effect=url_to_record.get):
            result = fetch_espn_injuries("BAL")
        designations = {r.designation for r in result}
        assert "Questionable" in designations
        assert "Active" in designations


# ---------------------------------------------------------------------------
# fetch_nflverse_injuries — mock requests.get
# ---------------------------------------------------------------------------

def _mock_nflverse_response(rows: list[dict]) -> MagicMock:
    mock = MagicMock()
    mock.status_code = 200
    mock.text = _nflverse_csv_text(rows)
    return mock


def _ari_rows() -> list[dict]:
    return [r for r in _load_nflverse_fixture() if r["team"] == "ARI"]


class TestFetchNflverseInjuries:
    def test_returns_list_for_known_team(self):
        rows = _ari_rows()
        with patch("reporter.enrich.injury_adapter.requests.get",
                   return_value=_mock_nflverse_response(rows)):
            result = fetch_nflverse_injuries("ARI")
        assert isinstance(result, list)
        assert len(result) > 0

    def test_source_is_nflverse(self):
        rows = _ari_rows()
        with patch("reporter.enrich.injury_adapter.requests.get",
                   return_value=_mock_nflverse_response(rows)):
            result = fetch_nflverse_injuries("ARI")
        assert all(r.source == "nflverse" for r in result)

    def test_practice_status_populated(self):
        rows = _ari_rows()
        with patch("reporter.enrich.injury_adapter.requests.get",
                   return_value=_mock_nflverse_response(rows)):
            result = fetch_nflverse_injuries("ARI")
        # At least one row should have a non-None practice_status.
        statuses = [r.practice_status for r in result if r.practice_status is not None]
        assert len(statuses) > 0

    def test_practice_status_not_unavailable_sentinel(self):
        rows = _ari_rows()
        with patch("reporter.enrich.injury_adapter.requests.get",
                   return_value=_mock_nflverse_response(rows)):
            result = fetch_nflverse_injuries("ARI")
        for r in result:
            assert r.practice_status != PRACTICE_STATUS_UNAVAILABLE

    def test_week_filter(self):
        rows = _ari_rows()
        with patch("reporter.enrich.injury_adapter.requests.get",
                   return_value=_mock_nflverse_response(rows)):
            result = fetch_nflverse_injuries("ARI", week=3)
        assert all(True for r in result)  # just confirm it doesn't crash

    def test_unknown_team_returns_empty(self):
        rows = _ari_rows()
        with patch("reporter.enrich.injury_adapter.requests.get",
                   return_value=_mock_nflverse_response(rows)):
            result = fetch_nflverse_injuries("XYZ")
        assert result == []

    def test_http_error_raises_nflverse_error(self):
        mock = MagicMock()
        mock.status_code = 503
        with patch("reporter.enrich.injury_adapter.requests.get", return_value=mock):
            with pytest.raises(NflverseError):
                fetch_nflverse_injuries("ARI")

    def test_network_error_raises_nflverse_error(self):
        import requests as req_lib
        with patch("reporter.enrich.injury_adapter.requests.get",
                   side_effect=req_lib.ConnectionError("down")):
            with pytest.raises(NflverseError):
                fetch_nflverse_injuries("ARI")


# ---------------------------------------------------------------------------
# get_team_injuries — composite: ESPN primary, nflverse fallback
# ---------------------------------------------------------------------------

def _nflverse_ok_records(team_abbr: str = "BAL") -> list[InjuryRecord]:
    return [
        InjuryRecord(
            player_name="Test Player",
            position="WR",
            designation="Questionable",
            practice_status="Limited Participation in Practice",
            injury_type="Hamstring",
            source="nflverse",
            updated_at=None,
        )
    ]


class TestGetTeamInjuries:
    # -- ESPN (league-wide, default) success --

    def test_espn_success_returns_espn_source(self):
        records = [InjuryRecord("A", "QB", "Out", PRACTICE_STATUS_UNAVAILABLE, None, "espn", None)]
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   return_value=(records, 0, "2026-09-28T14:32:00Z")):
            result = get_team_injuries("BAL")
        assert result.source == "espn"
        assert result.status == "ok"
        assert result.records == records

    def test_espn_success_practice_status_is_unavailable(self):
        records = [InjuryRecord("A", "QB", "Out", PRACTICE_STATUS_UNAVAILABLE, None, "espn", None)]
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   return_value=(records, 0, "2026-09-28T14:32:00Z")):
            result = get_team_injuries("BAL")
        assert all(r.practice_status == PRACTICE_STATUS_UNAVAILABLE for r in result.records)

    def test_espn_success_carries_as_of_from_payload(self):
        records = [InjuryRecord("A", "QB", "Out", PRACTICE_STATUS_UNAVAILABLE, None, "espn", None)]
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   return_value=(records, 0, "2026-09-28T14:32:00Z")):
            result = get_team_injuries("BAL")
        assert result.as_of == "2026-09-28T14:32:00Z"

    def test_espn_success_carries_failed_count(self):
        records = [InjuryRecord("A", "QB", "Out", PRACTICE_STATUS_UNAVAILABLE, None, "espn", None)]
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   return_value=(records, 2, "2026-09-28T14:32:00Z")):
            result = get_team_injuries("BAL")
        assert result.failed_count == 2

    def test_espn_zero_records_status_no_designations(self):
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   return_value=([], 0, "2026-09-28T14:32:00Z")):
            result = get_team_injuries("BAL")
        assert result.source == "espn"
        assert result.status == "no_designations"
        assert result.records == []
        assert not result.is_unavailable

    def test_legacy_per_team_path_not_used_by_default(self):
        """The old sports.core.api.espn.com fan-out 403s from production and
        must not run unless ESPN_LEGACY_PER_TEAM is explicitly enabled."""
        with patch("reporter.enrich.injury_adapter.fetch_espn_injuries") as legacy_mock, \
             patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   return_value=([], 0, None)):
            get_team_injuries("BAL")
        legacy_mock.assert_not_called()

    def test_legacy_per_team_path_used_when_flag_enabled(self, monkeypatch):
        monkeypatch.setenv("ESPN_LEGACY_PER_TEAM", "true")
        records = [InjuryRecord("A", "QB", "Out", PRACTICE_STATUS_UNAVAILABLE, None, "espn", None)]
        with patch("reporter.enrich.injury_adapter.fetch_espn_injuries", return_value=records) as legacy_mock, \
             patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries") as league_mock:
            result = get_team_injuries("BAL")
        legacy_mock.assert_called_once()
        league_mock.assert_not_called()
        assert result.source == "espn"
        assert result.records == records

    # -- ESPN failure → nflverse fallback --

    def test_espn_failure_triggers_nflverse_fallback(self):
        nflverse_records = _nflverse_ok_records()
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("HTTP 403")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   return_value=(nflverse_records, 2026, 3)):
            result = get_team_injuries("BAL")
        assert result.source == "nflverse"
        assert result.status == "ok"

    def test_nflverse_fallback_practice_status_populated(self):
        nflverse_records = _nflverse_ok_records()
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("timeout")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   return_value=(nflverse_records, 2026, 3)):
            result = get_team_injuries("BAL")
        assert all(
            r.practice_status != PRACTICE_STATUS_UNAVAILABLE
            for r in result.records
        )
        assert result.records[0].practice_status == "Limited Participation in Practice"

    def test_nflverse_fallback_source_tagged_correctly(self):
        nflverse_records = _nflverse_ok_records()
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("bad response")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   return_value=(nflverse_records, 2026, 3)):
            result = get_team_injuries("BAL")
        assert all(r.source == "nflverse" for r in result.records)

    def test_nflverse_fallback_carries_season_and_week(self):
        nflverse_records = _nflverse_ok_records()
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("down")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   return_value=(nflverse_records, 2026, 3)):
            result = get_team_injuries("BAL")
        assert result.nflverse_season == 2026
        assert result.nflverse_week == 3

    def test_nflverse_fallback_stale_when_week_not_current(self):
        """Schedule-driven current-week path (get_current_week), not the
        date-heuristic fallback -- these are now different things."""
        nflverse_records = _nflverse_ok_records()
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("down")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   return_value=(nflverse_records, 2026, 3)), \
             patch("reporter.enrich.injury_adapter.get_current_week", return_value=9):
            result = get_team_injuries("BAL")
        assert result.stale is True

    def test_nflverse_fallback_not_stale_when_week_matches_current(self):
        nflverse_records = _nflverse_ok_records()
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("down")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   return_value=(nflverse_records, 2026, 3)), \
             patch("reporter.enrich.injury_adapter.get_current_week", return_value=3):
            result = get_team_injuries("BAL")
        assert result.stale is False

    def test_nflverse_fallback_stale_check_degrades_to_date_heuristic_on_schedule_failure(self):
        """If the schedule-driven current-week lookup itself fails (nflreadpy
        down), the stale check must degrade to the old date heuristic rather
        than crashing or silently treating the data as not-stale -- and it
        must log that the fallback happened (see _resolve_current_week)."""
        nflverse_records = _nflverse_ok_records()
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("down")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   return_value=(nflverse_records, 2026, 3)), \
             patch("reporter.enrich.injury_adapter.get_current_week",
                   side_effect=NflreadpyError("nflreadpy down")), \
             patch("reporter.enrich.injury_adapter._current_nfl_week", return_value=9) as mock_heuristic:
            result = get_team_injuries("BAL")
        mock_heuristic.assert_called_once()
        assert result.stale is True

    # -- Both sources fail --

    def test_both_fail_returns_unavailable(self):
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("down")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   side_effect=NflverseError("also down")):
            result = get_team_injuries("BAL")
        assert result is _UNAVAILABLE

    def test_both_fail_status_unavailable(self):
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("down")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   side_effect=NflverseError("also down")):
            result = get_team_injuries("BAL")
        assert result.status == "unavailable"
        assert result.source == "unavailable"
        assert result.records == []
        assert result.is_unavailable

    def test_both_fail_returns_empty_list_not_none(self):
        with patch("reporter.enrich.injury_adapter.fetch_espn_league_injuries",
                   side_effect=ESPNError("down")), \
             patch("reporter.enrich.injury_adapter.fetch_nflverse_injuries_with_meta",
                   side_effect=NflverseError("also down")):
            result = get_team_injuries("BAL")
        assert result.records is not None
        assert isinstance(result.records, list)


# ---------------------------------------------------------------------------
# Abbreviation override mapping (LA → ESPN 14, WAS → ESPN 28)
# ---------------------------------------------------------------------------

class TestAbbreviationMapping:
    def test_la_maps_to_espn_id_14(self):
        from reporter.enrich.injury_adapter import _espn_team_id
        assert _espn_team_id("LA") == 14

    def test_was_maps_to_espn_id_28(self):
        from reporter.enrich.injury_adapter import _espn_team_id
        assert _espn_team_id("WAS") == 28

    def test_la_lowercase(self):
        from reporter.enrich.injury_adapter import _espn_team_id
        assert _espn_team_id("la") == 14

    def test_was_lowercase(self):
        from reporter.enrich.injury_adapter import _espn_team_id
        assert _espn_team_id("was") == 28

    def test_all_32_teams_in_lookup(self):
        assert len(_ABBR_TO_ESPN_ID) == 32

    def test_bal_maps_to_espn_id_33(self):
        from reporter.enrich.injury_adapter import _espn_team_id
        assert _espn_team_id("BAL") == 33

    def test_unknown_abbr_raises_value_error(self):
        from reporter.enrich.injury_adapter import _espn_team_id
        with pytest.raises(ValueError):
            _espn_team_id("XYZ")

    def test_la_get_team_injuries_uses_correct_espn_id(self):
        """LA must resolve to ESPN team ID 14 (Rams), not any LAC/LAR ID."""
        payload = {
            "timestamp": "2026-09-28T14:32:00Z",
            "injuries": [{"id": "14", "displayName": "Los Angeles Rams", "injuries": []}],
        }
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            result = get_team_injuries("LA")
        assert result.status == "no_designations"
        assert result.source == "espn"

    def test_was_get_team_injuries_uses_correct_espn_id(self):
        payload = {
            "timestamp": "2026-09-28T14:32:00Z",
            "injuries": [{"id": "28", "displayName": "Washington Commanders", "injuries": []}],
        }
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            result = get_team_injuries("WAS")
        assert result.status == "no_designations"
        assert result.source == "espn"

    def test_team_block_lookup_uses_correct_espn_id(self):
        """A payload containing only WAS's id (28) must resolve for team_abbr=WAS,
        proving the id->abbr mapping (not a name match) drives the lookup."""
        from reporter.enrich.injury_adapter import fetch_espn_league_injuries
        payload = {
            "timestamp": "2026-09-28T14:32:00Z",
            "injuries": [{
                "id": "28",
                "displayName": "Washington Commanders",
                "injuries": [{
                    "status": "Out",
                    "athlete": {"displayName": "Test Player", "position": {"abbreviation": "QB"}},
                }],
            }],
        }
        with patch("reporter.enrich.injury_adapter.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
            records, failed, _ = fetch_espn_league_injuries("WAS")
        assert len(records) == 1
        assert records[0].player_name == "Test Player"


# ---------------------------------------------------------------------------
# InjuryResult properties
# ---------------------------------------------------------------------------

class TestInjuryResultProperties:
    def test_is_unavailable_true(self):
        assert _UNAVAILABLE.is_unavailable

    def test_is_unavailable_false_on_ok(self):
        r = InjuryResult(records=[], source="espn", status="ok")
        assert not r.is_unavailable

    def test_has_designations_false_when_empty(self):
        r = InjuryResult(records=[], source="espn", status="ok")
        assert not r.has_designations

    def test_has_designations_true_when_records(self):
        rec = InjuryRecord("P", "WR", "Out", PRACTICE_STATUS_UNAVAILABLE, None, "espn", None)
        r = InjuryResult(records=[rec], source="espn", status="ok")
        assert r.has_designations

    def test_has_designations_false_when_unavailable(self):
        rec = InjuryRecord("P", "WR", "Out", PRACTICE_STATUS_UNAVAILABLE, None, "espn", None)
        r = InjuryResult(records=[rec], source="unavailable", status="unavailable")
        assert not r.has_designations
