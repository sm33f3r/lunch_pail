"""
reporter/orchestrator.py

Runs the full report-writing pipeline on a schedule.

run_once()   -- one pipeline cycle; catches and logs errors, never raises.
run_forever() -- loops run_once() every settings.polling_interval_seconds.
"""

from __future__ import annotations

import time

from reporter.config import settings
from reporter.enrich.injury_adapter import InjuryResult, get_team_injuries
from reporter.enrich.team_stats_adapter import (
    NflreadpyError,
    TeamStatsResult,
    WeekContext,
    get_game_week_context,
    get_team_rolling_stats,
)
from reporter.enrich.weather_adapter import WeatherResult, get_game_weather
from reporter.report.report_writer import write_all_reports
from reporter.watcher.game_assembly import get_reportable_games

_INJURY_FETCH_FAILED = InjuryResult(records=[], source="unavailable", status="unavailable")
_TEAM_STATS_FETCH_FAILED = TeamStatsResult(
    team_abbr="", status="unavailable", season=None,
    last4=None, season_to_date=None, error="unexpected exception in orchestrator",
)
_WEATHER_FETCH_FAILED = WeatherResult(
    team_abbr="", status="unavailable", error="unexpected exception in orchestrator",
)


def _fetch_team_injuries(team_abbr: str) -> InjuryResult:
    """
    Fetch one team's injuries, never raising.

    get_team_injuries() already degrades to status="unavailable" on ESPN/
    nflverse failures; this wrapper additionally guards against an
    unexpected exception (e.g. an unrecognised team abbreviation) so a
    single team's injury lookup can never abort the game's report.
    """
    try:
        return get_team_injuries(team_abbr)
    except Exception as exc:
        print(f"[reporter] injury fetch failed for {team_abbr!r}: {exc}", flush=True)
        return _INJURY_FETCH_FAILED


def _fetch_team_rolling_stats(team_abbr: str, before_date: str) -> TeamStatsResult:
    """
    Fetch one team's rolling stats, never raising.

    get_team_rolling_stats() already degrades to status="unavailable" on
    nflreadpy fetch failures; this wrapper additionally guards against an
    unexpected exception so a single team's stats lookup can never abort
    the game's report or other games' reports.
    """
    try:
        return get_team_rolling_stats(team_abbr, before_date)
    except Exception as exc:
        print(f"[reporter] team stats fetch failed for {team_abbr!r}: {exc}", flush=True)
        return _TEAM_STATS_FETCH_FAILED


def _fetch_week_context(team_abbr: str, game_date: str) -> WeekContext | None:
    """
    Fetch the schedule-driven week context for one game, never raising.

    Returns None when the schedule lookup can't produce a trustworthy
    answer -- either the nflreadpy schedules fetch itself failed, or the
    game couldn't be matched in schedule data (e.g. a date mismatch). Per
    the "no silent defaults" rule, callers must treat None as "can't
    determine game-relative staleness" and simply omit the check, never as
    "this game is current."
    """
    try:
        ctx = get_game_week_context(team_abbr, game_date)
    except NflreadpyError as exc:
        print(f"[reporter] week context fetch failed for {team_abbr!r}: {exc}", flush=True)
        return None
    if not ctx.found:
        print(
            f"[reporter] week context: game not found in schedule for "
            f"{team_abbr!r} on {game_date!r}; skipping game-relative staleness check.",
            flush=True,
        )
        return None
    return ctx


def _fetch_game_weather(team_abbr: str, game_date: str, kickoff_utc: str | None) -> WeatherResult:
    """
    Fetch one game's weather, never raising.

    get_game_weather() already degrades to status="unavailable" on seed-
    lookup or Open-Meteo failures (and "forecast_not_yet_available" is a
    normal, expected state, not a failure); this wrapper additionally
    guards against an unexpected exception so a single game's weather
    lookup can never abort that game's report or other games' reports.
    """
    try:
        return get_game_weather(team_abbr, game_date, kickoff_utc)
    except Exception as exc:
        print(f"[reporter] weather fetch failed for {team_abbr!r}: {exc}", flush=True)
        return _WEATHER_FETCH_FAILED


