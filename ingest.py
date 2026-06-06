"""
ingest.py — Core data ingestion for the Lunch Pail NFL prediction system.

Covers: schedules, games, rosters, players, injuries, snap_counts, depth_charts.
Play-by-play is handled separately.

Usage:
    python ingest.py                  # runs seasons 2014-2024
"""

import math
import os
import sys
from pathlib import Path

import nflreadpy
import polars as pl
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ---------------------------------------------------------------------------
# Environment / engine
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")


def get_engine():
    """Return a SQLAlchemy engine built from .env credentials."""
    url = (
        f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
        f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
    )
    return create_engine(url, future=True)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _int_or_none(val):
    """Coerce a value to int, returning None for None/NaN/non-numeric."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else int(f)
    except (TypeError, ValueError):
        return None


def _float_or_none(val):
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _clean(row: dict) -> dict:
    """Replace float NaN with None in a row dict."""
    return {
        k: (None if (isinstance(v, float) and math.isnan(v)) else v)
        for k, v in row.items()
    }


def _trunc(val, max_len: int):
    """Return val as a string truncated to max_len chars, or None."""
    if val is None:
        return None
    s = str(val)
    return s[:max_len]


def _fill_nan(df: pl.DataFrame) -> pl.DataFrame:
    """
    Prepare a DataFrame for safe conversion to Python dicts:
      1. Replace float NaN with null in every Float column.
      2. Strip timezone info from tz-aware Datetime columns — Polars panics
         on Windows when calling to_dicts() on UTC-aware datetimes because
         the system tzdata package is absent.
    """
    float_cols = [c for c in df.columns if df[c].dtype in (pl.Float32, pl.Float64)]
    if float_cols:
        df = df.with_columns([pl.col(c).fill_nan(None) for c in float_cols])

    tz_cols = [
        c for c in df.columns
        if isinstance(df[c].dtype, pl.Datetime) and df[c].dtype.time_zone
    ]
    if tz_cols:
        df = df.with_columns([
            pl.col(c).dt.replace_time_zone(None) for c in tz_cols
        ])

    return df


def _week_type(weekday, gametime) -> str:
    """
    Derive broadcast-window label from weekday and 24-hour gametime string.
    TNF = Thursday night, MNF = Monday night,
    SNF = Sunday at or after 19:00 ET, otherwise 'standard'.
    """
    if not weekday:
        return "standard"
    wd = str(weekday).strip()
    if wd == "Thursday":
        return "TNF"
    if wd == "Monday":
        return "MNF"
    if wd == "Sunday" and gametime:
        try:
            hour = int(str(gametime).split(":")[0])
            if hour >= 19:
                return "SNF"
        except (ValueError, IndexError):
            pass
    return "standard"


def _result_flag(result) -> int | None:
    """Convert home-away score differential to 1 (home win) / 0 (away win) / None."""
    if result is None:
        return None
    try:
        r = float(result)
        if math.isnan(r):
            return None
        if r > 0:
            return 1
        if r < 0:
            return 0
        return None  # tie
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Shared SQL
# ---------------------------------------------------------------------------

_PLAYERS_INSERT = text("""
    INSERT INTO players (
        player_id, player_name, position, birth_date,
        college, draft_year, draft_round, draft_pick
    ) VALUES (
        :player_id, :player_name, :position, :birth_date,
        :college, :draft_year, :draft_round, :draft_pick
    )
    ON CONFLICT (player_id) DO NOTHING
""")

_COUNT = lambda tbl: text(f"SELECT COUNT(*) FROM {tbl}")  # noqa: E731


def _player_row(player_id, player_name=None, position=None,
                birth_date=None, college=None) -> dict:
    return {
        "player_id":   _trunc(player_id, 20),
        "player_name": _trunc(player_name, 100),
        "position":    _trunc(position, 10),
        "birth_date":  birth_date,
        "college":     _trunc(college, 100),
        "draft_year":  None,
        "draft_round": None,
        "draft_pick":  None,
    }


# ---------------------------------------------------------------------------
# 1. ingest_schedules  →  schedules + games
# ---------------------------------------------------------------------------

_SCHEDULES_INSERT = text("""
    INSERT INTO schedules (
        game_id, season, week, home_team, away_team,
        game_date, weekday, gametime,
        home_score, away_score, result, overtime
    ) VALUES (
        :game_id, :season, :week, :home_team, :away_team,
        :game_date, :weekday, :gametime,
        :home_score, :away_score, :result, :overtime
    )
    ON CONFLICT (game_id) DO NOTHING
