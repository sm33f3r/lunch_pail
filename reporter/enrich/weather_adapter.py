"""
reporter/enrich/weather_adapter.py

Fetches game-time weather (temperature, wind speed, precipitation) for
outdoor-stadium NFL games from Open-Meteo.

Entry point: get_game_weather(team_abbr, game_date, kickoff_utc=None) -> WeatherResult

Stadium lookup (reporter/enrich/stadiums.csv -- a committed copy of the v1
seed at data/stadiums.csv, copied here because the Dockerfile only COPYs
reporter/ into the production image):
  - Keyed on the HOME team's abbreviation (weather is looked up for the
    venue, not either team's home city in general).
  - A team can have multiple historical rows (stadium moves/renames); the
    "current" row is the one with an empty season_end.
  - is_outdoor is the authoritative field for whether weather applies --
    NOT roof_type, which has a known inconsistency between the Rams (LA)
    and Chargers (LAC) rows for the shared SoFi Stadium (dome vs.
    retractable) even though both agree is_outdoor=false. See
    WEATHER_RECON.md.

Forecast source (confirmed live, see WEATHER_RECON.md):
  - https://api.open-meteo.com/v1/forecast, no auth required.
  - hourly=temperature_2m,wind_speed_10m,precipitation,precipitation_probability
    with US units (fahrenheit/mph/inch) and the stadium's own IANA timezone
    so returned hourly timestamps are already in local time.
  - start_date=end_date=<game_date> (YYYY-MM-DD) selects the one target day.
  - Hard ~14-day forecast horizon, confirmed live: a date within the horizon
    returns real hourly data (HTTP 200); a date beyond it returns an
    explicit HTTP 400 ({"error": true, "reason": "...out of allowed
    range..."}), never empty data and never a climatological substitute.
    This adapter branches on the ACTUAL response (200 vs. the "out of
    allowed range" 400), not on a hardcoded day-count comparison, since
    that is what the live API actually told us and is robust to Open-Meteo
    changing the exact window. _FORECAST_HORIZON_DAYS is used only to
    compute the human-readable "N more day(s)" estimate in the
    "forecast_not_yet_available" result, not to decide which branch to take.

Status states (mirrors injury_adapter.InjuryResult / team_stats_adapter.
TeamStatsResult's pattern):
  "indoor"                      -- stadium is not outdoor; no fetch attempted.
                                    This is a correct no-op, not a failure.
  "ok"                          -- fetch succeeded, within forecast horizon;
                                    real temperature/wind/precipitation values.
  "forecast_not_yet_available"  -- the game is outdoors and real, but beyond
                                    Open-Meteo's forecast horizon. A known,
                                    expected, honest state -- not a failure.
  "unavailable"                 -- stadium not found in the seed, network
                                    failure, or an unexpected response shape.
                                    Never a silently guessed default.
"""

from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import requests

_STADIUMS_CSV = Path(__file__).resolve().parent / "stadiums.csv"

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_OPEN_METEO_TIMEOUT = 10

# Confirmed live 2026-10-02 (WEATHER_RECON.md #3): 14 days out from the query
# date returns real data; 15 days out returns the "out of allowed range" 400.
# Used only for the human-readable "available in N day(s)" estimate below --
# see module docstring for why the actual branch is driven by the live
# response, not this constant.
_FORECAST_HORIZON_DAYS = 14

_DEFAULT_CACHE_TTL_SECONDS = 900


