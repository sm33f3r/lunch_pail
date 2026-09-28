"""
Unit tests for reporter.watcher.game_assembly.

All tests run offline — no network access required.
Fixture events are constructed manually to control gameId grouping
and moneyline presence.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch, call

import pytest

os.environ["VOLUME_THRESHOLD"] = "5000"
os.environ["VOLUME_WINDOW"] = "24hr"
os.environ.setdefault("REPORT_OUTPUT_DIR", "/tmp/reporter_test")
os.environ.setdefault("POLLING_INTERVAL_SECONDS", "300")

from reporter.watcher.game_assembly import (  # noqa: E402
    build_game_report_data,
    consolidate_events_by_game_id,
    select_representative_event,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _load(name: str):
    with open(_FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _moneyline_market(market_id="ml1", condition_id="0xabc", outcomes=None, prices=None):
    """Minimal market dict that classify_market() will call 'moneyline'."""
    return {
        "id": market_id,
        "conditionId": condition_id,
        "sportsMarketType": "moneyline",
        "outcomes": json.dumps(outcomes or ["Away", "Home"]),
        "outcomePrices": json.dumps(prices or ["0.5", "0.5"]),
        "groupItemTitle": None,
    }


def _spread_market(market_id="sp1", condition_id="0xspread", line="-3.5"):
    """Minimal market dict that classify_market() will call 'spread'."""
    return {
        "id": market_id,
        "conditionId": condition_id,
        "sportsMarketType": "spreads",
        "outcomes": json.dumps(["Away", "Home"]),
        "outcomePrices": json.dumps(["0.48", "0.52"]),
        "groupItemTitle": f"Spread {line}",
    }


def _total_market(market_id="tot1", condition_id="0xtotal", line="44.5"):
    """Minimal market dict that classify_market() will call 'total'."""
    return {
        "id": market_id,
        "conditionId": condition_id,
        "sportsMarketType": "totals",
        "outcomes": json.dumps(["Over", "Under"]),
        "outcomePrices": json.dumps(["0.51", "0.49"]),
        "groupItemTitle": f"O/U {line}",
    }


def _prop_market(market_id="prop1"):
    return {
        "id": market_id,
        "conditionId": "0xprop",
        "sportsMarketType": "exact_margin",
        "groupItemTitle": "Exact Margin: Away by 1-6",
    }


def _game_event(game_id, slug, title, volume_24hr, markets):
    """Minimal event dict with the fields game_assembly.py uses."""
    return {
        "id": f"ev-{game_id}-{slug}",
        "gameId": game_id,
        "slug": slug,
        "title": title,
        "closed": False,
        "volume24hr": volume_24hr,
        "volume1wk": volume_24hr,
        "volume1mo": volume_24hr,
        "volume1yr": volume_24hr,
        "volume": volume_24hr,
        "markets": markets,
    }


# Two real events sharing gameId 100: one has moneyline, one is a prop event
_ML_EVENT = _game_event(
    game_id=100,
    slug="nfl-bal-dal-2026-09-27",
    title="Ravens vs. Cowboys",
    volume_24hr=20000.0,
    markets=[_moneyline_market()],
)

_PROP_EVENT = _game_event(
    game_id=100,
    slug="nfl-bal-dal-2026-09-27-highest-scoring-quarter",
    title="Ravens vs. Cowboys - Highest Scoring Quarter",
    volume_24hr=0.0,
    markets=[_prop_market()],
)

# A game with no moneyline anywhere
_NO_ML_EVENT = _game_event(
    game_id=200,
    slug="nfl-atl-gb-2026-09-25-props",
    title="Falcons vs. Packers - Props",
    volume_24hr=30000.0,
    markets=[_prop_market()],
)

# A standalone game well above threshold
_SOLO_EVENT = _game_event(
    game_id=300,
    slug="nfl-kc-mia-2026-09-27",
    title="Chiefs vs. Dolphins",
    volume_24hr=50000.0,
    markets=[_moneyline_market(market_id="ml2", condition_id="0xdef")],
)

# A standalone game below threshold
_LOW_VOL_EVENT = _game_event(
    game_id=400,
    slug="nfl-ne-ten-2026-09-27",
    title="Patriots vs. Titans",
    volume_24hr=100.0,
    markets=[_moneyline_market(market_id="ml3", condition_id="0x999")],
)

# A game with moneyline + spread + total markets
_FULL_MARKETS_EVENT = _game_event(
    game_id=500,
    slug="nfl-sf-sea-2026-09-28",
    title="49ers vs. Seahawks",
    volume_24hr=30000.0,
    markets=[
        _moneyline_market(market_id="ml4", condition_id="0xml4"),
        _spread_market(market_id="sp4", condition_id="0xsp4"),
        _total_market(market_id="tot4", condition_id="0xtot4"),
    ],
)


# ---------------------------------------------------------------------------
# consolidate_events_by_game_id
# ---------------------------------------------------------------------------

class TestConsolidateByGameId:
    def test_groups_shared_game_ids(self):
        result = consolidate_events_by_game_id([_ML_EVENT, _PROP_EVENT, _SOLO_EVENT])
        assert 100 in result
        assert 300 in result
        assert len(result[100]) == 2
        assert len(result[300]) == 1

    def test_null_game_id_excluded(self):
        null_event = {"id": "x", "gameId": None, "markets": []}
        result = consolidate_events_by_game_id([null_event, _SOLO_EVENT])
        assert None not in result
        assert 300 in result

    def test_empty_input_returns_empty(self):
        assert consolidate_events_by_game_id([]) == {}

    def test_single_event_per_game_id(self):
        result = consolidate_events_by_game_id([_ML_EVENT])
        assert result == {100: [_ML_EVENT]}

    def test_all_events_assigned(self):
        events = [_ML_EVENT, _PROP_EVENT, _SOLO_EVENT, _LOW_VOL_EVENT]
        result = consolidate_events_by_game_id(events)
        total = sum(len(v) for v in result.values())
        assert total == len(events)


# ---------------------------------------------------------------------------
# select_representative_event
# ---------------------------------------------------------------------------

class TestSelectRepresentativeEvent:
    def test_picks_moneyline_bearing_event(self):
        rep = select_representative_event([_ML_EVENT, _PROP_EVENT])
        assert rep is _ML_EVENT

    def test_returns_none_when_no_moneyline(self):
        rep = select_representative_event([_NO_ML_EVENT])
        assert rep is None

    def test_tiebreak_by_volume(self):
        high_vol = _game_event(
            game_id=100, slug="nfl-bal-dal-2026-09-27-alt", title="Ravens vs. Cowboys Alt",
            volume_24hr=99000.0, markets=[_moneyline_market(market_id="ml_high")]
        )
        rep = select_representative_event([_ML_EVENT, high_vol])
        assert rep is high_vol

    def test_single_moneyline_event_returned(self):
        rep = select_representative_event([_ML_EVENT])
        assert rep is _ML_EVENT

    def test_empty_list_returns_none(self):
        assert select_representative_event([]) is None


# ---------------------------------------------------------------------------
# build_game_report_data
# ---------------------------------------------------------------------------

class TestBuildGameReportData:
    def test_returns_none_when_no_moneyline(self):
        result = build_game_report_data(200, [_NO_ML_EVENT])
        assert result is None

    def test_returns_none_when_below_threshold(self):
        # _LOW_VOL_EVENT has $100 volume < $5000 threshold
        result = build_game_report_data(400, [_LOW_VOL_EVENT])
        assert result is None

    def test_volume_filter_uses_representative_not_prop(self):
        # gameId=100: ML event has $20000 (above $5000), prop has $0
        # If filter used the prop event, it would return None (wrong)
        # It should use the ML event and return a result (correct)
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 1.0, "recent_trades": []}
            result = build_game_report_data(100, [_ML_EVENT, _PROP_EVENT])
        assert result is not None, "Should pass because ML event volume=$20000 > $5000 threshold"

    def test_result_structure(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 1.0, "recent_trades": []}
            result = build_game_report_data(100, [_ML_EVENT, _PROP_EVENT])
        assert result is not None
        for key in ["game_id", "away_team", "home_team", "away_abbr", "home_abbr",
                    "game_date", "event_slug", "event_url", "volume", "moneyline",
                    "spreads", "totals"]:
            assert key in result, f"Missing key: {key}"

    def test_result_game_id(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {}, "open_interest": 0.0, "recent_trades": []}
            result = build_game_report_data(100, [_ML_EVENT, _PROP_EVENT])
        assert result["game_id"] == 100

    def test_result_team_identity(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {}, "open_interest": 0.0, "recent_trades": []}
            result = build_game_report_data(100, [_ML_EVENT, _PROP_EVENT])
        assert result["away_abbr"] == "BAL"
        assert result["home_abbr"] == "DAL"

    def test_result_event_url_format(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {}, "open_interest": 0.0, "recent_trades": []}
            result = build_game_report_data(100, [_ML_EVENT, _PROP_EVENT])
        assert result["event_url"] == "https://polymarket.com/event/nfl-bal-dal-2026-09-27"

    def test_result_volume(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {}, "open_interest": 0.0, "recent_trades": []}
            result = build_game_report_data(100, [_ML_EVENT, _PROP_EVENT])
        assert result["volume"] == 20000.0


# ---------------------------------------------------------------------------
# Moneyline-primary data fetching: spreads/totals use extract_prices only
# ---------------------------------------------------------------------------

class TestMarketDataFetchingScope:
    """
    Verifies that extract_market_data (network: OI + trades) is called only
    for the moneyline, and that spreads/totals are populated using the pure
    extract_prices() function with no network calls.
    """

    def test_extract_market_data_called_only_for_moneyline(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 100.0, "recent_trades": []}
            build_game_report_data(500, [_FULL_MARKETS_EVENT])
        # extract_market_data must be called exactly once (for the moneyline only)
        assert mock_emd.call_count == 1

    def test_spreads_entry_has_prices_and_line(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 100.0, "recent_trades": []}
            result = build_game_report_data(500, [_FULL_MARKETS_EVENT])
        assert result is not None
        assert len(result["spreads"]) == 1
        entry = result["spreads"][0]
        assert set(entry.keys()) == {"prices", "line"}, f"Expected {{'prices','line'}}, got {set(entry.keys())}"
        assert "open_interest" not in entry
        assert "recent_trades" not in entry

    def test_totals_entry_has_prices_and_line(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 100.0, "recent_trades": []}
            result = build_game_report_data(500, [_FULL_MARKETS_EVENT])
        assert result is not None
        assert len(result["totals"]) == 1
        entry = result["totals"][0]
        assert set(entry.keys()) == {"prices", "line"}, f"Expected {{'prices','line'}}, got {set(entry.keys())}"
        assert "open_interest" not in entry
        assert "recent_trades" not in entry

    def test_spreads_prices_correct(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 100.0, "recent_trades": []}
            result = build_game_report_data(500, [_FULL_MARKETS_EVENT])
        assert result is not None
        spread_prices = result["spreads"][0]["prices"]
        assert spread_prices == pytest.approx({"Away": 0.48, "Home": 0.52})

    def test_spreads_line_label_parsed(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 100.0, "recent_trades": []}
            result = build_game_report_data(500, [_FULL_MARKETS_EVENT])
        assert result is not None
        # _spread_market() sets groupItemTitle="Spread -3.5" so line should be "-3.5"
        assert result["spreads"][0]["line"] == "-3.5"

    def test_totals_prices_correct(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 100.0, "recent_trades": []}
            result = build_game_report_data(500, [_FULL_MARKETS_EVENT])
        assert result is not None
        total_prices = result["totals"][0]["prices"]
        assert total_prices == pytest.approx({"Over": 0.51, "Under": 0.49})

    def test_totals_line_label_parsed(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 100.0, "recent_trades": []}
            result = build_game_report_data(500, [_FULL_MARKETS_EVENT])
        assert result is not None
        # _total_market() sets groupItemTitle="O/U 44.5" so line should be "44.5"
        assert result["totals"][0]["line"] == "44.5"

    def test_moneyline_still_has_full_data(self):
        ml_data = {"prices": {"Away": 0.6, "Home": 0.4}, "open_interest": 500.0, "recent_trades": [{"side": "BUY"}]}
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = ml_data
            result = build_game_report_data(500, [_FULL_MARKETS_EVENT])
        assert result is not None
        assert result["moneyline"]["prices"] == {"Away": 0.6, "Home": 0.4}
        assert result["moneyline"]["open_interest"] == 500.0
        assert result["moneyline"]["recent_trades"] == [{"side": "BUY"}]
        assert result["moneyline"]["open_interest_status"] == "ok"
        assert result["moneyline"]["open_interest_reason"] is None


# ---------------------------------------------------------------------------
# PriceDataError tolerance: spreads/totals skipped, moneyline skips game
# ---------------------------------------------------------------------------

def _bad_spread_market(market_id="sp_bad"):
    """Spread market whose prices don't sum to 1.0 — extract_prices() will raise."""
    return {
        "id": market_id,
        "conditionId": "0xbad",
        "sportsMarketType": "spreads",
        "outcomes": json.dumps(["Away", "Home"]),
        "outcomePrices": json.dumps(["0.10", "0.10"]),  # sums to 0.20, not ~1.0
        "groupItemTitle": "Spread -3.5",
    }


