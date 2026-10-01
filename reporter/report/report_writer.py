"""
reporter/report/report_writer.py

Formats and writes per-game markdown report files from the structured
data produced by get_reportable_games() (Step 8).

No network calls -- formats and writes only.
All generated content is ASCII-only (no Unicode symbols) so report files
are safe for downstream tooling and git diffs without encoding issues.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from reporter.config import settings
from reporter.enrich.injury_adapter import InjuryRecord, InjuryResult
from reporter.enrich.team_stats_adapter import TeamStatsResult, WeekContext, WindowStats
from reporter.watcher.game_assembly import get_reportable_games

# Pattern used to identify report files written by a previous run so stale
# ones can be cleaned up.  Matches: YYYY-MM-DD_ABBR_at_ABBR.md
_REPORT_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[A-Z0-9]+_at_[A-Z0-9]+\.md$")

# Prices within this of 0.500 on all outcomes are treated as untraded
# placeholders and excluded from the spread/total tables.
_PLACEHOLDER_EPSILON = 0.005

# Used when a game dict has no away_injuries/home_injuries key at all
# (e.g. callers that haven't wired the injury stage in yet).
_INJURIES_NOT_WIRED = InjuryResult(records=[], source="unavailable", status="unavailable")

# Used when a game dict has no away_team_stats/home_team_stats key at all
# (e.g. callers that haven't wired the team-stats stage in yet).
_TEAM_STATS_NOT_WIRED = TeamStatsResult(
    team_abbr="", status="unavailable", season=None,
    last4=None, season_to_date=None, error="team stats stage not wired in",
)


# ---------------------------------------------------------------------------
# Spread/total filtering and sorting helpers
# ---------------------------------------------------------------------------

def _is_placeholder(entry: dict) -> bool:
    """Return True when all prices are within epsilon of 0.5 (untraded market)."""
    return all(abs(p - 0.5) <= _PLACEHOLDER_EPSILON for p in entry["prices"].values())


def _balance_distance(entry: dict) -> float:
    """Sort key: distance of the most-extreme price from 0.5 (ascending = most balanced first)."""
    return max(abs(p - 0.5) for p in entry["prices"].values())


def _filter_and_sort_markets(entries: list[dict]) -> list[dict]:
    """
    Remove placeholder (untraded) entries and sort remaining by how balanced
    the prices are -- closest to 0.5/0.5 first.

    If filtering removes everything, the original list is returned unchanged so
    the table is never empty when there is at least one entry.
    """
    filtered = [e for e in entries if not _is_placeholder(e)]
    if not filtered:
        filtered = list(entries)
    return sorted(filtered, key=_balance_distance)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_price_row(line_label: str, outcome: str, price: float) -> str:
    implied_pct = price * 100
    return f"| {line_label} | {outcome} | {price:.3f} | {implied_pct:.1f}% |"


def _fmt_trade(trade: dict) -> str:
    side  = trade.get("side", "?")
    size  = trade.get("size",  "?")
    price = trade.get("price", "?")
    ts    = trade.get("timestamp", "")
    ts_str = f"  _{ts}_" if ts else ""
    return f"  - {side} {size} @ {price}{ts_str}"


def _moneyline_section(moneyline: dict) -> str:
    lines = ["## Moneyline\n"]
    lines.append("| Outcome | Price | Implied Prob |")
    lines.append("|---------|-------|-------------|")
    for outcome, price in moneyline["prices"].items():
        implied_pct = price * 100
        lines.append(f"| {outcome} | {price:.3f} | {implied_pct:.1f}% |")
    lines.append("")

    oi_status = moneyline.get("open_interest_status", "ok")
    oi = moneyline.get("open_interest")
    if oi_status == "unavailable" or oi is None:
        reason = moneyline.get("open_interest_reason") or "no data returned"
        lines.append(f"**Open Interest:** UNAVAILABLE -- {reason}\n")
    else:
        lines.append(f"**Open Interest:** ${oi:,.2f}\n")

    trades = moneyline.get("recent_trades") or []
    if trades:
        lines.append("**Recent Trades** (most recent first, up to 5):\n")
        for t in trades[:5]:
            lines.append(_fmt_trade(t))
        lines.append("")
    else:
        lines.append("**Recent Trades:** none recorded yet\n")

    return "\n".join(lines)


def _spreads_section(spreads: list[dict]) -> str:
    if not spreads:
        return "## Spreads\n\n_No spread markets available._\n"
    visible = _filter_and_sort_markets(spreads)
    lines = ["## Spreads\n"]
    lines.append("| Line | Outcome | Price | Implied Prob |")
    lines.append("|------|---------|-------|-------------|")
    for entry in visible:
        line_label = entry.get("line", "")
        for outcome, price in entry["prices"].items():
            lines.append(_fmt_price_row(line_label, outcome, price))
    lines.append("")
    return "\n".join(lines)


def _totals_section(totals: list[dict]) -> str:
    if not totals:
        return "## Totals\n\n_No totals markets available._\n"
    visible = _filter_and_sort_markets(totals)
    lines = ["## Totals\n"]
    lines.append("| Line | Outcome | Price | Implied Prob |")
    lines.append("|------|---------|-------|-------------|")
    for entry in visible:
        line_label = entry.get("line", "")
        for outcome, price in entry["prices"].items():
            lines.append(_fmt_price_row(line_label, outcome, price))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Injury report section
# ---------------------------------------------------------------------------

def _fmt_injury_record(rec: InjuryRecord) -> str:
    """Render one player's injury row. ASCII-only, one bullet plus sub-bullets."""
    header = f"- {rec.player_name}"
    if rec.position:
        header += f" ({rec.position})"
    header += f" -- {rec.designation}"

    detail_bits = [b for b in (rec.injury_type, rec.location, rec.side) if b]
    if detail_bits:
        header += f" [{', '.join(detail_bits)}]"

    sub_lines = []

    short = (rec.short_comment or "").strip()
    if short and short.lower() != (rec.designation or "").strip().lower():
        sub_lines.append(f"  - {short}")

    if rec.return_date:
        sub_lines.append(f"  - Return date: {rec.return_date}")

    # practice_status is only meaningful (and only ever populated) for nflverse
    # records -- ESPN has no structured practice-participation field.
    if rec.source == "nflverse" and rec.practice_status:
        sub_lines.append(f"  - Practice status: {rec.practice_status}")

    return "\n".join([header, *sub_lines])