""")

_GAMES_INSERT = text("""
    INSERT INTO games (
        game_id, season, week, home_team, away_team,
        home_score, away_score, result,
        game_date, game_time, season_type, week_type
    ) VALUES (
        :game_id, :season, :week, :home_team, :away_team,
        :home_score, :away_score, :result,
        :game_date, :game_time, :season_type, :week_type
    )
    ON CONFLICT (game_id) DO NOTHING
""")


def ingest_schedules(seasons: list) -> dict:
    """
    Ingest REG-season schedule rows into both the `schedules` and `games` tables.

    nflreadpy column notes:
      - game_type  (not season_type) — filter to 'REG'
      - gameday    (not game_date)   — DATE string
      - result     Int32             — home minus away score differential
      - overtime   Int32             — 0 / 1
    """
    engine = get_engine()
    totals = {"schedules": 0, "games": 0}

    for season in sorted(seasons):
        try:
            df = nflreadpy.load_schedules(seasons=[season])
            df = df.filter(pl.col("game_type") == "REG")
            df = _fill_nan(df)

            print(f"  [schedules] {season}: {len(df):>5} rows fetched", flush=True)
            if len(df) == 0:
                continue

            rows = df.to_dicts()
            sched_rows, game_rows = [], []

            for r in rows:
                r = _clean(r)
                game_id   = r.get("game_id")
                season_v  = r.get("season")
                week      = _int_or_none(r.get("week"))
                home_team = _trunc(r.get("home_team"), 10)
                away_team = _trunc(r.get("away_team"), 10)
                game_date = r.get("gameday")          # 'YYYY-MM-DD' string
                weekday   = _trunc(r.get("weekday"), 10)
                gametime  = _trunc(r.get("gametime"), 10)
                home_scr  = _int_or_none(r.get("home_score"))
                away_scr  = _int_or_none(r.get("away_score"))
                result_raw = r.get("result")
                overtime_raw = r.get("overtime")

                sched_rows.append({
                    "game_id":    game_id,
                    "season":     season_v,
                    "week":       week,
                    "home_team":  home_team,
                    "away_team":  away_team,
                    "game_date":  game_date,
                    "weekday":    weekday,
                    "gametime":   gametime,
                    "home_score": home_scr,
                    "away_score": away_scr,
                    "result":     _float_or_none(result_raw),
                    "overtime":   bool(overtime_raw) if overtime_raw is not None else None,
                })

                game_rows.append({
                    "game_id":     game_id,
                    "season":      season_v,
                    "week":        week,
                    "home_team":   home_team,
                    "away_team":   away_team,
                    "home_score":  home_scr,
                    "away_score":  away_scr,
                    "result":      _result_flag(result_raw),
                    "game_date":   game_date,
                    "game_time":   gametime,
                    "season_type": "REG",
                    "week_type":   _week_type(weekday, gametime),
                })

            with engine.begin() as conn:
                before_s = conn.execute(_COUNT("schedules")).scalar()
                before_g = conn.execute(_COUNT("games")).scalar()
                conn.execute(_SCHEDULES_INSERT, sched_rows)
                conn.execute(_GAMES_INSERT, game_rows)
                after_s  = conn.execute(_COUNT("schedules")).scalar()
                after_g  = conn.execute(_COUNT("games")).scalar()

            ins_s = after_s - before_s
            ins_g = after_g - before_g
            totals["schedules"] += ins_s
            totals["games"]     += ins_g
            print(
                f"  [schedules] {season}: inserted {ins_s} schedules, {ins_g} games",
                flush=True,
            )

        except Exception as exc:
            print(
                f"  [schedules] ERROR season {season}: {exc}",
                file=sys.stderr, flush=True,
            )

    return totals


# ---------------------------------------------------------------------------
# 2. ingest_rosters  →  rosters + players
# ---------------------------------------------------------------------------

_ROSTERS_INSERT = text("""
    INSERT INTO rosters (
        season, week, team, player_id, player_name,
        position, depth_chart_position, jersey_number, status
    ) VALUES (
        :season, :week, :team, :player_id, :player_name,
        :position, :depth_chart_position, :jersey_number, :status
    )
    ON CONFLICT (season, week, team, player_id) DO NOTHING
