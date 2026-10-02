"""
reporter/enrich/situational_context.py

Computes game-situational context: home/away (already resolved by
game_assembly.py -- just surfaced here), rest-day differential between
the two teams, divisional-game flag, and week of season.

Entry point: get_situational_context(away_abbr, home_abbr, game_date, season=None)
    -> SituationalContext

No new data source -- this reuses reporter.enrich.team_stats_adapter's
cached nflreadpy schedules fetch (fetch_schedules_df) and its schedule-
driven week lookup (get_game_week_context), the same pattern as the
#1/#2 week-context fix, so this module adds zero new network calls.

Week of season:
  Reuses team_stats_adapter.get_game_week_context() directly rather than
  re-deriving it -- see that function's docstring for the date-window
  matching logic and its found=False / no-silent-defaults guarantee.

Divisional-game flag (see SITUATIONAL_RECON.md):
  nflreadpy's own schedules dataframe carries a `div_game` column (0/1),
  confirmed present via a live load_schedules(seasons=2026) call on
  2026-10-02, and spot-checked by hand against known 2026 divisions for
  the current slate: 2026_03_CIN_PIT div_game=1 (AFC North, correct),
  2026_03_ARI_SF div_game=1 (NFC West, correct), 2026_03_SEA_WAS
  div_game=0 (NFC West vs. NFC East, correct). Read directly from the
  matched schedule row -- never a hardcoded division map, so this stays
  correct across any realignment nflverse's own data reflects.

Rest days (see SITUATIONAL_RECON.md):
  Deliberately NOT read from nflreadpy's own away_rest/home_rest columns.
  Those columns were inspected live and found to default to a flat 7 for
  EVERY week-1 matchup regardless of any team's actual prior-season
  schedule -- a schedule-generation placeholder, not a real "first game
  of the season" signal, and exactly the kind of misleading default this
  phase's point-in-time rules forbid. Instead, rest days are computed
  directly from the schedule's own `gameday` values: each team's
  immediately prior game THIS SEASON, found by week number (so a bye
  week is skipped correctly -- we want the team's next-lower scheduled
  week, not a fixed N-days-back window), then the calendar-day
  difference between that gameday and the target game's own date. A
  team's week-1 game has no prior game this season at all -- returned as
  an explicit "season_opener" state, never 0 or any numeric default.
  (Live spot-check, see SITUATIONAL_RECON.md: this calculation was
  verified to reproduce nflreadpy's own away_rest/home_rest values
  exactly whenever a real prior game exists -- e.g. PHI's week-3 game
  on 2026-09-28, prior game 2026-09-20, computes 8 days, matching
  nflreadpy's away_rest=8 for that row -- the discrepancy is isolated to
  the week-1 placeholder case.)

Status states, per piece (each independent -- a failure in one piece
never blanks the others):
  SituationalContext.status:
    "ok"          -- the schedules fetch succeeded (sub-pieces may still
                     be individually unavailable).
    "unavailable" -- the nflreadpy schedules fetch itself failed, or the
                     week context lookup's own fetch failed; all
                     sub-fields are None.
  RestDaysResult.status:
    "ok"             -- days computed from a real prior game this season.
    "season_opener"  -- this is the team's first game of the season; no
                        prior-game data exists to compute from.
    "unavailable"    -- the target game's own week could not be
                        resolved, or its date could not be parsed.
  DivisionalGameResult.status:
    "ok"          -- the target game's row was matched in schedule data
                     and its div_game field was read successfully.
    "unavailable" -- the target game's week could not be resolved, no
                     matching schedule row was found, or div_game was
                     missing/unparseable on that row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from reporter.enrich.team_stats_adapter import (
    NflreadpyError,
    WeekContext,
    fetch_schedules_df,
    get_game_week_context,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RestDaysResult:
    """One team's rest-day computation for the target game."""
    status: str               # "ok" | "season_opener" | "unavailable"
    days:   int | None = None
    error:  str | None = None


@dataclass(frozen=True)
class DivisionalGameResult:
    """Whether the target game is a divisional matchup."""
    status:        str            # "ok" | "unavailable"
    is_divisional: bool | None = None
    error:         str | None = None


@dataclass(frozen=True)
class SituationalContext:
    """Container returned by get_situational_context()."""
    status:     str    # "ok" | "unavailable" -- fetch-level only
    away_abbr:  str
    home_abbr:  str
    season:     int | None
    week:       int | None                  # the target game's own week; None if not resolved
    week_found: bool                        # mirrors WeekContext.found
    away_rest:  RestDaysResult | None       # None only when status == "unavailable"
    home_rest:  RestDaysResult | None
    divisional: DivisionalGameResult | None
    error:      str | None = None           # populated when status == "unavailable"

    @property
    def is_unavailable(self) -> bool:
        return self.status == "unavailable"


