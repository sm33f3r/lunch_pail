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
from reporter.report.report_writer import write_all_reports
from reporter.watcher.game_assembly import get_reportable_games

_INJURY_FETCH_FAILED = InjuryResult(records=[], source="unavailable", status="unavailable")


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


def _attach_injuries(games: list[dict]) -> list[dict]:
    """Attach away_injuries/home_injuries InjuryResult objects to each game."""
    for game in games:
        game["away_injuries"] = _fetch_team_injuries(game["away_abbr"])
        game["home_injuries"] = _fetch_team_injuries(game["home_abbr"])
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