def _bad_moneyline_market(market_id="ml_bad"):
    """Moneyline market whose prices don't sum to 1.0 — extract_prices() will raise."""
    return {
        "id": market_id,
        "conditionId": "0xmlbad",
        "sportsMarketType": "moneyline",
        "outcomes": json.dumps(["Away", "Home"]),
        "outcomePrices": json.dumps(["0.10", "0.10"]),  # sums to 0.20, not ~1.0
        "groupItemTitle": None,
    }


# Game 600: moneyline + one good spread + one BAD spread + one total
_MIXED_SPREADS_EVENT = _game_event(
    game_id=600,
    slug="nfl-no-min-2026-09-28",
    title="Saints vs. Vikings",
    volume_24hr=20000.0,
    markets=[
        _moneyline_market(market_id="ml6", condition_id="0xml6"),
        _spread_market(market_id="sp6_good", condition_id="0xsp6good"),
        _bad_spread_market(market_id="sp6_bad"),
        _total_market(market_id="tot6", condition_id="0xtot6"),
    ],
)

# Game 700: moneyline with bad prices
_BAD_ML_EVENT = _game_event(
    game_id=700,
    slug="nfl-ind-jax-2026-09-28",
    title="Colts vs. Jaguars",
    volume_24hr=20000.0,
    markets=[_bad_moneyline_market()],
)