def _format_team_injury_section(team_name: str, team_abbr: str, result: InjuryResult) -> str:
    lines = [f"### {team_name} ({team_abbr})\n"]
    lines.append(f"**Source:** {result.source}")

    if result.source == "espn":
        lines.append("_Practice status is not available from this source._")
        if result.as_of:
            lines.append(f"_As of: {result.as_of}_")
        if result.possibly_incomplete:
            raw_count = len(result.records) + result.failed_count
            lines.append(
                f"_NOTE: this team's injury list may be incomplete -- the ESPN "
                f"feed returned exactly {raw_count} records, which may indicate "
                f"a feed-side limit. Designations beyond this list, if any, are "
                f"not visible here._"
            )

    if result.source == "nflverse":
        if result.nflverse_season is not None and result.nflverse_week is not None:
            lines.append(f"_Data: nflverse, {result.nflverse_season} week {result.nflverse_week}_")
        else:
            lines.append("_Data: nflverse (week unknown)_")
        if result.stale:
            lines.append(
                "_WARNING: this is NOT current-week data -- injury "
                "designations may be out of date._"
            )

    if result.failed_count:
        lines.append(
            f"_{result.failed_count} injury record(s) for this team could not be parsed._"
        )

    lines.append("")

    if result.status == "unavailable":
        lines.append(
            "**UNAVAILABLE** -- both ESPN and nflverse injury fetches failed "
            "for this team. Treat this team's injury status as unknown.\n"
        )
        return "\n".join(lines)

    if result.status == "no_designations" or not result.records:
        lines.append("No injuries reported.\n")
        return "\n".join(lines)

    for rec in result.records:
        lines.append(_fmt_injury_record(rec))
    lines.append("")

    return "\n".join(lines)


