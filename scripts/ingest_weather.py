"""
scripts/ingest_weather.py

Pulls historical game-time weather from the Open-Meteo Historical Weather API
(free, no auth) and populates the weather table.

Strategy:
  1. Insert null-weather rows for every indoor/retractable game immediately —
     is_outdoor=False, all weather fields NULL.
  2. Group outdoor games by unique (lat, lon, timezone) so that all games at
     the same physical location are covered by a single API call.  NYG and NYJ
     share MetLife coordinates → one call.  Old-abbrev teams (OAK→LV Oakland
     Coliseum, SD→LAC Qualcomm, STL→LA Edward Jones Dome) are matched via a
     CASE expression in the stadium join.
  3. For each location, call Open-Meteo once with start_date = earliest game
     date, end_date = latest game date at that location.  Extract the kickoff
     hour's values for each game.
  4. On API failure: retry once after 5 s, then insert a null-weather row with
     is_outdoor=True so failed fetches are distinguishable from indoor games.

Fallback notice (if Open-Meteo is unreachable):
  WeatherAPI.com, MET Norway (api.met.no), NWS Historical Data (weather.gov/api)

Usage:
    python scripts/ingest_weather.py
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
API_DELAY_S    = 0.5   # polite pause between consecutive API calls
RETRY_DELAY_S  = 5.0   # wait before one retry on failure

# Pre-relocation team abbreviations used in the games table that differ from
# the nflverse-standard abbreviations used in the stadiums table.
# This mapping is applied in the stadium JOIN so historical games find their
# correct stadium row.
TEAM_ALIASES = {
    "OAK": "LV",   # Raiders Oakland era  (through 2019) → LV in stadiums
    "SD":  "LAC",  # Chargers San Diego era (through 2016) → LAC in stadiums
    "STL": "LA",   # Rams St. Louis era   (through 2015) → LA  in stadiums
}

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
# Alias-aware stadium join: the CASE expression maps old abbreviations to
# their current counterpart so season-range matching finds the correct row.
_LOAD_GAMES_SQL = text("""
    SELECT
        g.game_id,
        TO_CHAR(g.game_date, 'YYYY-MM-DD')  AS game_date,
        COALESCE(g.game_time, '')           AS game_time,
        g.season,
        g.home_team,
        s.stadium_id,
        s.latitude,
        s.longitude,
        s.timezone,
        s.is_outdoor
    FROM games g
    LEFT JOIN stadiums s
           ON s.team = CASE g.home_team
                           WHEN 'OAK' THEN 'LV'
                           WHEN 'SD'  THEN 'LAC'
                           WHEN 'STL' THEN 'LA'
                           ELSE g.home_team
                       END
          AND g.season >= s.season_start
          AND (s.season_end IS NULL OR g.season <= s.season_end)
    ORDER BY g.game_date
""")

_INSERT_SQL = text("""
    INSERT INTO weather (
        game_id, stadium_id,
        temperature_f, wind_speed_mph, wind_direction,
        precipitation_inch, weather_condition, is_outdoor
    ) VALUES (
        :game_id, :stadium_id,
        :temperature_f, :wind_speed_mph, :wind_direction,
        :precipitation_inch, :weather_condition, :is_outdoor
    )
    ON CONFLICT (game_id) DO NOTHING
""")

_UPDATE_SQL = text("""
    UPDATE weather
    SET temperature_f      = :temp,
        wind_speed_mph     = :wind,
        wind_direction     = :wdir,
        precipitation_inch = :precip
    WHERE game_id          = :game_id
      AND temperature_f    IS NULL
      AND is_outdoor        = true
""")

_COUNT_SQL = text("SELECT COUNT(*) FROM weather")

# ---------------------------------------------------------------------------
# Helpers
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
    # Last resort: leading integer
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
            "temperature_f":    _safe_float(temps[i]  if i < len(temps)  else None),
            "wind_speed_mph":   _safe_float(wspds[i]  if i < len(wspds)  else None),
            "wind_direction":   _safe_float(wdirs[i]  if i < len(wdirs)  else None),
            "precipitation_inch": _safe_float(precip[i] if i < len(precip) else None),
        }
    return idx


def lookup_game_weather(idx: dict, game_date: str, game_time: str) -> dict:
    """
    Return weather dict for the given game kickoff.
    Tries the exact kickoff hour, then ±1, ±2, ±3 hours to handle minor
    data gaps in the historical record.
    Falls back to all-None if nothing found.
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


