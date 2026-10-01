"""
reporter/enrich/injury_adapter.py

Fetches NFL injury reports from ESPN (primary) with nflverse CSV as a
fallback.

Entry point: get_team_injuries(team_abbr) -> InjuryResult

ESPN path (default -- league-wide):
  - Production (Contabo/AS51167) gets HTTP 403 from sports.core.api.espn.com,
    so the old per-team $ref fan-out fails for nearly every team there. The
    default path instead hits the league-wide endpoint
    (site.api.espn.com/apis/site/v2/sports/football/nfl/injuries), which is
    reachable from production, in a single request covering all 32 teams.
  - The parsed payload is cached in-process with a short TTL
    (ESPN_INJURY_CACHE_TTL_SECONDS, default 300s) so N per-team lookups in
    one cycle do not trigger N downloads.
  - A single malformed record within a team's block is skipped and counted
    (InjuryResult.failed_count) rather than aborting the team's fetch.
  - practice_status is always set to the unavailable sentinel because ESPN
    has no structured practice-participation field.

ESPN path (legacy -- per-team, opt-in only):
  - The old sports.core.api.espn.com $ref fan-out. 403s from production, so
    it is NOT used by default. Enable with ESPN_LEGACY_PER_TEAM=true.

nflverse fallback path:
  - Downloads the season CSV from nflverse-data GitHub releases.
  - practice_status IS populated here (nflverse has a structured field).
  - Only used when ESPN fails; tagged source="nflverse" so callers know
    the data is at most ~1 week stale. InjuryResult carries the season/week
    the data actually covers and a `stale` flag when that week is not the
    current NFL week, so callers never mistake old data for current data.
"""

from __future__ import annotations

import csv
import io
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from reporter.enrich.team_stats_adapter import NflreadpyError, get_current_week

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ESPN_LEAGUE_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
)
_ESPN_LIST_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
    "/teams/{team_id}/injuries?limit=50"
)
_NFLVERSE_CSV_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/injuries"
    "/injuries_{season}.csv"
)

_ESPN_TIMEOUT        = 10   # seconds per request (legacy per-team path)
_ESPN_LEAGUE_TIMEOUT = 60   # league payload is ~8.7 MB
_NFLVERSE_TIMEOUT    = 15
_MAX_WORKERS         = 8    # concurrent $ref fetches per team (legacy path only)

# The league-wide endpoint (site.api.espn.com/.../nfl/injuries) returns at
# most this many records per team block, confirmed live on 2026-10-01: all
# 32 teams returned exactly 25 records (800 total / 32), and a per-team
# core-API comparison (sports.core.api.espn.com, full pagination) against
# 5 teams with heavy injury loads (BAL 62, DAL 61, GB 64, PIT 60, NYG 60
# real records) confirmed the league feed silently drops the rest. Records
# are sorted by `date` descending and the drop is a positional cutoff --
# oldest entries cut first.
#
# CONFIRMED HARMFUL: this is not just trimming stale "Active" noise. GB,
# DAL, and PIT all had genuine non-Active designations beyond position 25
# -- e.g. GB's Josh Jacobs, Jordon Riley, and Luke Musgrave were all status
# "Out" but ranked 26+ (cut from the league feed) because their status
# hadn't been re-dated recently relative to the team's other injury
# traffic. A long-term "Out"/"Injured Reserve" player whose entry goes
# stale in date terms can fall off the list even though their designation
# is still fully current. This is exactly the misinformation risk this
# flag exists to catch -- do not treat hitting the cap as "probably fine."
_LEAGUE_INJURY_CAP = 25

_DEFAULT_CACHE_TTL_SECONDS = 300


def _cache_ttl_seconds() -> float:
    """Read fresh each call so tests can monkeypatch the env between calls."""
    raw = os.environ.get("ESPN_INJURY_CACHE_TTL_SECONDS")
    if not raw:
        return float(_DEFAULT_CACHE_TTL_SECONDS)
    try:
        return float(raw)
    except ValueError:
        return float(_DEFAULT_CACHE_TTL_SECONDS)


def _legacy_per_team_enabled() -> bool:
    """
    The old per-team sports.core.api.espn.com fan-out 403s from production
    (Contabo, AS51167) and must not run by default. Opt in explicitly.
    """
    return os.environ.get("ESPN_LEGACY_PER_TEAM", "").strip().lower() in (
        "1", "true", "yes", "on",
    )

