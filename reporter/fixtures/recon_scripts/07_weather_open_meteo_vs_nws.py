"""
Recon (read-only): Open-Meteo vs NWS hourly forecast for one NFL game, plus the
forecast hour our weather adapter actually selects.

Purpose: classify the 7-11 F report-vs-NWS temperature gap (CHI@GB, HOU@TEN,
LV@NE, NYG@WAS) as normal model variance vs an adapter bug (hour, timezone,
units). Phase 4 ongoing_fixes item 1c. This script fixes nothing and writes no
files; everything goes to stdout.

Run from the production host:
    python reporter/fixtures/recon_scripts/07_weather_open_meteo_vs_nws.py \
        --home GB --kickoff-utc 2026-10-11T17:00:00Z \
        [--report-file path/to/report.md] [--window-hours 3] [--game-date YYYY-MM-DD]

--game-date: the adapter is called with the schedule-driven game_date from the
game dict, which is not derivable from kickoff alone. Default: the date in the
--report-file name (YYYY-MM-DD_AWAY_at_HOME.md) if given, else the kickoff's
date in the stadium's local timezone.

Exit codes: 0 ok (or indoor), 1 a source failed, 2 kickoff beyond a forecast
horizon / not covered, 3 bad input.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Reused from the production adapter: stadium loader and the exact hour picker.
from reporter.enrich.weather_adapter import (  # noqa: E402
    _nearest_hour_index,
    get_stadium_info,
)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
NWS_POINTS_URL = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
USER_AGENT = "lunchpail-recon"
TIMEOUT = 10
MAX_RETRIES = 2
RETRY_WAIT = 5

EXIT_SOURCE_FAILED = 1
EXIT_HORIZON = 2
EXIT_BAD_INPUT = 3


class SourceFailed(Exception):
    pass


class HorizonExceeded(Exception):
    pass


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def parse_kickoff(value: str) -> datetime:
    """ISO 8601 UTC (trailing Z or offset) -> aware UTC datetime."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("kickoff must carry a timezone (e.g. trailing Z)")
    return dt.astimezone(timezone.utc)


def om_local_to_utc(local_str: str, tz: str) -> datetime:
    """Open-Meteo local string (e.g. '2026-10-11T13:00') in `tz` -> aware UTC."""
    naive = datetime.strptime(local_str, "%Y-%m-%dT%H:%M")
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def parse_nws_wind(text) -> float | None:
    """
    NWS windSpeed string -> mph. '10 mph' -> 10.0; '5 to 10 mph' -> 10.0 (the
    upper bound); '16 km/h' -> converted to mph. None if unparseable.
    """
    if not isinstance(text, str):
        return None
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not nums:
        return None
    val = max(nums)
    low = text.lower()
    if "km/h" in low or "kph" in low:
        return val * 0.621371
    if "kt" in low:
        return val * 1.15078
    if "mph" in low:
        return val
    return None


def nws_temp_to_f(value, unit) -> float | None:
    """NWS temperature + unit ('F' / 'C' / 'wmoUnit:degC' / 'wmoUnit:degF') -> F."""
    if value is None or unit is None:
        return None
    u = str(unit).upper()
    if u.endswith("F"):
        return float(value)
    if u.endswith("C"):
        return float(value) * 9.0 / 5.0 + 32.0
    return None


def shift_mad(om: dict, nws: dict, offset_hours: int):
    """
    Mean absolute temp difference when Open-Meteo is shifted by `offset_hours`:
    OM value at (t + offset) is paired with NWS value at t, for every NWS hour t
    where both exist. Returns (mad, n_pairs) or (None, 0) with no overlap.
    """
    delta = timedelta(hours=offset_hours)
    diffs = []
    for t, nv in nws.items():
        ov = om.get(t + delta)
        if ov is None or nv is None:
            continue
        diffs.append(abs(ov - nv))
    if not diffs:
        return None, 0
    return sum(diffs) / len(diffs), len(diffs)


def best_offset(results: dict):
    """results: offset -> (mad, n). Offset with the lowest non-None mad, or None."""
    valid = {k: v[0] for k, v in results.items() if v[0] is not None}
    if not valid:
        return None
    return min(valid, key=lambda k: (valid[k], abs(k)))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get(url, params=None, accept="application/json"):
    """GET with 10 s timeout, up to 2 retries (5 s wait) on network/429/5xx."""
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    last = "unknown"
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
            else:
                return resp
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_WAIT)
    raise SourceFailed(f"{last} after {MAX_RETRIES + 1} attempts")


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------

def om_params(lat, lon, tz, start, end):
    # COPIED VERBATIM from reporter/enrich/weather_adapter.fetch_forecast_day
    # (the adapter builds these inline and does not expose them); only the
    # start/end dates vary.
    return {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,wind_speed_10m,precipitation,precipitation_probability",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": tz,
        "start_date": start,
        "end_date": end,
    }