def _cache_ttl_seconds() -> float:
    """Read fresh each call so tests can monkeypatch the env between calls."""
    raw = os.environ.get("WEATHER_CACHE_TTL_SECONDS")
    if not raw:
        return float(_DEFAULT_CACHE_TTL_SECONDS)
    try:
        return float(raw)
    except ValueError:
        return float(_DEFAULT_CACHE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StadiumInfo:
    """One team's current stadium, as loaded from stadiums.csv."""
    team_abbr:     str
    stadium_name:  str
    latitude:      float
    longitude:     float
    timezone:      str
    is_outdoor:    bool


@dataclass(frozen=True)
class WeatherResult:
    """Container returned by get_game_weather()."""
    team_abbr:                      str
    status:                         str    # "indoor" | "ok" | "forecast_not_yet_available" | "unavailable"
    stadium_name:                   str | None = None
    temperature_f:                  float | None = None
    wind_speed_mph:                 float | None = None
    precipitation_in:               float | None = None
    precipitation_probability_pct:  float | None = None
    days_out:                       int | None = None   # set for forecast_not_yet_available
    days_until_available:           int | None = None   # set for forecast_not_yet_available, when computable
    error:                          str | None = None   # set for unavailable

    @property
    def is_unavailable(self) -> bool:
        return self.status == "unavailable"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class WeatherFetchError(Exception):
    """Raised when the Open-Meteo forecast fetch fails (network, bad response shape)."""


class ForecastHorizonError(Exception):
    """
    Raised when Open-Meteo reports the requested date is out of its forecast
    range (the live "out of allowed range" 400 response) -- distinct from
    WeatherFetchError so callers can tell "known, expected limitation" apart
    from "something actually went wrong."
    """


# ---------------------------------------------------------------------------
# Stadium seed loading
# ---------------------------------------------------------------------------

_STADIUM_CACHE: dict[str, dict[str, StadiumInfo]] = {}


def _reset_stadium_cache() -> None:
    """Test helper -- clears the in-process parsed-seed cache."""
    _STADIUM_CACHE.clear()


def _load_stadiums(csv_path: Path = _STADIUMS_CSV) -> dict[str, StadiumInfo]:
    """
    Parse stadiums.csv into a dict of team_abbr -> StadiumInfo for each
    team's CURRENT stadium (the row with an empty season_end).

    Cached in-process per csv_path (the seed is static data; there's no TTL
    concern, just avoiding re-parsing the file on every lookup within a
    cycle).

    Raises:
        WeatherFetchError: if the CSV file is missing or malformed.
    """
    cache_key = str(csv_path)
    cached = _STADIUM_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if not csv_path.exists():
        raise WeatherFetchError(f"Stadium seed not found at {csv_path}")

    stadiums: dict[str, StadiumInfo] = {}
    try:
        with csv_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                if (row.get("season_end") or "").strip():
                    continue  # a former venue, not this team's current one
                team = (row.get("team") or "").strip().upper()
                if not team:
                    continue
                stadiums[team] = StadiumInfo(
                    team_abbr=team,
                    stadium_name=(row.get("stadium_name") or "").strip(),
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    timezone=(row.get("timezone") or "UTC").strip(),
                    is_outdoor=(row.get("is_outdoor") or "").strip().lower() == "true",
                )
    except (OSError, ValueError, KeyError) as exc:
        raise WeatherFetchError(f"Failed to parse stadium seed {csv_path}: {exc}") from exc

    _STADIUM_CACHE[cache_key] = stadiums
    return stadiums


def get_stadium_info(team_abbr: str, csv_path: Path = _STADIUMS_CSV) -> StadiumInfo | None:
    """Look up one team's current stadium. Returns None if not in the seed."""
    stadiums = _load_stadiums(csv_path)
    return stadiums.get(team_abbr.upper())


# ---------------------------------------------------------------------------
# Cached forecast fetch -- one entry per (lat, lon, date)
# ---------------------------------------------------------------------------

_FORECAST_CACHE: dict[tuple, dict] = {}


def _reset_forecast_cache() -> None:
    """Test helper -- clears the in-process forecast response cache."""
    _FORECAST_CACHE.clear()


def fetch_forecast_day(
    latitude: float,
    longitude: float,
    game_date: str,
    tz: str,
) -> dict:
    """
    Fetch (or serve from cache) Open-Meteo's hourly forecast for one
    stadium/date, keyed on (lat, lon, date, tz) so repeated lookups for the
    same game across polling cycles -- or multiple games sharing a stadium
    and date -- within the TTL window don't each trigger a fresh request.

    Returns:
        The parsed "hourly" dict ({"time": [...], "temperature_2m": [...], ...})
        on success.

    Raises:
        ForecastHorizonError: when Open-Meteo's own response says the date is
            out of its forecast range (the confirmed live 400 shape).
        WeatherFetchError: on network failure, non-200/400 status, or an
            unexpected response shape.
    """
    key = (round(latitude, 4), round(longitude, 4), game_date, tz)
    entry = _FORECAST_CACHE.get(key)
    if entry is not None and (time.monotonic() - entry["fetched_at"]) < _cache_ttl_seconds():
        if entry["error"] is not None:
            raise entry["error"]
        return entry["hourly"]

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "temperature_2m,wind_speed_10m,precipitation,precipitation_probability",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": tz,
        "start_date": game_date,
        "end_date": game_date,
    }

    try:
        resp = requests.get(_OPEN_METEO_URL, params=params, timeout=_OPEN_METEO_TIMEOUT)
    except requests.RequestException as exc:
        err = WeatherFetchError(f"Open-Meteo request failed: {exc}")
        _FORECAST_CACHE[key] = {"hourly": None, "error": err, "fetched_at": time.monotonic()}
        raise err from exc

    try:
        data = resp.json()
    except ValueError as exc:
        err = WeatherFetchError(f"Open-Meteo non-JSON response (HTTP {resp.status_code}): {exc}")
        _FORECAST_CACHE[key] = {"hourly": None, "error": err, "fetched_at": time.monotonic()}
        raise err from exc

    if resp.status_code == 400 and data.get("error") is True:
        reason = data.get("reason", "out of allowed range")
        if "out of allowed range" in reason or "out of allowed" in reason.lower():
            err = ForecastHorizonError(reason)
            _FORECAST_CACHE[key] = {"hourly": None, "error": err, "fetched_at": time.monotonic()}
            raise err
        err = WeatherFetchError(f"Open-Meteo returned HTTP 400: {reason}")
        _FORECAST_CACHE[key] = {"hourly": None, "error": err, "fetched_at": time.monotonic()}
        raise err

    if resp.status_code != 200:
        err = WeatherFetchError(f"Open-Meteo returned HTTP {resp.status_code}: {str(data)[:200]}")
        _FORECAST_CACHE[key] = {"hourly": None, "error": err, "fetched_at": time.monotonic()}
        raise err

    hourly = data.get("hourly")
    if not isinstance(hourly, dict) or "time" not in hourly:
        err = WeatherFetchError(f"Unexpected Open-Meteo response shape (no 'hourly.time'): {str(data)[:200]}")
        _FORECAST_CACHE[key] = {"hourly": None, "error": err, "fetched_at": time.monotonic()}
        raise err

    _FORECAST_CACHE[key] = {"hourly": hourly, "error": None, "fetched_at": time.monotonic()}
    return hourly


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_iso_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _days_out(game_date: str, today: date | None = None) -> int | None:
    """Whole days between today (UTC) and the game's date. None if unparseable."""
    game_day = _parse_iso_date(game_date)
    if game_day is None:
        return None
    if today is None:
        today = datetime.now(timezone.utc).date()
    return (game_day - today).days