# Sentinel stored in InjuryRecord.practice_status when source is ESPN.
# Callers must check for this string rather than None; it is deliberately
# loud so a renderer never silently omits practice status without saying why.
PRACTICE_STATUS_UNAVAILABLE = "unavailable (ESPN source — no structured practice status)"

# Polymarket/Lunch-Pail abbreviation → ESPN numeric team ID.
# All 32 teams. Two abbreviations diverge from ESPN's own:
#   our "LA"  → ESPN uses "LAR", team ID 14
#   our "WAS" → ESPN uses "WSH", team ID 28
_ABBR_TO_ESPN_ID: dict[str, int] = {
    "ARI": 22,
    "ATL": 1,
    "BAL": 33,
    "BUF": 2,
    "CAR": 29,
    "CHI": 3,
    "CIN": 4,
    "CLE": 5,
    "DAL": 6,
    "DEN": 7,
    "DET": 8,
    "GB":  9,
    "HOU": 34,
    "IND": 11,
    "JAX": 30,
    "KC":  12,
    "LA":  14,
    "LAC": 24,
    "LV":  13,
    "MIA": 15,
    "MIN": 16,
    "NE":  17,
    "NO":  18,
    "NYG": 19,
    "NYJ": 20,
    "PHI": 21,
    "PIT": 23,
    "SEA": 26,
    "SF":  25,
    "TB":  27,
    "TEN": 10,
    "WAS": 28,
}

