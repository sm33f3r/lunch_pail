"""Offline tests for the pure helpers in recon script 07 (canned data, no network)."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PATH = (Path(__file__).resolve().parents[1]
         / "fixtures" / "recon_scripts" / "07_weather_open_meteo_vs_nws.py")
_spec = importlib.util.spec_from_file_location("recon07", _PATH)
recon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recon)


def test_om_local_to_utc_central_daylight():
    got = recon.om_local_to_utc("2026-10-11T12:00", "America/Chicago")
    assert got == datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc)


def test_om_local_to_utc_phoenix_no_dst():
    got = recon.om_local_to_utc("2026-10-11T10:00", "America/Phoenix")
    assert got == datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc)


def test_parse_nws_wind():
    assert recon.parse_nws_wind("10 mph") == 10.0
    assert recon.parse_nws_wind("5 to 15 mph") == 15.0
    assert abs(recon.parse_nws_wind("16 km/h") - 9.94) < 0.01
    assert recon.parse_nws_wind("calm") is None
    assert recon.parse_nws_wind(None) is None


def test_nws_temp_to_f():
    assert recon.nws_temp_to_f(50, "F") == 50.0
    assert recon.nws_temp_to_f(10, "C") == 50.0
    assert recon.nws_temp_to_f(10, "wmoUnit:degC") == 50.0
    assert recon.nws_temp_to_f(50, None) is None


def test_shift_mad_and_best_offset():
    base = datetime(2026, 10, 11, 0, tzinfo=timezone.utc)
    hours = [base + timedelta(hours=i) for i in range(10)]
    nws = {t: float(i) for i, t in enumerate(hours)}
    # OM lags NWS by 2h: OM(t) == NWS(t-2) -> OM(t+2) == NWS(t) at offset +2.
    om = {t: float(i - 2) for i, t in enumerate(hours)}
    mad, n = recon.shift_mad(om, nws, 2)
    assert mad == 0.0 and n == 8
    results = {k: recon.shift_mad(om, nws, k) for k in range(-3, 4)}
    assert recon.best_offset(results) == 2
    assert recon.shift_mad({}, nws, 0) == (None, 0)
