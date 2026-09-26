"""
Offline tests for the HTTP layer of reporter.watcher.market_data:
_get_with_retry() backoff (incl. 429-specific delay) and
fetch_open_interest() response handling.

requests.get and time.sleep are patched; no network, no real sleeping.
"""

import json
import os
from unittest.mock import patch

import pytest
import requests

os.environ.setdefault("VOLUME_THRESHOLD", "1000")
os.environ.setdefault("VOLUME_WINDOW", "24hr")
os.environ.setdefault("REPORT_OUTPUT_DIR", "/tmp/reporter_test")
os.environ.setdefault("POLLING_INTERVAL_SECONDS", "300")

from reporter.watcher import market_data  # noqa: E402
from reporter.watcher.market_data import (  # noqa: E402
    OpenInterestError,
    _get_with_retry,
    fetch_open_interest,
)

_CID = "0x" + "ab" * 32


def _resp(status: int, body=None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body if body is not None else {}).encode()
    r.url = "https://data-api.example/oi"
    return r


def _run(responses):
    """Call _get_with_retry against a fixed response sequence; return (result|exc, sleep calls)."""
    with patch.object(market_data.requests, "get", side_effect=responses) as mock_get, \
         patch.object(market_data.time, "sleep") as mock_sleep:
        try:
            result = _get_with_retry("https://data-api.example/oi", {})
        except requests.HTTPError as exc:
            result = exc
    return result, [c.args[0] for c in mock_sleep.call_args_list], mock_get.call_count


# ---------------------------------------------------------------------------
# _get_with_retry — backoff
# ---------------------------------------------------------------------------

class TestGetWithRetryBackoff:
    def test_rate_limit_delay_is_longer_than_generic(self):
        assert market_data._RATE_LIMIT_DELAY > max(market_data._RETRY_DELAYS)

    def test_429_uses_rate_limit_delay(self):
        result, sleeps, calls = _run([_resp(429), _resp(200)])
        assert result.status_code == 200
        assert sleeps == [market_data._RATE_LIMIT_DELAY]
        assert calls == 2

    def test_generic_non_200_uses_generic_delay(self):
        result, sleeps, _ = _run([_resp(500), _resp(200)])
        assert result.status_code == 200
        assert sleeps == [market_data._RETRY_DELAYS[0]]

    def test_mixed_failures_pick_delay_per_status(self):
        result, sleeps, _ = _run([_resp(503), _resp(429), _resp(200)])
        assert result.status_code == 200
        assert sleeps == [market_data._RETRY_DELAYS[0], market_data._RATE_LIMIT_DELAY]

    def test_persistent_429_raises_after_same_attempt_count(self):
        result, sleeps, calls = _run([_resp(429)] * 3)
        assert isinstance(result, requests.HTTPError)
        assert calls == 3
        assert sleeps == [market_data._RATE_LIMIT_DELAY] * 2

    def test_success_first_try_never_sleeps(self):
        result, sleeps, calls = _run([_resp(200)])
        assert result.status_code == 200
        assert sleeps == []
        assert calls == 1


# ---------------------------------------------------------------------------
# fetch_open_interest — response handling
# ---------------------------------------------------------------------------

def _fetch_oi(body):
    with patch.object(market_data.requests, "get", return_value=_resp(200, body)), \
         patch.object(market_data.time, "sleep"):
        return fetch_open_interest(_CID)


class TestFetchOpenInterest:
    def test_valid_record_returns_value(self):
        assert _fetch_oi([{"market": _CID, "value": 6735.24449}]) == 6735.24449

    def test_empty_list_raises_not_zero(self):
        # Unverified whether [] means "zero OI" or "not found" -> must not become 0.0.
        with pytest.raises(OpenInterestError, match="Unexpected /oi response shape"):
            _fetch_oi([])

    def test_non_list_raises(self):
        with pytest.raises(OpenInterestError, match="Unexpected /oi response shape"):
            _fetch_oi({"error": "not found"})

    def test_global_fallback_raises(self):
        with pytest.raises(OpenInterestError, match="GLOBAL"):
            _fetch_oi([{"market": "GLOBAL", "value": 336987405.13}])
