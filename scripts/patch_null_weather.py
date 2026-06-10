"""
scripts/patch_null_weather.py

Standalone patch for outdoor games that have null weather in the
weather table.  This situation arises because ingest_weather.py uses
ON CONFLICT DO NOTHING, which silently skips rows that already exist —
so any game that was inserted with null weather (e.g. because the
initial run used an earlier end_date that excluded late-season games)
is never back-filled by a plain re-run.

Strategy:
  1. Query weather WHERE is_outdoor = true AND temperature_f IS NULL,
     joined to games + stadiums to recover coordinates and game times.
  2. Group the resulting games by (lat, lon, timezone) — same bucketing
     logic as ingest_weather.py — so one API call covers all null games
     at the same physical location.
  3. For each bucket, fetch the full date range from Open-Meteo, build
     the hourly index, look up each game's kickoff hour, and UPDATE the
     weather row in place.
  4. Print a final summary: games patched vs. games still null.

Fallback notice (if Open-Meteo is unreachable):
  WeatherAPI.com, MET Norway (api.met.no), NWS Historical Data (weather.gov/api)

Usage:
    python scripts/patch_null_weather.py
"""

import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

DATABASE_URL = (
    f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)

OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
API_DELAY_S   = 0.5   # polite pause between consecutive API calls
RETRY_DELAY_S = 5.0   # wait before one retry on failure

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
# Finds every outdoor game with null weather.  The CASE expression in the
# stadium join mirrors ingest_weather.py so pre-relocation abbreviations
# (OAK, SD, STL) resolve correctly to their current stadium rows.
_LOAD_NULL_SQL = text("""
    SELECT
        w.game_id,
        TO_CHAR(g.game_date, 'YYYY-MM-DD')  AS game_date,
        COALESCE(g.game_time, '')            AS game_time,
        g.home_team,
        s.latitude,
        s.longitude,
        s.timezone
    FROM weather w
    JOIN games g ON g.game_id = w.game_id
    LEFT JOIN stadiums s
           ON s.team = CASE g.home_team
                           WHEN 'OAK' THEN 'LV'
                           WHEN 'SD'  THEN 'LAC'
                           WHEN 'STL' THEN 'LA'
                           ELSE g.home_team
                       END
          AND g.season >= s.season_start
          AND (s.season_end IS NULL OR g.season <= s.season_end)
    WHERE w.is_outdoor = true
      AND w.temperature_f IS NULL
    ORDER BY g.game_date
""")

_UPDATE_SQL = text("""
    UPDATE weather
    SET temperature_f      = :temperature_f,
        wind_speed_mph     = :wind_speed_mph,
        wind_direction     = :wind_direction,
        precipitation_inch = :precipitation_inch
    WHERE game_id = :game_id
""")

_NULL_COUNT_SQL = text("""
    SELECT COUNT(*) FROM weather
    WHERE is_outdoor = true AND temperature_f IS NULL
""")

# ---------------------------------------------------------------------------
# Helpers  (identical to ingest_weather.py so behaviour is consistent)
# ---------------------------------------------------------------------------