def _nearest_hour_index(times: list[str], kickoff_utc: str | None, tz: str) -> int:
    """
    Pick the hourly array index closest to kickoff.

    Open-Meteo's hourly.time values are localized to the requested `tz` and
    have no UTC suffix (e.g. "2026-10-04T13:00"). kickoff_utc (the raw
    Polymarket event's startTime, e.g. "2026-10-04T17:00:00Z") is converted
    to that same local timezone before comparing.

    Falls back to the middle of the day (index 12, local noon) when
    kickoff_utc is absent or unparseable -- a neutral estimate rather than a
    guess at an actual kickoff time, used only when the caller genuinely
    doesn't know it.
    """
    if kickoff_utc:
        try:
            from zoneinfo import ZoneInfo
            kickoff_dt = datetime.strptime(kickoff_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            local_dt = kickoff_dt.astimezone(ZoneInfo(tz))
            local_str = local_dt.strftime("%Y-%m-%dT%H:00")
            if local_str in times:
                return times.index(local_str)
            # Nearest by hour difference if the exact string isn't present
            # (shouldn't normally happen for a same-day hourly array).
            target_hour = local_dt.hour
            best_idx, best_diff = 0, None
            for i, t in enumerate(times):
                try:
                    hour = int(t[11:13])
                except (ValueError, IndexError):
                    continue
                diff = abs(hour - target_hour)
                if best_diff is None or diff < best_diff:
                    best_diff, best_idx = diff, i
            return best_idx
        except Exception:
            pass
    return min(12, len(times) - 1)


# ---------------------------------------------------------------------------
# Composite entry point
# ---------------------------------------------------------------------------

def get_game_weather(
    team_abbr: str,
    game_date: str,
    kickoff_utc: str | None = None,
) -> WeatherResult:
    """
    Return game-time weather for one game's venue (the HOME team's stadium).

    Args:
        team_abbr:   the HOME team's abbreviation (weather is a property of
                     the venue, which is the home team's stadium).
        game_date:   the game's date (YYYY-MM-DD) -- the same schedule-driven
                     date already carried on the game dict (team_mapping's
                     parse_teams_from_event / game_assembly's game_date), not
                     a separately determined date.
        kickoff_utc: the game's actual kickoff timestamp, if known (the raw
                     Polymarket event's startTime, e.g.
                     "2026-10-04T17:00:00Z"). Used to pick the single hourly
                     forecast value nearest kickoff. None degrades to a
                     neutral local-noon estimate rather than guessing a
                     kickoff time.

    Returns:
        WeatherResult. Never raises -- every failure mode (team not in the
        stadium seed, Open-Meteo network/shape failure) is reported as
        status="unavailable" with a reason, never a silently guessed default.
    """
    try:
        stadium = get_stadium_info(team_abbr)
    except WeatherFetchError as exc:
        return WeatherResult(team_abbr=team_abbr, status="unavailable", error=str(exc))

    if stadium is None:
        return WeatherResult(
            team_abbr=team_abbr, status="unavailable",
            error=f"No stadium found in seed for team {team_abbr!r}.",
        )

    if not stadium.is_outdoor:
        return WeatherResult(team_abbr=team_abbr, status="indoor", stadium_name=stadium.stadium_name)

    try:
        hourly = fetch_forecast_day(stadium.latitude, stadium.longitude, game_date, stadium.timezone)
    except ForecastHorizonError:
        days_out = _days_out(game_date)
        days_until_available = (
            max(0, days_out - _FORECAST_HORIZON_DAYS) if days_out is not None else None
        )
        return WeatherResult(
            team_abbr=team_abbr, status="forecast_not_yet_available",
            stadium_name=stadium.stadium_name,
            days_out=days_out, days_until_available=days_until_available,
        )
    except WeatherFetchError as exc:
        return WeatherResult(
            team_abbr=team_abbr, status="unavailable",
            stadium_name=stadium.stadium_name, error=str(exc),
        )

    times = hourly.get("time") or []
    if not times:
        return WeatherResult(
            team_abbr=team_abbr, status="unavailable",
            stadium_name=stadium.stadium_name,
            error="Open-Meteo returned an empty hourly forecast for this date.",
        )

    idx = _nearest_hour_index(times, kickoff_utc, stadium.timezone)

    def _at(field: str) -> float | None:
        values = hourly.get(field) or []
        if idx < len(values) and values[idx] is not None:
            return float(values[idx])
        return None

    return WeatherResult(
        team_abbr=team_abbr, status="ok",
        stadium_name=stadium.stadium_name,
        temperature_f=_at("temperature_2m"),
        wind_speed_mph=_at("wind_speed_10m"),
        precipitation_in=_at("precipitation"),
        precipitation_probability_pct=_at("precipitation_probability"),
    )