def _game_relative_staleness_note(week_ctx: WeekContext | None) -> str | None:
    """
    Render the game-relative injury staleness note, or None when it doesn't
    apply.

    This is distinct from (and can coexist with) the per-team nflverse
    "NOT current-week data" warning in _format_team_injury_section: that
    one flags when the nflverse FALLBACK's own week isn't the current week;
    this one flags when the GAME ITSELF is weeks away from "now," which
    matters regardless of source -- ESPN's league injury payload is always
    current-week data, so a future game (caught by the volume filter well
    ahead of kickoff) gets designations that may be stale relative to that
    specific game's actual date even though ESPN itself isn't "stale" in
    any general sense.

    week_ctx is None when the schedule lookup couldn't produce a
    trustworthy answer (fetch failure or game not found) -- per the "no
    silent defaults" rule, that means no claim is made either way, so no
    note is rendered rather than guessing.
    """
    if week_ctx is None or week_ctx.game_week is None:
        return None
    if week_ctx.game_week == week_ctx.current_week:
        return None
    gap = abs(week_ctx.game_week - week_ctx.current_week)
    return (
        f"**NOTE:** This game is {gap} week(s) out from the current injury data "
        f"(game: season {week_ctx.season} week {week_ctx.game_week}; current week "
        f"{week_ctx.current_week}). ESPN designations reflect the current week, "
        "not necessarily gameday.\n"
    )


def _injury_report_section(game: dict) -> str:
    """
    Render the full injury report section for both teams of a game.

    game["away_injuries"] / game["home_injuries"] are InjuryResult objects
    attached by the orchestrator between game assembly and report writing.
    Absent keys are treated as unavailable rather than causing a KeyError,
    so a per-source degradation never blanks the whole section.

    Each team's "as of" / data-recency line is per-team (not a shared
    report-generation timestamp): ESPN sections use the payload's own
    timestamp, and nflverse sections state the season/week actually
    returned plus a staleness warning when that week isn't current --
    report generation time is never a substitute for either.

    game["week_context"] (a WeekContext or None, attached by the
    orchestrator) drives a separate, game-level staleness note that fires
    regardless of source -- see _game_relative_staleness_note().
    """
    away_result: InjuryResult = game.get("away_injuries") or _INJURIES_NOT_WIRED
    home_result: InjuryResult = game.get("home_injuries") or _INJURIES_NOT_WIRED

    lines = ["## Injury Report\n"]

    note = _game_relative_staleness_note(game.get("week_context"))
    if note:
        lines.append(note)

    lines.append(_format_team_injury_section(game["away_team"], game["away_abbr"], away_result))
    lines.append(_format_team_injury_section(game["home_team"], game["home_abbr"], home_result))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Team performance (rolling stats) section
# ---------------------------------------------------------------------------

def _fmt_window_stats(label: str, window: WindowStats) -> list[str]:
    """Render one window (last-4 or season-to-date) as a labeled bullet block."""
    if window.status == "insufficient_data":
        return [f"**{label}:** INSUFFICIENT DATA -- 0 completed games so far this season."]

    lines = [f"**{label}** (n={window.games_used} completed game(s)):"]
    epa_off = f"{window.epa_offense:+.3f}" if window.epa_offense is not None else "N/A"
    epa_def = f"{window.epa_defense:+.3f}" if window.epa_defense is not None else "N/A"
    lines.append(f"  - EPA/play offense: {epa_off}")
    lines.append(f"  - EPA/play defense (allowed): {epa_def}")
    lines.append(f"  - Points for (avg): {window.points_for_avg:.1f}")
    lines.append(f"  - Points against (avg): {window.points_against_avg:.1f}")
    record = f"{window.wins}-{window.losses}"
    if window.ties:
        record += f"-{window.ties}"
    lines.append(f"  - Record: {record}")
    return lines


def _format_team_stats_section(team_name: str, team_abbr: str, result: TeamStatsResult) -> str:
    lines = [f"### {team_name} ({team_abbr})\n"]

    if result.status == "unavailable":
        reason = result.error or "no data returned"
        lines.append(
            f"**UNAVAILABLE** -- nflreadpy fetch failed for this team ({reason}). "
            "Treat rolling stats as unknown.\n"
        )
        return "\n".join(lines)

    lines.extend(_fmt_window_stats("Last 4 Games", result.last4))
    lines.append("")
    lines.extend(_fmt_window_stats("Season-to-Date", result.season_to_date))
    lines.append("")
    return "\n".join(lines)