class TestPriceDataErrorTolerance:
    def test_bad_spread_skipped_game_still_assembles(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 1.0, "recent_trades": []}
            result = build_game_report_data(600, [_MIXED_SPREADS_EVENT])
        assert result is not None, "Game should still assemble when only one spread line is bad"

    def test_bad_spread_excluded_from_result(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 1.0, "recent_trades": []}
            result = build_game_report_data(600, [_MIXED_SPREADS_EVENT])
        assert result is not None
        # Only the good spread survives; bad one is excluded
        assert len(result["spreads"]) == 1
        entry = result["spreads"][0]
        assert entry["prices"] == pytest.approx({"Away": 0.48, "Home": 0.52})
        assert "line" in entry

    def test_good_total_unaffected_by_bad_spread(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 1.0, "recent_trades": []}
            result = build_game_report_data(600, [_MIXED_SPREADS_EVENT])
        assert result is not None
        assert len(result["totals"]) == 1
        assert result["totals"][0]["prices"] == pytest.approx({"Over": 0.51, "Under": 0.49})

    def test_bad_spread_logged_to_skip_log(self):
        skip_log: list = []
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            mock_emd.return_value = {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 1.0, "recent_trades": []}
            build_game_report_data(600, [_MIXED_SPREADS_EVENT], _skip_log=skip_log)
        assert len(skip_log) == 1
        label, gid, mid = skip_log[0]
        assert label == "spread"
        assert gid == 600
        assert mid == "sp6_bad"

    def test_bad_moneyline_skips_entire_game(self):
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            from reporter.watcher.market_data import PriceDataError
            mock_emd.side_effect = PriceDataError("prices sum to 0.20")
            result = build_game_report_data(700, [_BAD_ML_EVENT])
        assert result is None, "Bad moneyline prices must cause the entire game to be skipped"

    def test_bad_moneyline_does_not_log_to_skip_log(self):
        # skip_log only tracks spread/total skips, not moneyline failures
        skip_log: list = []
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd:
            from reporter.watcher.market_data import PriceDataError
            mock_emd.side_effect = PriceDataError("prices sum to 0.20")
            build_game_report_data(700, [_BAD_ML_EVENT], _skip_log=skip_log)
        assert skip_log == []


