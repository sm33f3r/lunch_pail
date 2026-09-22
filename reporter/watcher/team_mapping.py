"""
reporter/watcher/team_mapping.py

Parses away/home team identity from Polymarket NFL event data.

Polymarket has no structured team fields. Team identity is extracted from
the event slug (primary) or title (fallback), per confirmed recon patterns:
  - slug:  nfl-{away_abbr}-{home_abbr}-{YYYY-MM-DD}[optional-suffix]
  - title: "{Away Team} vs. {Home Team}"

All functions are pure (no network calls).
"""

from __future__ import annotations

import re
from typing import TypedDict

# ---------------------------------------------------------------------------
# Static abbreviation → full name mapping, all 32 NFL teams.
# Abbreviations match those used in Polymarket slugs (confirmed from live data).
# ---------------------------------------------------------------------------

ABBR_TO_NAME: dict[str, str] = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB":  "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC":  "Kansas City Chiefs",
    "LA":  "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LV":  "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE":  "New England Patriots",
    "NO":  "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF":  "San Francisco 49ers",
    "TB":  "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}

# Slug pattern: nfl-{away}-{home}-{YYYY-MM-DD}[optional-suffix]
# Prop market slugs share the same prefix (e.g. nfl-atl-gb-2026-09-25-first-td-scorer)
# so the regex anchors to the date and ignores everything after it.
_SLUG_RE = re.compile(
    r"^nfl-([a-z0-9]+)-([a-z0-9]+)-(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

# Title pattern: "{Away} vs. {Home}" — first team is always away (confirmed by recon)
_TITLE_RE = re.compile(r"^(.+?)\s+vs\.\s+(.+?)(?:\s+-\s+.+)?$")


class TeamParseError(ValueError):
    """Raised when team identity cannot be determined from event data."""


class ParsedTeams(TypedDict):
    away_team: str
    home_team: str
    away_abbr: str
    home_abbr: str
    game_date: str


def _abbr_to_name(abbr: str) -> str:
    key = abbr.upper()
    if key not in ABBR_TO_NAME:
        raise TeamParseError(
            f"Unknown team abbreviation {abbr!r}. "
            f"If this is a new/relocated franchise, update ABBR_TO_NAME in team_mapping.py."
        )
    return ABBR_TO_NAME[key]


def _parse_from_slug(slug: str) -> ParsedTeams | None:
    """
    Extract team abbreviations and game date from a Polymarket NFL slug.
    Returns None if the slug doesn't match the expected format.
    """
    m = _SLUG_RE.match(slug)
    if not m:
        return None
    away_abbr = m.group(1).upper()
    home_abbr = m.group(2).upper()
    game_date = m.group(3)
    return ParsedTeams(
        away_team=_abbr_to_name(away_abbr),
        home_team=_abbr_to_name(home_abbr),
        away_abbr=away_abbr,
        home_abbr=home_abbr,
        game_date=game_date,
    )


def _parse_from_title(title: str) -> ParsedTeams | None:
    """
    Extract team names from a Polymarket NFL event title.
    Returns None if the title doesn't match the expected format.
    Full names are returned as-is; abbreviations are looked up in reverse.
    """
    m = _TITLE_RE.match(title)
    if not m:
        return None
    away_name = m.group(1).strip()
    home_name = m.group(2).strip()

    # Reverse lookup: full name → abbreviation
    name_to_abbr = {v: k for k, v in ABBR_TO_NAME.items()}
    away_abbr = name_to_abbr.get(away_name)
    home_abbr = name_to_abbr.get(home_name)

    if not away_abbr or not home_abbr:
        return None  # Caller will raise with full context

    return ParsedTeams(
        away_team=away_name,
        home_team=home_name,
        away_abbr=away_abbr,
        home_abbr=home_abbr,
        game_date="",  # title carries no date
    )


def parse_teams_from_event(event: dict) -> ParsedTeams:
    """
    Parse away/home team identity from a Polymarket NFL event dict.

    Uses slug as primary source (structured, unambiguous). Falls back to
    title parsing if the slug format doesn't match.

    Args:
        event: A raw event dict as returned by get_current_nfl_games().

    Returns:
        ParsedTeams with away_team, home_team, away_abbr, home_abbr, game_date.

    Raises:
        TeamParseError: if team identity cannot be determined. A trading
            pipeline must not operate with unknown team identity.
    """
    slug = event.get("slug", "")
    title = event.get("title", "")
    event_id = event.get("id", "<unknown>")

    result = _parse_from_slug(slug)
    if result is not None:
        return result

    result = _parse_from_title(title)
    if result is not None:
        return result

    raise TeamParseError(
        f"Cannot parse team identity from event id={event_id!r} "
        f"slug={slug!r} title={title!r}. "
        f"Neither slug nor title matched expected patterns."
    )