def _team_performance_section(game: dict) -> str:
    """
    Render the full team-performance section for both teams of a game.

    game["away_team_stats"] / game["home_team_stats"] are TeamStatsResult
    objects attached by the orchestrator between injury enrichment and
    report writing. Absent keys are treated as unavailable rather than
    causing a KeyError, matching the injury section's pattern.
    """
    away_result: TeamStatsResult = game.get("away_team_stats") or _TEAM_STATS_NOT_WIRED
    home_result: TeamStatsResult = game.get("home_team_stats") or _TEAM_STATS_NOT_WIRED

    lines = ["## Team Performance\n"]
    lines.append(_format_team_stats_section(game["away_team"], game["away_abbr"], away_result))
    lines.append(_format_team_stats_section(game["home_team"], game["home_abbr"], home_result))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def format_game_report(game: dict) -> str:
    """
    Build the full markdown content for one game report.

    Args:
        game: A game dict as returned by get_reportable_games().

    Returns:
        ASCII-only markdown string ready to write to disk.
    """
    away    = game["away_team"]
    home    = game["home_team"]
    date    = game["game_date"]
    volume  = game["volume"]
    url     = game["event_url"]
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    sections = []

    # Header
    sections.append(f"# {away} @ {home}\n\n**Date:** {date}  \n**24h Volume:** ${volume:,.2f}\n")

    # Injury report -- prominent, immediately after header. This is the
    # standing injury-report-gate: both teams' injury designations must
    # appear here, sourced live (ESPN, with nflverse fallback).
    sections.append(_injury_report_section(game))

    # Market data
    if game["moneyline"]:
        sections.append(_moneyline_section(game["moneyline"]))
    else:
        sections.append("## Moneyline\n\n_No moneyline market available._\n")

    sections.append(_spreads_section(game["spreads"]))
    sections.append(_totals_section(game["totals"]))

    # Team performance -- last-4-games and season-to-date rolling stats
    # (EPA/play offense+defense, points for/against, win-loss record),
    # sourced live from nflreadpy (see reporter/enrich/team_stats_adapter.py).
    sections.append(_team_performance_section(game))

    # Phase 4 placeholder
    sections.append(
        "## NOT YET AVAILABLE -- Phase 4\n\n"
        "The following data will be added in Phase 4 and is absent from this report:\n\n"
        "- **Weather conditions** -- for outdoor venues\n"
        "- **Line movement history** -- opening line vs. current spread/total\n"
    )

    # Footer
    sections.append(f"---\n\n_Generated: {now_utc}_  \n_Source: {url}_\n")

    return "\n".join(sections)


def write_game_report(game: dict, output_dir: Path) -> Path:
    """
    Format and write one game's report to disk.

    Filename: {game_date}_{away_abbr}_at_{home_abbr}.md

    Args:
        game:       A game dict from get_reportable_games().
        output_dir: Directory to write into (created if absent).

    Returns:
        Path of the written file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{game['game_date']}_{game['away_abbr']}_at_{game['home_abbr']}.md"
    path = output_dir / filename
    content = format_game_report(game)
    # Write to a temp file in the same directory then rename so a crash mid-write
    # never leaves a truncated report at the final path.
    fd, tmp = tempfile.mkstemp(dir=output_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_all_reports(games: list[dict] | None = None) -> list[Path]:
    """
    Write one markdown file per reportable game.

    Args:
        games: Pre-assembled, injury-enriched games (as produced by the
               orchestrator's game-assembly + injury-fetch stages). When
               omitted, games are fetched directly via get_reportable_games()
               with no injury data attached (injury sections render as
               unavailable) -- this keeps direct/standalone calls working.

    Stale reports from a prior run (files matching the YYYY-MM-DD_*_at_*.md
    pattern that are no longer in the current reportable set) are deleted so
    they don't linger for games that ended or dropped below threshold.

    Returns:
        List of Paths of the files written in this run.
    """
    output_dir = settings.report_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    if games is None:
        games = get_reportable_games()

    # Build expected filenames for this run
    expected = {
        f"{g['game_date']}_{g['away_abbr']}_at_{g['home_abbr']}.md"
        for g in games
    }

    # Remove stale files that look like prior-run reports but aren't expected now
    for existing in output_dir.iterdir():
        if existing.is_file() and _REPORT_FILENAME_RE.match(existing.name):
            if existing.name not in expected:
                existing.unlink()

    written = []
    for game in games:
        path = write_game_report(game, output_dir)
        written.append(path)

    return written
