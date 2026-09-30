"""
reporter/enrich/team_stats_adapter.py

Fetches rolling team-performance stats from nflreadpy: last-4-completed-games
and season-to-date EPA/play (offense + defense), points for/against, and
win-loss record, per team.

Entry point: get_team_rolling_stats(team_abbr, before_date) -> TeamStatsResult

Data source (single source, no fallback -- see ROLLING_STATS_RECON.md):
  - nflreadpy.load_schedules(seasons=season): final scores / win-loss.
  - nflreadpy.load_pbp(seasons=season): play-level EPA, aggregated to
    per-team-per-game offense/defense EPA/play using the filter verified in
    recon: (rush_attempt or pass_attempt), excluding qb_kneel/qb_spike, and
    epa not null, grouped by posteam (offense) / defteam (defense allowed).
  - Both calls hit nflreadpy's nflverse-data GitHub Releases transport --
    the same domain/redirect chain already used by injury_adapter.py's
    nflverse CSV fallback.
  - Both dataframes are cached in-process with a short TTL
    (TEAM_STATS_CACHE_TTL_SECONDS, default 300s) so N per-team lookups in
    one cycle only fetch each season's data once.

Point-in-time correctness (non-negotiable, see ROLLING_STATS_RECON.md #7):
  - Only games with a non-null score are ever included ("completed"). The
    game currently being reported on always has a null score in nflreadpy
    at report time -- get_reportable_games() only returns games where the
    Polymarket market is still open (closed=False), so the target game can
    never itself be "completed" data. This is the primary guarantee.
  - When a `before_date` (the target game's own date, YYYY-MM-DD) is
    supplied, completed games are additionally filtered to
    gameday < before_date, as a second guard against including a same-day
    or later game.
  - No padding: when fewer than 4 (or 0) completed games exist, the real
    count is always returned in `games_used` -- never silently computed
    over a shorter/empty window without saying so, and never padded with
    a prior season's games.

Status states (mirrors injury_adapter.InjuryResult's pattern):
  "ok"               -- fetch succeeded; games_used may be 0..4 for last4
                        and 0..N for season_to_date, always the real count.
  "insufficient_data" -- fetch succeeded but zero completed games exist
                        yet this season for this team (e.g. week 1).
  "unavailable"      -- the nflreadpy fetch itself failed (schedules or
                        pbp). Never defaults any stat to 0.0 in this case.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import nflreadpy
import polars as pl

_DEFAULT_CACHE_TTL_SECONDS = 300
_MAX_GAMES_DEFAULT = 4


def _cache_ttl_seconds() -> float:
    """Read fresh each call so tests can monkeypatch the env between calls."""
    raw = os.environ.get("TEAM_STATS_CACHE_TTL_SECONDS")
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
class WindowStats:
    """Stats over one rolling window (last-4 or season-to-date) for one team."""
    status:              str            # "ok" | "insufficient_data"
    games_used:          int            # real count; never padded
    epa_offense:         float | None   # None only when games_used == 0
    epa_defense:         float | None
    points_for_avg:      float | None
    points_against_avg:  float | None
    wins:                int
    losses:              int
    ties:                int


_INSUFFICIENT_WINDOW = WindowStats(
    status="insufficient_data", games_used=0,
    epa_offense=None, epa_defense=None,
    points_for_avg=None, points_against_avg=None,
    wins=0, losses=0, ties=0,
)


@dataclass(frozen=True)
class TeamStatsResult:
    """Container returned by get_team_rolling_stats()."""
    team_abbr:      str
    status:         str    # "ok" | "unavailable" -- fetch-level only
    season:         int | None
    last4:          WindowStats | None   # None only when status == "unavailable"
    season_to_date: WindowStats | None
    error:          str | None = None    # populated when status == "unavailable"

    @property
    def is_unavailable(self) -> bool:
        return self.status == "unavailable"


def _unavailable(team_abbr: str, season: int | None, error: str) -> TeamStatsResult:
    return TeamStatsResult(
        team_abbr=team_abbr, status="unavailable", season=season,
        last4=None, season_to_date=None, error=error,
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class NflreadpyError(Exception):
    """Raised when nflreadpy's schedules or pbp fetch fails."""


