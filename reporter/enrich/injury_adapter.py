"""
reporter/enrich/injury_adapter.py

Fetches per-team NFL injury reports from ESPN (primary) with nflverse
CSV as a fallback.

Entry point: get_team_injuries(team_abbr) -> InjuryResult

ESPN path:
  - Hits sports.core.api.espn.com (the working endpoint; site.api.espn.com
    returns empty {} for every team and must not be used).
  - The list endpoint returns paginated $ref links; each ref is resolved
    concurrently with a bounded thread pool (_MAX_WORKERS).
  - A single failed $ref is skipped and logged; it does not abort the fetch.
  - practice_status is always set to the unavailable sentinel because ESPN
    has no structured practice-participation field.

nflverse fallback path:
  - Downloads the season CSV from nflverse-data GitHub releases.
  - practice_status IS populated here (nflverse has a structured field).
  - Only used when ESPN fails; tagged source="nflverse" so callers know
    the data is at most ~1 week stale.
"""

from __future__ import annotations

import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ESPN_LIST_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
    "/teams/{team_id}/injuries?limit=50"
)
_NFLVERSE_CSV_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/injuries"
    "/injuries_{season}.csv"
)

_ESPN_TIMEOUT    = 10   # seconds per request
_NFLVERSE_TIMEOUT = 15
_MAX_WORKERS     = 8    # concurrent $ref fetches per team

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
    """
    records: list[InjuryRecord]
    source:  str    # "espn" | "nflverse" | "unavailable"
    status:  str    # "ok" | "no_designations" | "unavailable"

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


def fetch_nflverse_injuries(
    team_abbr: str,
    week: int | None = None,
    season: int | None = None,
) -> list[InjuryRecord]:
    """
    Fetch injury data for one team from nflverse's season CSV.

    Args:
        team_abbr: e.g. "BAL". Matches nflverse team column directly (no crosswalk needed).
        week:      Filter to this week number. None = latest available week for the team.
        season:    Season year. None = current season.

    Returns:
        List of InjuryRecord with source="nflverse" and practice_status populated.

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
        return []

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

    return records


# ---------------------------------------------------------------------------
# Composite entry point
# ---------------------------------------------------------------------------

def get_team_injuries(team_abbr: str, week: int | None = None) -> InjuryResult:
    """
    Return the injury report for one team, ESPN first, nflverse as fallback.

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
    try:
        records = fetch_espn_injuries(team_abbr)
        status = "ok" if records else "no_designations"
        return InjuryResult(records=records, source="espn", status=status)
    except (ESPNError, ValueError) as exc:
        print(f"[injury_adapter] ESPN failed for {team_abbr!r}: {exc} — falling back to nflverse.")

    # nflverse — fallback, ~1 week stale but has structured practice_status
    try:
        records = fetch_nflverse_injuries(team_abbr, week=week)
        status = "ok" if records else "no_designations"
        return InjuryResult(records=records, source="nflverse", status=status)
    except NflverseError as exc:
        print(f"[injury_adapter] nflverse also failed for {team_abbr!r}: {exc}.")

    return _UNAVAILABLE