def _attach_injuries(games: list[dict]) -> list[dict]:
    """Attach away_injuries/home_injuries InjuryResult objects to each game."""
    for game in games:
        game["away_injuries"] = _fetch_team_injuries(game["away_abbr"])
        game["home_injuries"] = _fetch_team_injuries(game["home_abbr"])
    return games


def _attach_team_stats(games: list[dict]) -> list[dict]:
    """
    Attach away_team_stats/home_team_stats TeamStatsResult objects to each
    game. before_date is the game's own date, so completed games on or
    after it are excluded from both teams' rolling windows.
    """
    for game in games:
        before_date = game.get("game_date") or ""
        game["away_team_stats"] = _fetch_team_rolling_stats(game["away_abbr"], before_date)
        game["home_team_stats"] = _fetch_team_rolling_stats(game["home_abbr"], before_date)
    return games


def _attach_week_context(games: list[dict]) -> list[dict]:
    """
    Attach a WeekContext (or None) to each game, used by the report writer
    for the game-relative injury staleness check -- flagging when the
    target game's own week differs from the current real week, regardless
    of whether injuries came from ESPN or nflverse (see
    reporter.enrich.team_stats_adapter.get_game_week_context).
    """
    for game in games:
        game_date = game.get("game_date") or ""
        game["week_context"] = _fetch_week_context(game["away_abbr"], game_date)
    return games


def _attach_weather(games: list[dict]) -> list[dict]:
    """
    Attach a weather WeatherResult to each game, looked up for the HOME
    team's stadium (weather is a property of the venue, not either team).
    """
    for game in games:
        game_date = game.get("game_date") or ""
        kickoff_utc = game.get("kickoff_utc")
        game["weather"] = _fetch_game_weather(game["home_abbr"], game_date, kickoff_utc)
    return games


def run_once() -> None:
    """
    Execute one full pipeline cycle: game assembly, then injury enrichment,
    then report writing.

    Every stage prints a start/end line (flush=True) so a stalled or
    failing cycle is visible in real time rather than only after the whole
    cycle finishes -- important since a hung network call would otherwise
    produce no output until it eventually times out or fails.

    Prints a summary to stdout, and swallows any exception so a transient
    failure (network outage, bad API response) does not crash the process
    or the surrounding run_forever() loop.
    """
    try:
        print("[reporter] Stage: game assembly starting...", flush=True)
        games = get_reportable_games()
        print(f"[reporter] Stage: game assembly done -- {len(games)} game(s).", flush=True)

        print("[reporter] Stage: injury fetch starting...", flush=True)
        games = _attach_injuries(games)
        print(f"[reporter] Stage: injury fetch done -- {len(games)} game(s).", flush=True)

        print("[reporter] Stage: team stats fetch starting...", flush=True)
        games = _attach_team_stats(games)
        print(f"[reporter] Stage: team stats fetch done -- {len(games)} game(s).", flush=True)

        print("[reporter] Stage: week context fetch starting...", flush=True)
        games = _attach_week_context(games)
        print(f"[reporter] Stage: week context fetch done -- {len(games)} game(s).", flush=True)

        print("[reporter] Stage: weather fetch starting...", flush=True)
        games = _attach_weather(games)
        print(f"[reporter] Stage: weather fetch done -- {len(games)} game(s).", flush=True)

        print("[reporter] Stage: report writing starting...", flush=True)
        written = write_all_reports(games)
        count = len(written)
        print(f"[reporter] Stage: report writing done -- {count} report(s) written.", flush=True)

        if count:
            print(f"[reporter] Cycle complete: {count} report(s) written.", flush=True)
            for path in written:
                print(f"  {path.name}", flush=True)
        else:
            print("[reporter] Cycle complete: no reportable games (0 reports written).", flush=True)
    except Exception as exc:
        print(f"[reporter] ERROR in cycle: {exc}", flush=True)


def run_forever() -> None:
    """
    Loop run_once() indefinitely, sleeping between cycles.

    Interval is taken from settings.polling_interval_seconds.
    KeyboardInterrupt and SystemExit propagate normally so the container
    can be stopped cleanly.
    """
    interval = settings.polling_interval_seconds
    print(f"[reporter] Starting loop (interval={interval}s). Press Ctrl-C to stop.", flush=True)
    while True:
        run_once()
        time.sleep(interval)