def fetch_om(params):
    """Return (hourly dict, hourly_units dict). Raises HorizonExceeded / SourceFailed."""
    resp = http_get(OPEN_METEO_URL, params=params)
    try:
        data = resp.json()
    except ValueError:
        raise SourceFailed(f"non-JSON response (HTTP {resp.status_code})")
    if resp.status_code == 400 and data.get("error") is True:
        reason = data.get("reason", "")
        if "out of allowed" in reason.lower():
            raise HorizonExceeded(reason)
        raise SourceFailed(f"HTTP 400: {reason}")
    if resp.status_code != 200:
        raise SourceFailed(f"HTTP {resp.status_code}: {str(data)[:200]}")
    hourly = data.get("hourly")
    if not isinstance(hourly, dict) or not hourly.get("time"):
        raise SourceFailed(f"unexpected shape (no hourly.time): {str(data)[:200]}")
    return hourly, data.get("hourly_units") or {}


def om_series(hourly, field, tz):
    out = {}
    for t, v in zip(hourly["time"], hourly.get(field) or []):
        if v is not None:
            out[om_local_to_utc(t, tz)] = float(v)
    return out


# ---------------------------------------------------------------------------
# NWS
# ---------------------------------------------------------------------------

def fetch_nws(lat, lon):
    """Return (forecastHourly URL, periods list, generatedAt). Raises SourceFailed."""
    pts_url = NWS_POINTS_URL.format(lat=lat, lon=lon)
    resp = http_get(pts_url, accept="application/geo+json")
    if resp.status_code != 200:
        raise SourceFailed(f"/points HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        hourly_url = resp.json()["properties"]["forecastHourly"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceFailed(f"/points missing properties.forecastHourly: {exc!r}")
    resp = http_get(hourly_url, accept="application/geo+json")
    if resp.status_code != 200:
        raise SourceFailed(f"forecastHourly HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        props = resp.json()["properties"]
        periods = props["periods"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceFailed(f"forecastHourly missing properties.periods: {exc!r}")
    if not periods:
        raise SourceFailed("forecastHourly returned zero periods")
    return hourly_url, periods, props.get("generatedAt")


def _qv(x):
    """NWS values are plain or {'value':..,'unitCode':..}; return (value, unit)."""
    if isinstance(x, dict):
        return x.get("value"), x.get("unitCode")
    return x, None


def nws_series(periods):
    """-> (temp F, wind mph, precip prob %) dicts keyed by aware UTC hour, plus unit set."""
    temp, wind, pop = {}, {}, {}
    units = set()
    for p in periods:
        start = datetime.fromisoformat(p["startTime"])
        if start.tzinfo is None:
            raise SourceFailed(f"NWS startTime has no offset: {p['startTime']!r}")
        t = start.astimezone(timezone.utc)
        tv, tu = _qv(p.get("temperature"))
        tu = p.get("temperatureUnit") or tu
        units.add(f"temp:{tu}")
        f = nws_temp_to_f(tv, tu)
        if f is not None:
            temp[t] = f
        w = parse_nws_wind(p.get("windSpeed"))
        if w is not None:
            wind[t] = w
        units.add("wind:" + re.sub(r"[\d.\s]+(to)?", "", str(p.get("windSpeed"))).strip())
        pv, _ = _qv(p.get("probabilityOfPrecipitation"))
        if pv is not None:
            pop[t] = float(pv)
    return temp, wind, pop, units


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def fmt(v, nd=0):
    return "--" if v is None else f"{v:.{nd}f}"


def weather_section(report_path):
    text = Path(report_path).read_text(encoding="utf-8")
    m = re.search(r"^## Weather\b.*?(?=^## |\Z)", text, re.S | re.M)
    return m.group(0).rstrip() if m else None


def utc_str(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only Open-Meteo vs NWS hourly comparison for one NFL game.")
    ap.add_argument("--home", required=True, help="home team abbreviation, e.g. GB")
    ap.add_argument("--kickoff-utc", required=True, help="e.g. 2026-10-11T17:00:00Z")
    ap.add_argument("--report-file", help="generated report .md to print the Weather section from")
    ap.add_argument("--window-hours", type=int, default=3)
    ap.add_argument("--game-date", help="YYYY-MM-DD passed to the adapter (see module docstring)")
    args = ap.parse_args(argv)

    try:
        kickoff = parse_kickoff(args.kickoff_utc)
    except ValueError as exc:
        print(f"ERROR: bad --kickoff-utc: {exc}")
        return EXIT_BAD_INPUT
    kickoff_str = kickoff.strftime("%Y-%m-%dT%H:%M:%SZ")  # format the adapter parses

    try:
        stadium = get_stadium_info(args.home)
    except Exception as exc:
        print(f"ERROR: stadium loader failed: {exc}")
        return EXIT_BAD_INPUT
    if stadium is None:
        print(f"ERROR: no stadium for team {args.home!r} in stadiums.csv")
        return EXIT_BAD_INPUT
    if not stadium.is_outdoor:
        print(f"{stadium.team_abbr} @ {stadium.stadium_name}: indoor stadium "
              "(is_outdoor=false). Nothing to compare; no fetch performed.")
        return 0

    tz = stadium.timezone
    zi = ZoneInfo(tz)
    k_local = kickoff.astimezone(zi)
    k_hour = kickoff.replace(minute=0, second=0, microsecond=0)
    w = args.window_hours

    game_date = args.game_date
    if not game_date and args.report_file:
        m = re.match(r"(\d{4}-\d{2}-\d{2})_", Path(args.report_file).name)
        game_date = m.group(1) if m else None
    if not game_date:
        game_date = k_local.strftime("%Y-%m-%d")

    # Adapter-exact request (one day), and a wide request for table/shift test.
    ad_params = om_params(stadium.latitude, stadium.longitude, tz, game_date, game_date)
    d = datetime.strptime(game_date, "%Y-%m-%d").date()
    wide_params = om_params(stadium.latitude, stadium.longitude, tz,
                            (d - timedelta(days=1)).isoformat(),
                            (d + timedelta(days=1)).isoformat())
    nws_points = NWS_POINTS_URL.format(lat=stadium.latitude, lon=stadium.longitude)

    print("=" * 78)
    print("1. HEADER")
    print("=" * 78)
    print(f"Home team       : {stadium.team_abbr}  ({stadium.stadium_name})")
    print(f"Coordinates     : lat={stadium.latitude} lon={stadium.longitude}")
    print(f"Timezone        : {tz}")
    print(f"Kickoff UTC     : {utc_str(kickoff)}")
    print(f"Kickoff local   : {k_local.strftime('%Y-%m-%d %H:%M %Z (UTC%z)')}")
    print(f"Adapter game_date: {game_date}")
    print(f"Run at          : {utc_str(datetime.now(timezone.utc))}")
    print()
    print("Open-Meteo (adapter-identical, single day) params:")
    for k, v in ad_params.items():
        print(f"    {k} = {v}")
    print(f"  URL: {OPEN_METEO_URL}")
    print("Open-Meteo (wide, for table + shift test) start_date/end_date: "
          f"{wide_params['start_date']} .. {wide_params['end_date']}  (other params identical)")
    print("  Units requested: temperature_unit=fahrenheit, wind_speed_unit=mph (no conversion needed)")
    print(f"NWS points URL  : {nws_points}")
    print("NWS hourly URL  : taken from the points response (printed below)")
    print(f"User-Agent      : {USER_AGENT}")
    print()

    failed = False
    horizon = False

    # --- Open-Meteo, adapter-exact ---
    ad_hourly = None
    try:
        ad_hourly, ad_units = fetch_om(ad_params)
        print(f"Open-Meteo hourly_units: {ad_units}")
        print(f"Open-Meteo adapter-day returned {len(ad_hourly['time'])} hours, "
              f"first={ad_hourly['time'][0]} last={ad_hourly['time'][-1]} (local, tz={tz})")
    except HorizonExceeded as exc:
        horizon = True
        print(f"Open-Meteo: kickoff beyond forecast horizon: {exc}")
    except SourceFailed as exc:
        failed = True
        print(f"FAILED Open-Meteo (adapter-day request): {exc}")

    # --- Open-Meteo, wide ---
    om_t = om_w = om_p = {}
    try:
        wide, _ = fetch_om(wide_params)
        om_t = om_series(wide, "temperature_2m", tz)
        om_w = om_series(wide, "wind_speed_10m", tz)
        om_p = om_series(wide, "precipitation_probability", tz)
    except HorizonExceeded as exc:
        horizon = True
        print(f"Open-Meteo (wide): beyond forecast horizon: {exc}")
    except SourceFailed as exc:
        failed = True
        print(f"FAILED Open-Meteo (wide request): {exc}")

    # --- NWS ---
    nw_t = nw_w = nw_p = {}
    try:
        hourly_url, periods, generated = fetch_nws(stadium.latitude, stadium.longitude)
        nw_t, nw_w, nw_p, nws_units = nws_series(periods)
        print(f"NWS hourly URL  : {hourly_url}")
        print(f"NWS generatedAt : {generated}")
        print(f"NWS units seen  : {sorted(nws_units)} (temps converted to F; wind ranges use upper bound)")
        print(f"NWS returned {len(periods)} periods, first={periods[0]['startTime']} "
              f"last={periods[-1]['startTime']}")
        if k_hour not in nw_t:
            horizon = True
            print("NWS: kickoff hour is not covered by the forecast (beyond horizon or already past).")
    except SourceFailed as exc:
        failed = True
        print(f"FAILED NWS: {exc}")
    print()

    if ad_hourly is not None and k_hour not in om_t and om_t:
        horizon = True
        print("Open-Meteo: kickoff hour not present in wide series.")

    # --- 2. Adapter's chosen hour ---
    print("=" * 78)
    print("2. ADAPTER-SELECTED HOUR")
    print("=" * 78)
    adapter_hour_utc = None
    if ad_hourly is None:
        print("n/a (adapter-day Open-Meteo request did not succeed)")
    else:
        # Same function the adapter calls, same inputs it would get.
        idx = _nearest_hour_index(ad_hourly["time"], kickoff_str, tz)
        sel = ad_hourly["time"][idx]
        adapter_hour_utc = om_local_to_utc(sel, tz)
        print(f"_nearest_hour_index -> index {idx}, Open-Meteo local string {sel!r}")
        print(f"Adapter hour    : {utc_str(adapter_hour_utc)}  |  local "
              f"{adapter_hour_utc.astimezone(zi).strftime('%Y-%m-%d %H:%M %Z')}")

        def at(field):
            vals = ad_hourly.get(field) or []
            return float(vals[idx]) if idx < len(vals) and vals[idx] is not None else None
        print("Values the adapter would report:")
        print(f"    temperature_f                 = {fmt(at('temperature_2m'), 1)}")
        print(f"    wind_speed_mph                = {fmt(at('wind_speed_10m'), 1)}")
        print(f"    precipitation_in              = {fmt(at('precipitation'), 2)}")
        print(f"    precipitation_probability_pct = {fmt(at('precipitation_probability'), 0)}")
    print()

    # --- 3. Report section ---
    if args.report_file:
        print("=" * 78)
        print(f"3. REPORT WEATHER SECTION ({args.report_file})")
        print("=" * 78)
        try:
            sec = weather_section(args.report_file)
            print(sec if sec else "(no '## Weather' section found in file)")
        except OSError as exc:
            print(f"FAILED reading report file: {exc}")
        print()

    # --- 4. Table ---
    print("=" * 78)
    print(f"4. HOURLY TABLE (kickoff -{w}h .. +{w}h; times UTC, local alongside)")
    print("=" * 78)
    print("Markers: K = kickoff hour, A = adapter-selected hour. delta = Open-Meteo - NWS.")
    hdr = (f"{'hour (UTC)':<17} {'local':<11} {'mk':<3} {'OM F':>6} {'NWS F':>6} {'delta':>6} "
           f"{'OM mph':>7} {'NWS mph':>8} {'OM pop%':>8} {'NWS pop%':>9}")
    print(hdr)
    print("-" * len(hdr))
    for off in range(-w, w + 1):
        t = k_hour + timedelta(hours=off)
        mk = ("K" if t == k_hour else "") + ("A" if t == adapter_hour_utc else "")
        ot, nt = om_t.get(t), nw_t.get(t)
        dl = None if ot is None or nt is None else ot - nt
        print(f"{t.strftime('%Y-%m-%d %H:%M'):<17} {t.astimezone(zi).strftime('%m-%d %H:%M'):<11} "
              f"{mk:<3} {fmt(ot,1):>6} {fmt(nt,1):>6} {fmt(dl,1):>6} "
              f"{fmt(om_w.get(t),1):>7} {fmt(nw_w.get(t),1):>8} "
              f"{fmt(om_p.get(t)):>8} {fmt(nw_p.get(t)):>9}")
    print()

    # --- 5. Shift test ---
    print("=" * 78)
    print("5. SHIFT TEST (mean abs temp diff F; OM(t+offset) vs NWS(t), over all overlapping hours)")
    print("=" * 78)
    results = {}
    for off in range(-3, 4):
        results[off] = shift_mad(om_t, nw_t, off)
        mad, n = results[off]
        print(f"offset {off:+d}h : MAD = {fmt(mad, 2):>6} F   (n={n})")
    best = best_offset(results) if om_t and nw_t else None
    print(f"Lowest MAD at offset: {'n/a' if best is None else f'{best:+d}h'}")
    print()

    # --- 6. Final line ---
    kd = None if k_hour not in om_t or k_hour not in nw_t else om_t[k_hour] - nw_t[k_hour]
    if adapter_hour_utc is None:
        same = "n/a"
    else:
        same = "yes" if adapter_hour_utc == k_hour else "no"
    print(f"FACTS: adapter_hour_equals_kickoff_hour={same}; "
          f"best_shift_offset={'n/a' if best is None else f'{best:+d}h'}; "
          f"delta_at_kickoff_hour_F(OM-NWS)={fmt(kd, 1)}")

    if horizon:
        return EXIT_HORIZON
    if failed:
        return EXIT_SOURCE_FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main())