# ---------------------------------------------------------------------------
# Cached fetch -- one entry per season
# ---------------------------------------------------------------------------

_SCHEDULES_CACHE: dict[int, dict] = {}
_PBP_CACHE: dict[int, dict] = {}


def _reset_caches() -> None:
    """Test helper -- clears both in-process caches."""
    _SCHEDULES_CACHE.clear()
    _PBP_CACHE.clear()


def fetch_schedules_df(season: int) -> pl.DataFrame:
    """
    Fetch (or serve from cache) nflreadpy's schedules dataframe for one season.

    Raises:
        NflreadpyError: on any failure from nflreadpy.load_schedules.
    """
    entry = _SCHEDULES_CACHE.get(season)
    if entry is not None and (time.monotonic() - entry["fetched_at"]) < _cache_ttl_seconds():
        return entry["df"]

    try:
        df = nflreadpy.load_schedules(seasons=season)
    except Exception as exc:
        raise NflreadpyError(f"nflreadpy load_schedules failed for season {season}: {exc}") from exc

    _SCHEDULES_CACHE[season] = {"df": df, "fetched_at": time.monotonic()}
    return df


def fetch_pbp_df(season: int) -> pl.DataFrame:
    """
    Fetch (or serve from cache) nflreadpy's play-by-play dataframe for one season.

    Raises:
        NflreadpyError: on any failure from nflreadpy.load_pbp.
    """
    entry = _PBP_CACHE.get(season)
    if entry is not None and (time.monotonic() - entry["fetched_at"]) < _cache_ttl_seconds():
        return entry["df"]

    try:
        df = nflreadpy.load_pbp(seasons=season)
    except Exception as exc:
        raise NflreadpyError(f"nflreadpy load_pbp failed for season {season}: {exc}") from exc

    _PBP_CACHE[season] = {"df": df, "fetched_at": time.monotonic()}
    return df


# ---------------------------------------------------------------------------
# Internal aggregation helpers
# ---------------------------------------------------------------------------

def _current_season() -> int:
    return int(nflreadpy.get_current_season())


def _completed_games_for_team(
    schedules_df: pl.DataFrame,
    team_abbr: str,
    season: int,
    before_date: str | date | datetime | None,
) -> pl.DataFrame:
    """
    Completed games (non-null score) for one team in one season, sorted by
    week ascending. When before_date is given (YYYY-MM-DD, or a date/datetime
    object), also excludes any game on or after that date -- see module
    docstring for why this is a secondary guard, not the primary one.

    nflreadpy's `gameday` column is always a Polars String (verified against
    both the fixtures and a live load_schedules() call -- there is no dtype
    drift there), so before_date must be coerced to a plain ISO date string
    before filtering. Passing a date/datetime object straight through makes
    Polars raise InvalidOperationError ("cannot compare 'date/datetime/time'
    to a string value"), since it refuses to compare a String column against
    a non-string literal.
    """
    df = schedules_df.filter(
        (pl.col("season") == season)
        & pl.col("home_score").is_not_null()
        & ((pl.col("home_team") == team_abbr) | (pl.col("away_team") == team_abbr))
    )
    if before_date:
        if isinstance(before_date, (date, datetime)):
            before_date = before_date.strftime("%Y-%m-%d")
        df = df.filter(pl.col("gameday") < before_date)
    return df.sort("week")