""")


def ingest_rosters(seasons: list) -> dict:
    """
    Ingest REG weekly roster rows into `rosters`, seeding `players` as we go.

    nflreadpy column notes:
      - game_type  (not season_type) — filter to 'REG'
      - gsis_id    — used as player_id
      - full_name  — used as player_name
      - draft_year / draft_round / draft_pick not available; stored as NULL
    """
    engine = get_engine()
    totals = {"players": 0, "rosters": 0}

    for season in sorted(seasons):
        try:
            df = nflreadpy.load_rosters_weekly(seasons=[season])
            df = df.filter(pl.col("game_type") == "REG")
            df = _fill_nan(df)

            print(f"  [rosters]   {season}: {len(df):>5} rows fetched", flush=True)
            if len(df) == 0:
                continue

            rows = df.to_dicts()
            player_rows, roster_rows = [], []
            seen_players: set = set()

            for r in rows:
                r = _clean(r)
                player_id = _trunc(r.get("gsis_id"), 20)
                if not player_id:
                    continue

                player_name = _trunc(r.get("full_name"), 100)
                position    = _trunc(r.get("position"), 10)

                if player_id not in seen_players:
                    seen_players.add(player_id)
                    player_rows.append(_player_row(
                        player_id, player_name, position,
                        birth_date=r.get("birth_date"),
                        college=r.get("college"),
                    ))

                roster_rows.append({
                    "season":               r.get("season"),
                    "week":                 _int_or_none(r.get("week")),
                    "team":                 _trunc(r.get("team"), 10),
                    "player_id":            player_id,
                    "player_name":          player_name,
                    "position":             position,
                    "depth_chart_position": _trunc(r.get("depth_chart_position"), 20),
                    "jersey_number":        _int_or_none(r.get("jersey_number")),
                    "status":               _trunc(r.get("status"), 20),
                })

            with engine.begin() as conn:
                before_p = conn.execute(_COUNT("players")).scalar()
                before_r = conn.execute(_COUNT("rosters")).scalar()
                if player_rows:
                    conn.execute(_PLAYERS_INSERT, player_rows)
                if roster_rows:
                    conn.execute(_ROSTERS_INSERT, roster_rows)
                after_p = conn.execute(_COUNT("players")).scalar()
                after_r = conn.execute(_COUNT("rosters")).scalar()

            ins_p = after_p - before_p
            ins_r = after_r - before_r
            totals["players"] += ins_p
            totals["rosters"] += ins_r
            print(
                f"  [rosters]   {season}: inserted {ins_p} players, {ins_r} rosters",
                flush=True,
            )

        except Exception as exc:
            print(
                f"  [rosters] ERROR season {season}: {exc}",
                file=sys.stderr, flush=True,
            )

    return totals


# ---------------------------------------------------------------------------
# 3. ingest_injuries  →  injuries (+ players as needed)
# ---------------------------------------------------------------------------

_INJURIES_INSERT = text("""
    INSERT INTO injuries (
        season, week, team, player_id, player_name,
        position, report_primary_injury,
        practice_status_wed, practice_status_thu, practice_status_fri,
        game_status
    ) VALUES (
        :season, :week, :team, :player_id, :player_name,
        :position, :report_primary_injury,
        :practice_status_wed, :practice_status_thu, :practice_status_fri,
        :game_status
    )
    ON CONFLICT (season, week, team, player_id) DO NOTHING