_ESPN_ID_TO_ABBR: dict[str, str] = {
    str(team_id): abbr for abbr, team_id in _ABBR_TO_ESPN_ID.items()
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InjuryRecord:
    """One player's injury status, normalised across ESPN and nflverse."""
    player_name:     str
    position:        str
    designation:     str          # "Active" | "Questionable" | "Doubtful" | "Out"
    practice_status: str | None   # nflverse: string value; ESPN: PRACTICE_STATUS_UNAVAILABLE
    injury_type:     str | None   # body part ("Ankle", "Knee", …); None if not reported
    source:          str          # "espn" | "nflverse"
    updated_at:      str | None   # ISO 8601 from ESPN; None for nflverse
    short_comment:   str | None = None   # ESPN details.shortComment; None for nflverse
    location:        str | None = None   # ESPN details.location; None for nflverse
    side:            str | None = None   # ESPN details.side; None if "Not Specified" or nflverse
    return_date:     str | None = None   # ESPN details.returnDate; None for nflverse


@dataclass(frozen=True)
class InjuryResult:
    """
    Container returned by get_team_injuries().

    status values:
      "ok"              — records found (may include "Active" entries from ESPN)
      "no_designations" — fetch succeeded but no records returned for this team
      "unavailable"     — both ESPN and nflverse failed; do not use records

    failed_count:
      Number of records in this team's block that failed to parse and were
      skipped. A team with failures must never render identically to a
      complete team, so this is always surfaced by the report writer.

    as_of:
      ESPN league payload's top-level `timestamp` field (when source="espn").
      None for nflverse/unavailable results.

    nflverse_season / nflverse_week:
      The season/week the nflverse fallback data actually covers (when
      source="nflverse"). None when not applicable.

    stale:
      True when source="nflverse" and nflverse_week is not the current NFL
      week -- i.e. this data should not be treated as current-week data.

    possibly_incomplete:
      True when source="espn" (league-wide path) and this team's raw record
      count hit _LEAGUE_INJURY_CAP (25) -- the league feed may be silently
      truncating this team's list. This is distinct from "unavailable" (no
      data at all) and "no_designations" (fetch succeeded, team genuinely
      has none): here we have data, but cannot promise it is everything.
      Never set for the legacy per-team ESPN path (which paginates fully)
      or nflverse (full season CSV, no per-team cap).
    """
    records: list[InjuryRecord]
    source:  str    # "espn" | "nflverse" | "unavailable"
    status:  str    # "ok" | "no_designations" | "unavailable"
    failed_count:        int = 0
    as_of:               str | None = None
    nflverse_season:     int | None = None
    nflverse_week:       int | None = None
    stale:               bool = False
    possibly_incomplete: bool = False

    @property
    def is_unavailable(self) -> bool:
        return self.status == "unavailable"

    @property
    def has_designations(self) -> bool:
        return self.status == "ok" and bool(self.records)


_UNAVAILABLE = InjuryResult(records=[], source="unavailable", status="unavailable")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _current_season() -> int:
    """Current NFL season year (Sep–Jan; year of September = season year)."""
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 3 else now.year - 1


def _season_kickoff(season: int) -> datetime:
    """First Thursday of September of the given season -- approximate Week 1 kickoff."""
    d = datetime(season, 9, 1, tzinfo=timezone.utc)
    days_ahead = (3 - d.weekday()) % 7   # Thursday == weekday 3
    return d + timedelta(days=days_ahead)


def _current_nfl_week(season: int | None = None) -> int:
    """
    Best-effort current NFL week number, clamped to [1, 18].

    This is a date-based approximation (regular season == 18 weeks starting
    the first Thursday of September). It is NOT the primary path any more --
    _resolve_current_week() below prefers the schedule-driven answer from
    team_stats_adapter.get_current_week(). This heuristic is kept only as a
    degrade-to path for when the nflreadpy schedules fetch itself fails, so
    a network hiccup never crashes injury enrichment; it is not fed a real
    schedule, so it can be off by one around bye weeks/holidays.
    """
    now = datetime.now(timezone.utc)
    if season is None:
        season = _current_season()
    kickoff = _season_kickoff(season)
    if now < kickoff:
        return 1
    week = (now - kickoff).days // 7 + 1
    return max(1, min(week, 18))


def _resolve_current_week(season: int | None = None) -> int:
    """
    Schedule-driven current NFL week (team_stats_adapter.get_current_week),
    falling back to the old date-based heuristic (_current_nfl_week) if the
    nflreadpy schedules fetch itself fails. Never raises -- a network
    hiccup must not abort injury enrichment -- but always logs when the
    degraded path is used, per the "no silent defaults" rule: callers must
    be able to tell the difference between a real schedule-based answer and
    a coarse fallback from the logs.
    """
    try:
        return get_current_week(season)
    except NflreadpyError as exc:
        print(
            f"[injury_adapter] schedule-driven current week lookup failed "
            f"({exc}); falling back to date heuristic."
        )
        return _current_nfl_week(season)


def _espn_team_id(abbr: str) -> int:
    key = abbr.upper()
    if key not in _ABBR_TO_ESPN_ID:
        raise ValueError(
            f"Unknown team abbreviation {abbr!r}. "
            "Update _ABBR_TO_ESPN_ID in injury_adapter.py if this is a new franchise."
        )
    return _ABBR_TO_ESPN_ID[key]


def _fetch_one_ref(url: str) -> dict | None:
    """
    Fetch one ESPN injury $ref URL and resolve its nested athlete $ref.

    The live API returns athlete as {"$ref": "..."} (not inline). We follow
    it within the same call so _parse_espn_record can read fullName and
    position.abbreviation. The athlete's position is inline in the athlete
    response (no third call needed).

    Returns None on any failure so the caller can skip this record.
    """
    try:
        resp = requests.get(url, timeout=_ESPN_TIMEOUT)
        resp.raise_for_status()
        record = resp.json()
    except Exception as exc:
        print(f"[injury_adapter] injury ref failed {url!r}: {exc}")
        return None

    # Resolve the athlete $ref when the live API returns it as a bare link.
    # Fixtures captured by the recon script already have this resolved inline.
    athlete = record.get("athlete") or {}
    if "$ref" in athlete and "fullName" not in athlete:
        try:
            a_resp = requests.get(athlete["$ref"], timeout=_ESPN_TIMEOUT)
            a_resp.raise_for_status()
            record["athlete"] = a_resp.json()
        except Exception as exc:
            print(f"[injury_adapter] athlete ref failed for {url!r}: {exc}")
            return None   # record is useless without a player name

    return record


def _fetch_espn_ref_list(team_id: int) -> list[str]:
    """
    Retrieve all $ref URLs from the ESPN team injuries list endpoint.
    Handles pagination via the pageCount field.

    Raises:
        ESPNError on network failures or unexpected response shape.
    """
    ref_urls: list[str] = []
    page = 1
    while True:
        url = _ESPN_LIST_URL.format(team_id=team_id)
        if page > 1:
            url += f"&page={page}"
        try:
            resp = requests.get(url, timeout=_ESPN_TIMEOUT)
        except requests.RequestException as exc:
            raise ESPNError(f"ESPN list request failed for team {team_id}: {exc}") from exc

        if resp.status_code != 200:
            raise ESPNError(
                f"ESPN returned HTTP {resp.status_code} for team {team_id}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ESPNError(f"ESPN non-JSON response for team {team_id}: {exc}") from exc

        items = data.get("items")
        if items is None:
            # Empty team or unexpected shape — treat as zero injuries, not an error.
            break
        if not isinstance(items, list):
            raise ESPNError(
                f"Unexpected ESPN items type for team {team_id}: {type(items)}"
            )

        for item in items:
            ref = item.get("$ref")
            if ref:
                ref_urls.append(ref)

        page_count = int(data.get("pageCount", 1))
        if page >= page_count:
            break
        page += 1

    return ref_urls


def _parse_espn_record(raw: dict) -> InjuryRecord | None:
    """Parse one resolved ESPN injury dict into an InjuryRecord. Returns None if malformed."""
    athlete = raw.get("athlete") or {}
    if not athlete or not athlete.get("fullName"):
        return None
    position_obj = athlete.get("position") or {}
    details = raw.get("details") or {}
    side = details.get("side") or None
    if side == "Not Specified":
        side = None
    return InjuryRecord(
        player_name=athlete.get("fullName", ""),
        position=position_obj.get("abbreviation", ""),
        designation=raw.get("status", ""),
        practice_status=PRACTICE_STATUS_UNAVAILABLE,
        injury_type=details.get("type") or None,
        source="espn",
        updated_at=raw.get("date") or None,
        short_comment=raw.get("shortComment") or None,
        location=details.get("location") or None,
        side=side,
        return_date=details.get("returnDate") or None,
    )


def _parse_league_record(raw: dict) -> InjuryRecord | None:
    """
    Parse one injury dict from the league-wide payload's per-team
    `injuries` list into an InjuryRecord. Returns None if malformed.

    Shape (verified against the live endpoint):
      id, status (titlecase), date, shortComment, longComment, source,
      type, athlete{displayName, firstName, lastName, position{abbreviation}}.
      details{type, location, side, detail, returnDate} is present only on
      injured players (~1/3 of records) -- absent on "Active" is expected,
      not an error.
    """
    athlete = raw.get("athlete") or {}
    name = athlete.get("displayName") or ""
    if not name:
        return None
    position_obj = athlete.get("position") or {}
    details = raw.get("details") or {}
    side = details.get("side") or None
    if side == "Not Specified":
        side = None
    return InjuryRecord(
        player_name=name,
        position=position_obj.get("abbreviation", ""),
        designation=raw.get("status", ""),
        practice_status=PRACTICE_STATUS_UNAVAILABLE,
        injury_type=details.get("type") or None,
        source="espn",
        updated_at=raw.get("date") or None,
        short_comment=raw.get("shortComment") or None,
        location=details.get("location") or None,
        side=side,
        return_date=details.get("returnDate") or None,
    )


# Module-level cache for the league-wide payload. A single fetch serves
# every team lookup in a cycle; TTL keeps it from going stale across cycles.
_LEAGUE_CACHE: dict = {"payload": None, "fetched_at": None}


def _reset_league_cache() -> None:
    """Test helper -- clears the in-process league payload cache."""
    _LEAGUE_CACHE["payload"] = None
    _LEAGUE_CACHE["fetched_at"] = None


def fetch_espn_league_payload() -> dict:
    """
    Fetch (or serve from cache) the league-wide ESPN injuries payload.

    One request covers all 32 teams (~8.7 MB). The parsed result is cached
    in-process for ESPN_INJURY_CACHE_TTL_SECONDS (default 300s) so that
    looking up injuries for many teams/games in one cycle does not trigger
    a download per team.

    Raises:
        ESPNError: on network failure, non-200 status, or non-JSON body.
    """
    cached = _LEAGUE_CACHE["payload"]
    fetched_at = _LEAGUE_CACHE["fetched_at"]
    if cached is not None and fetched_at is not None:
        if (time.monotonic() - fetched_at) < _cache_ttl_seconds():
            return cached

    try:
        resp = requests.get(_ESPN_LEAGUE_URL, timeout=_ESPN_LEAGUE_TIMEOUT)
    except requests.RequestException as exc:
        raise ESPNError(f"ESPN league injuries request failed: {exc}") from exc

    if resp.status_code != 200:
        raise ESPNError(f"ESPN league injuries returned HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ESPNError(f"ESPN league injuries non-JSON response: {exc}") from exc

    _LEAGUE_CACHE["payload"] = payload
    _LEAGUE_CACHE["fetched_at"] = time.monotonic()
    return payload


def _team_block(payload: dict, team_abbr: str) -> dict | None:
    """Find the team block in the league payload matching team_abbr's ESPN id."""
    team_id = str(_espn_team_id(team_abbr))
    for block in payload.get("injuries") or []:
        if str(block.get("id")) == team_id:
            return block
    return None


def fetch_espn_league_injuries(team_abbr: str) -> tuple[list[InjuryRecord], int, str | None]:
    """
    Fetch this team's injuries from the (cached) league-wide ESPN payload.

    Returns:
        (records, failed_count, as_of) where failed_count is the number of
        records in this team's block that failed to parse and were skipped,
        and as_of is the payload's top-level `timestamp` field.

    Raises:
        ESPNError: if the league payload cannot be fetched/parsed.
        ValueError: if team_abbr is not recognised.
    """
    payload = fetch_espn_league_payload()
    as_of = payload.get("timestamp")
    block = _team_block(payload, team_abbr)
    if block is None:
        return [], 0, as_of

    records: list[InjuryRecord] = []
    failed = 0
    for raw in block.get("injuries") or []:
        rec = _parse_league_record(raw)
        if rec is None:
            failed += 1
        else:
            records.append(rec)

    return records, failed, as_of


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ESPNError(Exception):
    """Raised when the ESPN injuries endpoint cannot be used."""


class NflverseError(Exception):
    """Raised when the nflverse CSV cannot be fetched or parsed."""


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------

def fetch_espn_injuries(team_abbr: str) -> list[InjuryRecord]:
    """
    Fetch the full injury list for one team from ESPN.

    $ref URLs from the list endpoint are resolved concurrently (up to
    _MAX_WORKERS in parallel). A single failed ref is skipped and logged.

    Returns:
        List of InjuryRecord with source="espn" and
        practice_status=PRACTICE_STATUS_UNAVAILABLE for every record.

    Raises:
        ESPNError: if the list request fails (not for individual ref failures).
        ValueError: if team_abbr is not recognised.
    """
    team_id = _espn_team_id(team_abbr)
    ref_urls = _fetch_espn_ref_list(team_id)

    if not ref_urls:
        return []

    raw_records: list[dict] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_one_ref, url): url for url in ref_urls}
        for future in as_completed(futures):
            result = future.result()   # _fetch_one_ref never raises; None on failure
            if result is not None:
                raw_records.append(result)

    records = []
    for raw in raw_records:
        record = _parse_espn_record(raw)
        if record is not None:
            records.append(record)

    return records


def fetch_nflverse_injuries_with_meta(
    team_abbr: str,
    week: int | None = None,
    season: int | None = None,
) -> tuple[list[InjuryRecord], int, int | None]:
    """
    Fetch injury data for one team from nflverse's season CSV, along with
    the season/week the returned data actually covers.

    Args:
        team_abbr: e.g. "BAL". Matches nflverse team column directly (no crosswalk needed).
        week:      Filter to this week number. None = latest available week for the team.
        season:    Season year. None = current season.

    Returns:
        (records, season, target_week). target_week is None when the CSV
        has no usable week column for this team (records are unfiltered
        in that case, and staleness cannot be determined).

    Raises:
        NflverseError: on network or parse failure.
    """
    if season is None:
        season = _current_season()

    url = _NFLVERSE_CSV_URL.format(season=season)
    try:
        resp = requests.get(url, timeout=_NFLVERSE_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        raise NflverseError(f"nflverse request failed: {exc}") from exc

    if resp.status_code != 200:
        raise NflverseError(f"nflverse returned HTTP {resp.status_code}")

    try:
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = [r for r in reader if r.get("team", "").upper() == team_abbr.upper()]
    except Exception as exc:
        raise NflverseError(f"nflverse CSV parse failed: {exc}") from exc

    if not rows:
        return [], season, None

    # Resolve target week.
    if week is not None:
        target_week = week
    else:
        try:
            target_week = max(int(r["week"]) for r in rows if r.get("week", "").isdigit())
        except (ValueError, TypeError):
            target_week = None

    if target_week is not None:
        rows = [r for r in rows if r.get("week") == str(target_week)]

    records = []
    for row in rows:
        designation = (row.get("report_status") or "").strip() or "Active"
        practice_status = (row.get("practice_status") or "").strip() or None
        records.append(InjuryRecord(
            player_name=row.get("full_name", ""),
            position=row.get("position", ""),
            designation=designation,
            practice_status=practice_status,
            injury_type=(row.get("report_primary_injury") or "").strip() or None,
            source="nflverse",
            updated_at=None,
        ))

    return records, season, target_week


def fetch_nflverse_injuries(
    team_abbr: str,
    week: int | None = None,
    season: int | None = None,
) -> list[InjuryRecord]:
    """
    Fetch injury data for one team from nflverse's season CSV.

    Thin wrapper around fetch_nflverse_injuries_with_meta() for callers that
    only need the records (kept for backward compatibility).

    Returns:
        List of InjuryRecord with source="nflverse" and practice_status populated.

    Raises:
        NflverseError: on network or parse failure.
    """
    records, _season, _week = fetch_nflverse_injuries_with_meta(team_abbr, week=week, season=season)
    return records


# ---------------------------------------------------------------------------
# Composite entry point
# ---------------------------------------------------------------------------

def get_team_injuries(team_abbr: str, week: int | None = None) -> InjuryResult:
    """
    Return the injury report for one team.

    Source chain: league-wide ESPN -> nflverse -> unavailable. The legacy
    per-team ESPN path (sports.core.api.espn.com) 403s from production and
    is only used when ESPN_LEGACY_PER_TEAM is explicitly enabled.

    Return states:
      status="ok"              records found; source indicates which data provider
      status="no_designations" fetch succeeded but this team has no injury entries
      status="unavailable"     both providers failed; records is empty

    Args:
        team_abbr: e.g. "BAL", "LA" (Rams), "WAS" (Commanders)
        week:      Passed to the nflverse fallback as its week filter.

    Returns:
        InjuryResult
    """
    # ESPN — primary, current-week live data
    if _legacy_per_team_enabled():
        try:
            records = fetch_espn_injuries(team_abbr)
            status = "ok" if records else "no_designations"
            return InjuryResult(records=records, source="espn", status=status)
        except (ESPNError, ValueError) as exc:
            print(f"[injury_adapter] ESPN (legacy per-team) failed for {team_abbr!r}: {exc} — falling back to nflverse.")
    else:
        try:
            records, failed_count, as_of = fetch_espn_league_injuries(team_abbr)
            status = "ok" if records else "no_designations"
            # Raw block size = parsed records + the ones that failed to parse
            # but were still present in the team's block. The cap applies to
            # what the feed returned, not to what we successfully parsed.
            raw_count = len(records) + failed_count
            possibly_incomplete = raw_count == _LEAGUE_INJURY_CAP
            return InjuryResult(
                records=records,
                source="espn",
                status=status,
                failed_count=failed_count,
                as_of=as_of,
                possibly_incomplete=possibly_incomplete,
            )
        except (ESPNError, ValueError) as exc:
            print(f"[injury_adapter] ESPN (league-wide) failed for {team_abbr!r}: {exc} — falling back to nflverse.")

    # nflverse — fallback, ~1 week stale but has structured practice_status
    try:
        records, nfl_season, target_week = fetch_nflverse_injuries_with_meta(team_abbr, week=week)
        status = "ok" if records else "no_designations"
        stale = target_week is not None and target_week != _resolve_current_week(nfl_season)
        return InjuryResult(
            records=records,
            source="nflverse",
            status=status,
            nflverse_season=nfl_season,
            nflverse_week=target_week,
            stale=stale,
        )
    except NflverseError as exc:
        print(f"[injury_adapter] nflverse also failed for {team_abbr!r}: {exc}.")

    return _UNAVAILABLE