def _epa_window_stats(pbp_df: pl.DataFrame, team_abbr: str, game_ids: list[str]) -> tuple[float | None, float | None]:
    """
    Offense/defense EPA/play for the given games, pooled across all plays
    in the window (not an average of per-game averages). Filter matches
    the one verified in recon against real PHI/CHI week 3 data.
    """
    if not game_ids:
        return None, None

    plays = pbp_df.filter(
        pl.col("game_id").is_in(game_ids)
        & ((pl.col("rush_attempt") == 1) | (pl.col("pass_attempt") == 1))
        & (pl.col("qb_kneel") == 0)
        & (pl.col("qb_spike") == 0)
        & pl.col("epa").is_not_null()
    )

    off = plays.filter(pl.col("posteam") == team_abbr)
    dfn = plays.filter(pl.col("defteam") == team_abbr)

    epa_offense = float(off["epa"].mean()) if off.shape[0] else None
    epa_defense = float(dfn["epa"].mean()) if dfn.shape[0] else None
    return epa_offense, epa_defense


def _window_stats(games_df: pl.DataFrame, team_abbr: str, pbp_df: pl.DataFrame) -> WindowStats:
    """Build a WindowStats for the given (already-windowed) set of completed games."""
    games_used = games_df.shape[0]
    if games_used == 0:
        return _INSUFFICIENT_WINDOW

    points_for = 0
    points_against = 0
    wins = losses = ties = 0
    for row in games_df.to_dicts():
        is_home = row["home_team"] == team_abbr
        team_score = row["home_score"] if is_home else row["away_score"]
        opp_score = row["away_score"] if is_home else row["home_score"]
        points_for += team_score
        points_against += opp_score
        if team_score > opp_score:
            wins += 1
        elif team_score < opp_score:
            losses += 1
        else:
            ties += 1

    game_ids = games_df["game_id"].to_list()
    epa_offense, epa_defense = _epa_window_stats(pbp_df, team_abbr, game_ids)

    return WindowStats(
        status="ok",
        games_used=games_used,
        epa_offense=epa_offense,
        epa_defense=epa_defense,
        points_for_avg=points_for / games_used,
        points_against_avg=points_against / games_used,
        wins=wins, losses=losses, ties=ties,
    )


# ---------------------------------------------------------------------------
# Composite entry point
# ---------------------------------------------------------------------------

def get_team_rolling_stats(
    team_abbr: str,
    before_date: str | date | datetime | None,
    season: int | None = None,
    max_games: int = _MAX_GAMES_DEFAULT,
) -> TeamStatsResult:
    """
    Return last-4-completed-games and season-to-date rolling stats for one team.

    Args:
        team_abbr:   e.g. "BAL", "LA", "WAS" -- matches nflreadpy's own
                     abbreviations directly (confirmed identical to the
                     32-team map in injury_adapter.py; no crosswalk needed).
        before_date: the target game's own date (YYYY-MM-DD string, or a
                     date/datetime object -- either is coerced to an ISO
                     date string before comparison). Completed games on or
                     after this date are excluded. Pass None/"" only when
                     the date genuinely cannot be determined --
                     the completed-only filter still protects the target
                     game itself (see module docstring).
        season:      season year. Defaults to nflreadpy.get_current_season().
        max_games:   size of the "last N" window. Defaults to 4.

    Returns:
        TeamStatsResult. status="unavailable" (last4/season_to_date both
        None) only when the nflreadpy fetch itself fails -- a team with
        zero completed games still returns status="ok" with each window
        marked status="insufficient_data" and games_used=0, never None
        top-level and never a 0.0 stat standing in for missing data.
    """
    if season is None:
        season = _current_season()

    try:
        schedules_df = fetch_schedules_df(season)
        pbp_df = fetch_pbp_df(season)
    except NflreadpyError as exc:
        return _unavailable(team_abbr, season, str(exc))

    completed = _completed_games_for_team(schedules_df, team_abbr, season, before_date)
    season_to_date = _window_stats(completed, team_abbr, pbp_df)
    last4 = _window_stats(completed.tail(max_games), team_abbr, pbp_df)

    return TeamStatsResult(
        team_abbr=team_abbr,
        status="ok",
        season=season,
        last4=last4,
        season_to_date=season_to_date,
    )
