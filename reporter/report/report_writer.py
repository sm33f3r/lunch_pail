"""
reporter/report/report_writer.py

Formats and writes per-game markdown report files from the structured
data produced by get_reportable_games() (Step 8).

No network calls -- formats and writes only.
All generated content is ASCII-only (no Unicode symbols) so report files
are safe for downstream tooling and git diffs without encoding issues.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from reporter.config import settings
from reporter.watcher.game_assembly import get_reportable_games

# Pattern used to identify report files written by a previous run so stale
# ones can be cleaned up.  Matches: YYYY-MM-DD_ABBR_at_ABBR.md
_REPORT_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[A-Z0-9]+_at_[A-Z0-9]+\.md$")

# Prices within this of 0.500 on all outcomes are treated as untraded
# placeholders and excluded from the spread/total tables.
_PLACEHOLDER_EPSILON = 0.005

_INJURY_DISCLAIMER = """\
> ### WARNING: INJURY GATE NOT CLEARED
> **This report has NOT been checked against current NFL injury designations.**
> Player availability is a primary factor in Lunch Pail's trading gate.
> Do not use this report as a standalone trading signal until injury
> statuses (OUT / DOUBTFUL / QUESTIONABLE) have been reviewed and the
> gate has been explicitly cleared.  This is a standing system rule --
> not optional boilerplate.
"""


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

    oi = moneyline.get("open_interest", 0.0)
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

    # Injury gate -- prominent, immediately after header
    sections.append(_INJURY_DISCLAIMER)

    # Market data
    if game["moneyline"]:
        sections.append(_moneyline_section(game["moneyline"]))
    else:
        sections.append("## Moneyline\n\n_No moneyline market available._\n")

    sections.append(_spreads_section(game["spreads"]))
    sections.append(_totals_section(game["totals"]))

    # Phase 4 placeholder
    sections.append(
        "## NOT YET AVAILABLE -- Phase 4\n\n"
        "The following data will be added in Phase 4 and is absent from this report:\n\n"
        "- **Injury designations** -- OUT, DOUBTFUL, QUESTIONABLE for key players\n"
        "- **Team stats** -- recent scoring averages, offensive/defensive rankings\n"
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
    path.write_text(format_game_report(game), encoding="utf-8")
    return path


def write_all_reports() -> list[Path]:
    """
    Fetch all reportable games and write one markdown file per game.

    Stale reports from a prior run (files matching the YYYY-MM-DD_*_at_*.md
    pattern that are no longer in the current reportable set) are deleted so
    they don't linger for games that ended or dropped below threshold.

    Returns:
        List of Paths of the files written in this run.
    """
    output_dir = settings.report_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

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