# ---------------------------------------------------------------------------
# OpenInterestError tolerance: OI failure degrades the game, never aborts it
# ---------------------------------------------------------------------------

# Game 800: moneyline with good prices, e.g. a brand-new market whose /oi
# endpoint returns an empty list (reproduces the DEN @ SF production incident).
_OI_FAIL_EVENT = _game_event(
    game_id=800,
    slug="nfl-den-sf-2026-09-28",
    title="Broncos vs. 49ers",
    volume_24hr=1051.0 + 5000.0,  # comfortably above threshold
    markets=[_moneyline_market(market_id="ml8", condition_id="0xml8")],
)


class TestOpenInterestErrorTolerance:
    def test_oi_failure_does_not_abort_game(self):
        from reporter.watcher.market_data import OpenInterestError
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd, \
             patch("reporter.watcher.game_assembly.fetch_recent_trades", return_value=[]):
            mock_emd.side_effect = OpenInterestError(
                "Unexpected /oi response shape for conditionId='0xml8': []"
            )
            result = build_game_report_data(800, [_OI_FAIL_EVENT])
        assert result is not None, "An OI fetch failure must not abort the game's report"

    def test_oi_failure_prices_still_present(self):
        from reporter.watcher.market_data import OpenInterestError
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd, \
             patch("reporter.watcher.game_assembly.fetch_recent_trades", return_value=[]):
            mock_emd.side_effect = OpenInterestError("empty /oi response")
            result = build_game_report_data(800, [_OI_FAIL_EVENT])
        assert result["moneyline"]["prices"] == pytest.approx({"Away": 0.5, "Home": 0.5})

    def test_oi_failure_open_interest_is_none_not_zero(self):
        from reporter.watcher.market_data import OpenInterestError
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd, \
             patch("reporter.watcher.game_assembly.fetch_recent_trades", return_value=[]):
            mock_emd.side_effect = OpenInterestError("empty /oi response")
            result = build_game_report_data(800, [_OI_FAIL_EVENT])
        assert result["moneyline"]["open_interest"] is None

    def test_oi_failure_status_and_reason_set(self):
        from reporter.watcher.market_data import OpenInterestError
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd, \
             patch("reporter.watcher.game_assembly.fetch_recent_trades", return_value=[]):
            mock_emd.side_effect = OpenInterestError("empty /oi response")
            result = build_game_report_data(800, [_OI_FAIL_EVENT])
        assert result["moneyline"]["open_interest_status"] == "unavailable"
        assert "empty /oi response" in result["moneyline"]["open_interest_reason"]

    def test_oi_failure_trades_still_fetched(self):
        from reporter.watcher.market_data import OpenInterestError
        trades = [{"side": "BUY", "size": "10", "price": "0.5"}]
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd, \
             patch("reporter.watcher.game_assembly.fetch_recent_trades", return_value=trades) as mock_trades:
            mock_emd.side_effect = OpenInterestError("empty /oi response")
            result = build_game_report_data(800, [_OI_FAIL_EVENT])
        mock_trades.assert_called_once_with("0xml8")
        assert result["moneyline"]["recent_trades"] == trades

    def test_oi_failure_trades_fetch_also_fails_falls_back_to_empty(self):
        from reporter.watcher.market_data import OpenInterestError
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd, \
             patch("reporter.watcher.game_assembly.fetch_recent_trades",
                   side_effect=RuntimeError("trades endpoint down")):
            mock_emd.side_effect = OpenInterestError("empty /oi response")
            result = build_game_report_data(800, [_OI_FAIL_EVENT])
        assert result is not None, "Trades also failing must not abort the game either"
        assert result["moneyline"]["recent_trades"] == []

    def test_oi_failure_not_logged_to_skip_log(self):
        # skip_log only tracks spread/total skips, not moneyline OI degradation
        from reporter.watcher.market_data import OpenInterestError
        skip_log: list = []
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd, \
             patch("reporter.watcher.game_assembly.fetch_recent_trades", return_value=[]):
            mock_emd.side_effect = OpenInterestError("empty /oi response")
            build_game_report_data(800, [_OI_FAIL_EVENT], _skip_log=skip_log)
        assert skip_log == []

    def test_oi_failure_logs_warning(self, capsys):
        from reporter.watcher.market_data import OpenInterestError
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd, \
             patch("reporter.watcher.game_assembly.fetch_recent_trades", return_value=[]):
            mock_emd.side_effect = OpenInterestError("empty /oi response")
            build_game_report_data(800, [_OI_FAIL_EVENT])
        out = capsys.readouterr().out
        assert "open interest unavailable" in out
        assert "800" in out

    def test_requests_http_error_also_degrades_not_aborts(self):
        import requests
        with patch("reporter.watcher.game_assembly.extract_market_data") as mock_emd, \
             patch("reporter.watcher.game_assembly.fetch_recent_trades", return_value=[]):
            mock_emd.side_effect = requests.HTTPError("503 Service Unavailable")
            result = build_game_report_data(800, [_OI_FAIL_EVENT])
        assert result is not None
        assert result["moneyline"]["open_interest_status"] == "unavailable"
        assert result["moneyline"]["open_interest"] is None

    def test_other_games_unaffected_by_one_games_oi_failure(self):
        """Full-cycle-style check: game 800's OI failure must not affect game 100's assembly."""
        from reporter.watcher.market_data import OpenInterestError

        def flaky_extract(market):
            if market.get("conditionId") == "0xml8":
                raise OpenInterestError("empty /oi response")
            return {"prices": {"Away": 0.5, "Home": 0.5}, "open_interest": 42.0, "recent_trades": []}

        with patch("reporter.watcher.game_assembly.extract_market_data", side_effect=flaky_extract), \
             patch("reporter.watcher.game_assembly.fetch_recent_trades", return_value=[]):
            result_800 = build_game_report_data(800, [_OI_FAIL_EVENT])
            result_100 = build_game_report_data(100, [_ML_EVENT, _PROP_EVENT])

        assert result_800 is not None
        assert result_800["moneyline"]["open_interest_status"] == "unavailable"
        assert result_100 is not None
        assert result_100["moneyline"]["open_interest_status"] == "ok"
        assert result_100["moneyline"]["open_interest"] == 42.0