def _safe_float(val) -> float | None:
    """Coerce API value to float, returning None for None / NaN."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def parse_kickoff_hour(game_time: str) -> int:
    """
    Parse game_time string to hour (0-23).  Handles:
      '20:20', '9:30', '1:00 PM', '13:00:00' and empty/None.
    Defaults to 13 (1 PM local) if unparseable.
    """
    if not game_time or not game_time.strip():
        return 13
    s = game_time.strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p"):
        try:
            return datetime.strptime(s, fmt).hour
        except ValueError:
            pass
    try:
        return int(s.split(":")[0])
    except Exception:
        return 13


def fetch_open_meteo(
    lat: float, lon: float,
    start_date: str, end_date: str,
    timezone: str,
) -> dict | None:
    """
    Call the Open-Meteo archive endpoint.  Returns parsed JSON dict or None.
    Retries once after RETRY_DELAY_S seconds before giving up.
    """
    params = {
        "latitude":           lat,
        "longitude":          lon,
        "start_date":         start_date,
        "end_date":           end_date,
        "hourly": (
            "temperature_2m,wind_speed_10m,"
            "wind_direction_10m,precipitation"
        ),
        "temperature_unit":   "fahrenheit",
        "wind_speed_unit":    "mph",
        "precipitation_unit": "inch",
        "timezone":           timezone,
    }
    for attempt in range(2):
        try:
            resp = requests.get(OPEN_METEO_URL, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if attempt == 0:
                print(f"    [retry] {exc}", flush=True)
                time.sleep(RETRY_DELAY_S)
            else:
                print(f"    [fail]  {exc}", flush=True)
    return None


def build_hourly_index(api_data: dict) -> dict:
    """
    Index Open-Meteo hourly payload by time string.
    Returns dict: "YYYY-MM-DDTHH:MM" → {temperature_f, wind_speed_mph,
                                         wind_direction, precipitation_inch}
    All values are Python float or None — no NaN.
    t.strip() guards against trailing whitespace in API responses.
    """
    h      = api_data.get("hourly", {})
    times  = h.get("time",                [])
    temps  = h.get("temperature_2m",      [])
    wspds  = h.get("wind_speed_10m",      [])
    wdirs  = h.get("wind_direction_10m",  [])
    precip = h.get("precipitation",       [])

    idx = {}
    for i, t in enumerate(times):
        idx[t.strip()] = {
            "temperature_f":      _safe_float(temps[i]  if i < len(temps)  else None),
            "wind_speed_mph":     _safe_float(wspds[i]  if i < len(wspds)  else None),
            "wind_direction":     _safe_float(wdirs[i]  if i < len(wdirs)  else None),
            "precipitation_inch": _safe_float(precip[i] if i < len(precip) else None),
        }
    return idx


def lookup_game_weather(idx: dict, game_date: str, game_time: str) -> dict:
    """
    Return weather dict for the given game kickoff.
    Tries the exact kickoff hour, then ±1, ±2, ±3 hours to handle minor
    data gaps in the historical record.
    Falls back to all-None if nothing found.
    str() + strip() on both inputs guards against invisible characters
    that would cause a silent key miss.
    """
    game_date = str(game_date).strip()
    game_time = str(game_time).strip()
    hour = parse_kickoff_hour(game_time)
    for delta in (0, -1, 1, -2, 2, -3, 3):
        key = f"{game_date}T{(hour + delta) % 24:02d}:00"
        if key in idx:
            return dict(idx[key])
    return {
        "temperature_f":      None,
        "wind_speed_mph":     None,
        "wind_direction":     None,
        "precipitation_inch": None,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def patch_null_weather() -> None:
    engine = create_engine(DATABASE_URL, future=True)

    print(f"\n{'=' * 62}")
    print("  patch_null_weather")
    print(f"{'=' * 62}\n")

    # ── Connectivity check ───────────────────────────────────────────
    print("Checking Open-Meteo API...", flush=True)
    _probe = fetch_open_meteo(
        39.0489, -94.4839,
        "2023-09-07", "2023-09-07",
        "America/Chicago",
    )
    if _probe is None:
        print(
            "\nERROR: Open-Meteo API is unreachable or returned an error.\n"
            "Fallback alternatives:\n"
            "  • WeatherAPI.com   (paid, good historical coverage)\n"
            "  • MET Norway       (api.met.no, free, global)\n"
            "  • NWS              (weather.gov/api, US-only, free)\n",
            file=sys.stderr,
        )
        sys.exit(1)
    print("  API reachable.\n", flush=True)

    # ── Load all null-weather outdoor rows ───────────────────────────
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(_LOAD_NULL_SQL)]

    if not rows:
        print("No null-weather outdoor rows found. Nothing to patch.\n")
        return

    # Warn about any rows missing stadium data after the join
    unmatched = [r for r in rows if r["latitude"] is None]
    if unmatched:
        for r in unmatched:
            print(
                f"  WARNING: no stadium match for {r['game_id']}"
                f" (home_team={r['home_team']}) — skipping.",
                flush=True,
            )
    patchable = [r for r in rows if r["latitude"] is not None]

    print(
        f"Null outdoor rows to patch : {len(rows):>5}"
        f"  ({len(unmatched)} unmatched stadiums skipped)\n",
        flush=True,
    )

    # ── Group by (lat, lon, timezone) ────────────────────────────────
    location_buckets: dict = defaultdict(list)
    for g in patchable:
        loc_key = (
            round(g["latitude"],  4),
            round(g["longitude"], 4),
            g["timezone"],
        )
        location_buckets[loc_key].append(g)

    total_locs = len(location_buckets)
    print(
        f"Grouped into {total_locs} unique location bucket(s).\n",
        flush=True,
    )

    # ── Fetch + update per location ──────────────────────────────────
    games_patched    = 0
    games_still_null = 0

    for loc_idx, ((lat, lon, tz), games) in enumerate(
        location_buckets.items(), start=1
    ):
        dates      = [g["game_date"] for g in games if g["game_date"]]
        start_date = min(dates)
        end_date   = max(dates)

        sample_team = games[0]["home_team"]
        print(
            f"[{loc_idx:>2}/{total_locs}]  {sample_team:<4}  "
            f"({lat}, {lon})  {start_date} → {end_date}  "
            f"({len(games)} games)",
            flush=True,
        )

        api_data = fetch_open_meteo(lat, lon, start_date, end_date, tz)
        time.sleep(API_DELAY_S)

        if api_data is None:
            print(
                f"    API failed — {len(games)} game(s) remain null.",
                flush=True,
            )
            games_still_null += len(games)
            continue

        hourly_idx  = build_hourly_index(api_data)
        update_rows = []

        for g in games:
            w = lookup_game_weather(hourly_idx, g["game_date"], g["game_time"])
            update_rows.append({
                "game_id":           g["game_id"],
                "temperature_f":     w["temperature_f"],
                "wind_speed_mph":    w["wind_speed_mph"],
                "wind_direction":    w["wind_direction"],
                "precipitation_inch": w["precipitation_inch"],
            })

            temp_s = (
                f"{w['temperature_f']:.1f}°F"
                if w["temperature_f"] is not None else "None"
            )
            wind_s = (
                f"{w['wind_speed_mph']:.1f} mph"
                if w["wind_speed_mph"] is not None else "None"
            )
            prec_s = (
                f"{w['precipitation_inch']:.3f}\""
                if w["precipitation_inch"] is not None else "None"
            )
            print(
                f"    {g['game_id']}  "
                f"temp={temp_s}  wind={wind_s}  precip={prec_s}",
                flush=True,
            )

        try:
            with engine.begin() as conn:
                conn.execute(_UPDATE_SQL, update_rows)
            # Count how many actually got data vs. stayed null (API returned
            # nothing for that kickoff hour even after ±3 h fallback)
            patched   = sum(1 for r in update_rows if r["temperature_f"] is not None)
            still_null = len(update_rows) - patched
            games_patched    += patched
            games_still_null += still_null
        except SQLAlchemyError as exc:
            print(f"    DB update error: {exc}", file=sys.stderr)
            games_still_null += len(update_rows)

    # ── Final summary ────────────────────────────────────────────────
    with engine.connect() as conn:
        remaining_null = conn.execute(_NULL_COUNT_SQL).scalar()

    print(f"\n{'=' * 62}")
    print("  PATCH COMPLETE")
    print(f"  {'─' * 40}")
    print(f"  Games patched (weather populated) : {games_patched:>5,}")
    print(f"  Games still null after patch      : {games_still_null:>5,}")
    print(f"  Null outdoor rows remaining in DB : {remaining_null:>5,}")
    print(f"{'=' * 62}\n")


if __name__ == "__main__":
    patch_null_weather()