def _null_weather_row(game: dict, is_outdoor: bool) -> dict:
    return {
        "game_id":           game["game_id"],
        "stadium_id":        game["stadium_id"],
        "temperature_f":     None,
        "wind_speed_mph":    None,
        "wind_direction":    None,
        "precipitation_inch": None,
        "weather_condition": None,
        "is_outdoor":        is_outdoor,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def ingest_weather() -> None:
    engine = create_engine(DATABASE_URL, future=True)

    print(f"\n{'=' * 62}")
    print("  ingest_weather  |  2014\u20132024")
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

    # ── Load all games with stadium info ─────────────────────────────
    indoor_games, outdoor_games = [], []
    unmatched_count = 0

    with engine.connect() as conn:
        for row in conn.execute(_LOAD_GAMES_SQL):
            g = dict(row._mapping)

            if g["is_outdoor"] is None:
                # No stadium match found even after alias resolution.
                # Treat as indoor (safe default) and log.
                unmatched_count += 1
                print(
                    f"  WARNING: no stadium match for {g['game_id']}"
                    f" (home_team={g['home_team']}, season={g['season']})",
                    flush=True,
                )
                g["is_outdoor"] = False

            if g["game_date"] is None:
                # Future/unknown date — can't call API; treat as indoor
                g["is_outdoor"] = False

            if g["is_outdoor"]:
                outdoor_games.append(g)
            else:
                indoor_games.append(g)

    if unmatched_count:
        print(f"  {unmatched_count} unmatched stadium rows treated as indoor.\n")

    print(
        f"Games: {len(indoor_games)} indoor/retractable, "
        f"{len(outdoor_games)} outdoor\n",
        flush=True,
    )

    # ── Step 1: Indoor / retractable — bulk insert null rows ─────────
    print(
        f"Inserting {len(indoor_games)} indoor/retractable rows "
        f"(is_outdoor=False, weather=NULL)...",
        flush=True,
    )
    indoor_rows = [_null_weather_row(g, is_outdoor=False) for g in indoor_games]

    try:
        with engine.begin() as conn:
            before = conn.execute(_COUNT_SQL).scalar()
            if indoor_rows:
                conn.execute(_INSERT_SQL, indoor_rows)
            after = conn.execute(_COUNT_SQL).scalar()
        indoor_inserted = after - before
    except SQLAlchemyError as exc:
        print(f"ERROR inserting indoor rows: {exc}", file=sys.stderr)
        indoor_inserted = 0

    print(f"  {indoor_inserted} indoor rows inserted.\n", flush=True)

    # ── Step 2: Group outdoor games by (lat, lon, timezone) ──────────
    # Grouping by unique physical location lets us cover all games at
    # that location (across all seasons) with a single API call.
    location_buckets: dict = defaultdict(list)
    for g in outdoor_games:
        loc_key = (
            round(g["latitude"],  4),
            round(g["longitude"], 4),
            g["timezone"],
        )
        location_buckets[loc_key].append(g)

    total_locs = len(location_buckets)
    print(
        f"Outdoor games: {len(outdoor_games)} across "
        f"{total_locs} unique locations.\n",
        flush=True,
    )

    # ── Step 3: Fetch + insert per location ──────────────────────────
    outdoor_with_weather = 0
    outdoor_null_weather  = 0

    for loc_idx, ((lat, lon, tz), games) in enumerate(
        location_buckets.items(), start=1
    ):
        # Date range for all games at this location
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

        # API call
        api_data = fetch_open_meteo(lat, lon, start_date, end_date, tz)
        time.sleep(API_DELAY_S)

        if api_data is None:
            # Insert null rows — is_outdoor=True flags these as failed fetches
            print(
                f"    API failed — {len(games)} games will have "
                f"null weather (is_outdoor=True).",
                flush=True,
            )
            null_rows = [_null_weather_row(g, is_outdoor=True) for g in games]
            try:
                with engine.begin() as conn:
                    conn.execute(_INSERT_SQL, null_rows)
            except SQLAlchemyError as exc:
                print(f"    DB insert error: {exc}", file=sys.stderr)
            outdoor_null_weather += len(games)
            continue

        # Build lookup and extract per-game weather
        hourly_idx  = build_hourly_index(api_data)

        # [debug] one-time key-presence check for first game at this location
        _dg = games[0]
        _dg_date = str(_dg["game_date"]).strip()
        _dg_time = str(_dg["game_time"]).strip()
        _dg_hour = parse_kickoff_hour(_dg_time)
        _dg_key  = f"{_dg_date}T{_dg_hour:02d}:00"
        print(
            f"    [debug] {_dg['game_id']} key='{_dg_key}' found={_dg_key in hourly_idx}",
            flush=True,
        )

        weather_rows = []

        for g in games:
            w = lookup_game_weather(hourly_idx, g["game_date"], g["game_time"])
            row = {
                "game_id":           g["game_id"],
                "stadium_id":        g["stadium_id"],
                "temperature_f":     w["temperature_f"],
                "wind_speed_mph":    w["wind_speed_mph"],
                "wind_direction":    w["wind_direction"],
                "precipitation_inch": w["precipitation_inch"],
                "weather_condition": None,   # WMO codes not requested
                "is_outdoor":        True,
            }
            weather_rows.append(row)

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

        update_params = [
            {
                "temp":    r["temperature_f"],
                "wind":    r["wind_speed_mph"],
                "wdir":    r["wind_direction"],
                "precip":  r["precipitation_inch"],
                "game_id": r["game_id"],
            }
            for r in weather_rows
        ]
        try:
            with engine.begin() as conn:
                conn.execute(_INSERT_SQL, weather_rows)
                conn.execute(_UPDATE_SQL, update_params)
            outdoor_with_weather += len(weather_rows)
        except SQLAlchemyError as exc:
            print(f"    DB insert error: {exc}", file=sys.stderr)
            outdoor_null_weather += len(weather_rows)

    # ── Summary ──────────────────────────────────────────────────────
    with engine.connect() as conn:
        total_in_db = conn.execute(_COUNT_SQL).scalar()

    print(f"\n{'=' * 62}")
    print("  WEATHER INGEST COMPLETE")
    print(f"  {'─' * 40}")
    print(f"  Indoor / retractable rows inserted : {indoor_inserted:>6,}")
    print(f"  Outdoor rows with weather fetched  : {outdoor_with_weather:>6,}")
    print(f"  Outdoor rows with null weather     : {outdoor_null_weather:>6,}")
    print(f"  Total rows now in weather table    : {total_in_db:>6,}")
    print(f"{'=' * 62}\n")


if __name__ == "__main__":
    ingest_weather()