""")


def ingest_injuries(seasons: list) -> dict:
    """
    Ingest injury report rows into `injuries`.

    nflreadpy column notes (2023 observed):
      - game_type        — filter to 'REG' (season_type not present)
      - gsis_id          — player_id
      - full_name        — player_name
      - practice_status  — most recent (Friday) practice status; per-day
                           breakdown not available — stored in practice_status_fri
      - report_status    — game designation (game_status)

    Player records are upserted first to satisfy the FK constraint for
    players who appear in injury reports but not in weekly rosters.
    """
    engine = get_engine()
    totals = {"injuries": 0}

    # Load one season to inspect columns, then reload per season in loop
    _probe = nflreadpy.load_injuries(seasons=[seasons[0]])
    cols = list(_probe.columns)
    del _probe

    has_game_type    = "game_type"    in cols
    has_season_type  = "season_type"  in cols
    player_id_col    = "gsis_id"      if "gsis_id"      in cols else None
    name_col         = "full_name"    if "full_name"     in cols else (
                       "player_name"  if "player_name"   in cols else None)
    practice_col     = "practice_status" if "practice_status" in cols else None
    game_status_col  = "report_status"   if "report_status"   in cols else (
                       "game_status"     if "game_status"      in cols else None)

    print(
        f"  [injuries]  columns: {cols}\n"
        f"  [injuries]  mapping → player_id={player_id_col}, "
        f"name={name_col}, practice_status_fri={practice_col}, "
        f"game_status={game_status_col}",
        flush=True,
    )

    for season in sorted(seasons):
        try:
            df = nflreadpy.load_injuries(seasons=[season])
            if has_game_type:
                df = df.filter(pl.col("game_type") == "REG")
            elif has_season_type:
                df = df.filter(pl.col("season_type") == "REG")

            df = _fill_nan(df)

            print(f"  [injuries]  {season}: {len(df):>5} rows fetched", flush=True)
            if len(df) == 0:
                continue

            rows = df.to_dicts()
            player_rows, inj_rows = [], []
            seen_players: set = set()

            for r in rows:
                r = _clean(r)
                player_id = (
                    _trunc(r.get(player_id_col), 20) if player_id_col else None
                )
                if not player_id:
                    continue

                player_name = _trunc(r.get(name_col), 100)  if name_col         else None
                position    = _trunc(r.get("position"), 10)
                practice_v  = _trunc(r.get(practice_col), 20) if practice_col   else None
                game_stat   = _trunc(r.get(game_status_col), 20) if game_status_col else None

                if player_id not in seen_players:
                    seen_players.add(player_id)
                    player_rows.append(
                        _player_row(player_id, player_name, position)
                    )

                inj_rows.append({
                    "season":                 r.get("season"),
                    "week":                   _int_or_none(r.get("week")),
                    "team":                   _trunc(r.get("team"), 10),
                    "player_id":              player_id,
                    "player_name":            player_name,
                    "position":               position,
                    "report_primary_injury":  _trunc(r.get("report_primary_injury"), 100),
                    "practice_status_wed":    None,   # per-day data not in nflreadpy
                    "practice_status_thu":    None,
                    "practice_status_fri":    practice_v,
                    "game_status":            game_stat,
                })

            with engine.begin() as conn:
                before = conn.execute(_COUNT("injuries")).scalar()
                if player_rows:
                    conn.execute(_PLAYERS_INSERT, player_rows)
                if inj_rows:
                    conn.execute(_INJURIES_INSERT, inj_rows)
                after = conn.execute(_COUNT("injuries")).scalar()

            ins = after - before
            totals["injuries"] += ins
            print(
                f"  [injuries]  {season}: inserted {ins} injury records",
                flush=True,
            )

        except Exception as exc:
            print(
                f"  [injuries] ERROR season {season}: {exc}",
                file=sys.stderr, flush=True,
            )

    return totals


# ---------------------------------------------------------------------------
# 4. ingest_snap_counts  →  snap_counts (+ players as needed)
# ---------------------------------------------------------------------------

_SNAP_COUNTS_INSERT = text("""
    INSERT INTO snap_counts (
        game_id, season, week, team, player_id, player_name,
        position, offense_snaps, offense_pct, defense_snaps, defense_pct
    ) VALUES (
        :game_id, :season, :week, :team, :player_id, :player_name,
        :position, :offense_snaps, :offense_pct, :defense_snaps, :defense_pct
    )
    ON CONFLICT (game_id, player_id) DO NOTHING