def _unavailable(away_abbr: str, home_abbr: str, season: int | None, error: str) -> SituationalContext:
    return SituationalContext(
        status="unavailable", away_abbr=away_abbr, home_abbr=home_abbr,
        season=season, week=None, week_found=False,
        away_rest=None, home_rest=None, divisional=None, error=error,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_iso_date(value: str | date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _team_rest_days(
    schedules_df: pl.DataFrame,
    team_abbr: str,
    season: int,
    week: int | None,
    game_date_obj: date | None,
) -> RestDaysResult:
    """
    Days since team_abbr's immediately prior game this season (by week
    number, ascending), relative to game_date_obj.

    Returns "season_opener" when no such prior game exists (week 1, or
    the team's earliest appearance in the season's schedule) -- never 0.
    """
    if week is None:
        return RestDaysResult(status="unavailable", error="game week not resolved; cannot locate prior game")
    if game_date_obj is None:
        return RestDaysResult(status="unavailable", error="target game date unknown/unparseable")

    prior = schedules_df.filter(
        (pl.col("season") == season)
        & (pl.col("week") < week)
        & ((pl.col("home_team") == team_abbr) | (pl.col("away_team") == team_abbr))
    ).sort("week")

    if prior.shape[0] == 0:
        return RestDaysResult(status="season_opener")

    prior_gameday = prior["gameday"].to_list()[-1]
    prior_date = _parse_iso_date(prior_gameday)
    if prior_date is None:
        return RestDaysResult(status="unavailable", error=f"could not parse prior game date {prior_gameday!r}")

    return RestDaysResult(status="ok", days=(game_date_obj - prior_date).days)


def _divisional_flag(
    schedules_df: pl.DataFrame,
    away_abbr: str,
    home_abbr: str,
    season: int,
    week: int | None,
) -> DivisionalGameResult:
    """Read nflreadpy's own div_game field off the target game's matched schedule row."""
    if week is None:
        return DivisionalGameResult(status="unavailable", error="game week not resolved; cannot match schedule row")

    row = schedules_df.filter(
        (pl.col("season") == season)
        & (pl.col("week") == week)
        & (pl.col("away_team") == away_abbr)
        & (pl.col("home_team") == home_abbr)
    )
    if row.shape[0] == 0:
        return DivisionalGameResult(status="unavailable", error="no matching schedule row for this game/week")

    if "div_game" not in row.columns:
        return DivisionalGameResult(status="unavailable", error="div_game column missing from schedule data")

    raw = row["div_game"].to_list()[0]
    if raw is None:
        return DivisionalGameResult(status="unavailable", error="div_game value missing on matched schedule row")

    return DivisionalGameResult(status="ok", is_divisional=bool(raw))


# ---------------------------------------------------------------------------
# Composite entry point
# ---------------------------------------------------------------------------

def get_situational_context(
    away_abbr: str,
    home_abbr: str,
    game_date: str | date | datetime | None,
    season: int | None = None,
) -> SituationalContext:
    """
    Return home/away, rest-day, divisional-game, and week-of-season
    context for one game.

    Args:
        away_abbr:  the away team's abbreviation.
        home_abbr:  the home team's abbreviation.
        game_date:  the target game's own date (YYYY-MM-DD string, or a
                    date/datetime object).
        season:     season year. Defaults to nflreadpy.get_current_season()
                    (resolved via get_game_week_context()).

    Returns:
        SituationalContext. status="unavailable" (all sub-fields None)
        only when the nflreadpy schedules fetch itself fails. A resolvable
        schedules fetch but an unmatched/unresolvable game still returns
        status="ok" with each sub-piece independently marked
        "unavailable"/"season_opener" as appropriate -- never a top-level
        failure for a per-piece lookup miss.
    """
    try:
        week_ctx: WeekContext = get_game_week_context(away_abbr, game_date, season)
    except NflreadpyError as exc:
        return _unavailable(away_abbr, home_abbr, season, str(exc))

    season = week_ctx.season

    try:
        schedules_df = fetch_schedules_df(season)
    except NflreadpyError as exc:
        return _unavailable(away_abbr, home_abbr, season, str(exc))

    week = week_ctx.game_week if week_ctx.found else None
    game_date_obj = _parse_iso_date(game_date)

    away_rest = _team_rest_days(schedules_df, away_abbr, season, week, game_date_obj)
    home_rest = _team_rest_days(schedules_df, home_abbr, season, week, game_date_obj)
    divisional = _divisional_flag(schedules_df, away_abbr, home_abbr, season, week)

    return SituationalContext(
        status="ok", away_abbr=away_abbr, home_abbr=home_abbr,
        season=season, week=week, week_found=week_ctx.found,
        away_rest=away_rest, home_rest=home_rest, divisional=divisional,
    )