""")


def ingest_snap_counts(seasons: list) -> dict:
    """
    Ingest snap count rows into `snap_counts`.

    nflreadpy column notes (2023 observed):
      - player_id not available as gsis_id; only pfr_player_id is present.
        pfr_player_id is used as player_id and player records are seeded first
        so the FK constraint is satisfied. These player records are 'skeleton'
        rows that can be enriched later via a gsis↔pfr crosswalk.
      - player     — player display name (player_name)
      - game_type  — filter to 'REG' to match games table FK
    """
    engine = get_engine()
    totals = {"snap_counts": 0}

    _probe = nflreadpy.load_snap_counts(seasons=[seasons[0]])
    cols = list(_probe.columns)
    del _probe

    player_id_col   = "pfr_player_id" if "pfr_player_id" in cols else None
    player_name_col = "player"        if "player"        in cols else (
                      "player_name"   if "player_name"   in cols else None)
    has_game_type   = "game_type" in cols

    print(
        f"  [snap_counts] columns: {cols}\n"
        f"  [snap_counts] mapping → player_id={player_id_col}, "
        f"player_name={player_name_col}",
        flush=True,
    )

    for season in sorted(seasons):
        try:
            df = nflreadpy.load_snap_counts(seasons=[season])
            if has_game_type:
                df = df.filter(pl.col("game_type") == "REG")
            df = _fill_nan(df)

            print(f"  [snap_counts] {season}: {len(df):>5} rows fetched", flush=True)
            if len(df) == 0:
                continue

            rows = df.to_dicts()
            player_rows, snap_rows = [], []
            seen_players: set = set()

            for r in rows:
                r = _clean(r)
                game_id   = r.get("game_id")
                player_id = (
                    _trunc(r.get(player_id_col), 20) if player_id_col else None
                )
                if not game_id or not player_id:
                    continue

                player_name = (
                    _trunc(r.get(player_name_col), 100) if player_name_col else None
                )
                position = _trunc(r.get("position"), 10)

                if player_id not in seen_players:
                    seen_players.add(player_id)
                    player_rows.append(
                        _player_row(player_id, player_name, position)
                    )

                snap_rows.append({
                    "game_id":       game_id,
                    "season":        _int_or_none(r.get("season")),
                    "week":          _int_or_none(r.get("week")),
                    "team":          _trunc(r.get("team"), 10),
                    "player_id":     player_id,
                    "player_name":   player_name,
                    "position":      position,
                    "offense_snaps": _int_or_none(r.get("offense_snaps")),
                    "offense_pct":   _float_or_none(r.get("offense_pct")),
                    "defense_snaps": _int_or_none(r.get("defense_snaps")),
                    "defense_pct":   _float_or_none(r.get("defense_pct")),
                })

            with engine.begin() as conn:
                before = conn.execute(_COUNT("snap_counts")).scalar()
                # Seed players first so FK is satisfied within the same txn
                if player_rows:
                    conn.execute(_PLAYERS_INSERT, player_rows)
                if snap_rows:
                    conn.execute(_SNAP_COUNTS_INSERT, snap_rows)
                after = conn.execute(_COUNT("snap_counts")).scalar()

            ins = after - before
            totals["snap_counts"] += ins
            print(
                f"  [snap_counts] {season}: inserted {ins} snap count records",
                flush=True,
            )

        except Exception as exc:
            print(
                f"  [snap_counts] ERROR season {season}: {exc}",
                file=sys.stderr, flush=True,
            )

    return totals


# ---------------------------------------------------------------------------
# 5. ingest_depth_charts  →  depth_charts (+ players as needed)
# ---------------------------------------------------------------------------

_DEPTH_CHARTS_INSERT = text("""
    INSERT INTO depth_charts (
        season, week, team, player_id, player_name,
        position, depth_team, depth_position
    ) VALUES (
        :season, :week, :team, :player_id, :player_name,
        :position, :depth_team, :depth_position
    )
    ON CONFLICT (season, week, team, player_id, depth_position) DO NOTHING
""")


def ingest_depth_charts(seasons: list) -> dict:
    """
    Ingest depth chart rows into `depth_charts`.

    nflreadpy column notes (2023 observed):
      - club_code   — team abbreviation (maps to team)
      - gsis_id     — player_id (same namespace as rosters/injuries)
      - full_name   — player display name
      - game_type   — filter to 'REG'
      - depth_position — required; rows without it are skipped
    """
    engine = get_engine()
    totals = {"depth_charts": 0}

    _probe = nflreadpy.load_depth_charts(seasons=[seasons[0]])
    cols = list(_probe.columns)
    del _probe

    team_col     = "club_code" if "club_code" in cols else (
                   "team"      if "team"      in cols else None)
    name_col     = "full_name"     if "full_name"     in cols else (
                   "football_name" if "football_name" in cols else None)
    has_game_type   = "game_type"   in cols
    has_season_type = "season_type" in cols
    has_gsis        = "gsis_id"     in cols

    print(
        f"  [depth_charts] columns: {cols}\n"
        f"  [depth_charts] mapping → team={team_col}, "
        f"player_id={'gsis_id' if has_gsis else 'None'}, name={name_col}",
        flush=True,
    )

    for season in sorted(seasons):
        try:
            df = nflreadpy.load_depth_charts(seasons=[season])
            if has_game_type:
                df = df.filter(pl.col("game_type") == "REG")
            elif has_season_type:
                df = df.filter(pl.col("season_type") == "REG")
            df = _fill_nan(df)

            print(f"  [depth_charts] {season}: {len(df):>5} rows fetched", flush=True)
            if len(df) == 0:
                continue

            rows = df.to_dicts()
            player_rows, dc_rows = [], []
            seen_players: set = set()

            for r in rows:
                r = _clean(r)
                player_id    = _trunc(r.get("gsis_id"), 20) if has_gsis else None
                team         = _trunc(r.get(team_col), 10)  if team_col  else None
                depth_pos    = _trunc(r.get("depth_position"), 20)

                if not player_id or not team or not depth_pos:
                    continue

                player_name = _trunc(r.get(name_col), 100) if name_col else None
                position    = _trunc(r.get("position"), 10)

                if player_id not in seen_players:
                    seen_players.add(player_id)
                    player_rows.append(
                        _player_row(player_id, player_name, position)
                    )

                dc_rows.append({
                    "season":         _int_or_none(r.get("season")),
                    "week":           _int_or_none(r.get("week")),
                    "team":           team,
                    "player_id":      player_id,
                    "player_name":    player_name,
                    "position":       position,
                    "depth_team":     _int_or_none(r.get("depth_team")),
                    "depth_position": depth_pos,
                })

            with engine.begin() as conn:
                before = conn.execute(_COUNT("depth_charts")).scalar()
                if player_rows:
                    conn.execute(_PLAYERS_INSERT, player_rows)
                if dc_rows:
                    conn.execute(_DEPTH_CHARTS_INSERT, dc_rows)
                after = conn.execute(_COUNT("depth_charts")).scalar()

            ins = after - before
            totals["depth_charts"] += ins
            print(
                f"  [depth_charts] {season}: inserted {ins} depth chart records",
                flush=True,
            )

        except Exception as exc:
            print(
                f"  [depth_charts] ERROR season {season}: {exc}",
                file=sys.stderr, flush=True,
            )

    return totals


# ---------------------------------------------------------------------------
# run_all
# ---------------------------------------------------------------------------

def run_all(seasons) -> dict:
    """Run all five ingest functions in dependency order and print a summary."""
    seasons = sorted(seasons)
    print(f"\n{'=' * 62}")
    print(f"  Lunch Pail ingest  |  seasons {seasons[0]}–{seasons[-1]}")
    print(f"{'=' * 62}\n")

    summary: dict = {}

    print("── Schedules & Games ──────────────────────────────────────")
    summary.update(ingest_schedules(seasons))

    print("\n── Rosters & Players ──────────────────────────────────────")
    summary.update(ingest_rosters(seasons))

    print("\n── Injuries ───────────────────────────────────────────────")
    summary.update(ingest_injuries(seasons))

    print("\n── Snap Counts ────────────────────────────────────────────")
    summary.update(ingest_snap_counts(seasons))

    print("\n── Depth Charts ───────────────────────────────────────────")
    summary.update(ingest_depth_charts(seasons))

    print(f"\n{'=' * 62}")
    print("  INGEST COMPLETE  —  rows inserted per table")
    print(f"  {'─' * 38}")
    table_order = ["schedules", "games", "players", "rosters",
                   "injuries", "snap_counts", "depth_charts"]
    for tbl in table_order:
        count = summary.get(tbl, 0)
        print(f"  {tbl:<20s}  {count:>10,}")
    print(f"{'=' * 62}\n")

    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_all(range(2014, 2025))
