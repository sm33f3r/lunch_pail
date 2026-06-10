"""
scripts/build_team_stats.py

Derives per-game team statistics from the play_by_play table already in
PostgreSQL and populates team_stats.  This is a pure SQL transformation —
no new data is downloaded and the play_by_play table is never loaded into
Python memory.

All computation runs inside PostgreSQL via a single CTE-based INSERT per
season.  The script is idempotent: ON CONFLICT (game_id, team) DO NOTHING
means it is safe to re-run without creating duplicates.

Usage:
    python scripts/build_team_stats.py                 # Build all team stats
    python scripts/build_team_stats.py --fix-pre-relocation  # Fix null EPA for OAK/SD/STL

Known data limitations (documented here, not masked):
  * turnover_differential
      The stored play_by_play schema does not include an interception-result
      indicator column.  In the raw nflverse PBP, interceptions appear as
      play_type = 'pass' with a separate binary flag that was NOT included in
      our schema at ingest time.  Consequently, play_type = 'interception'
      never matches any row, and turnover_differential will be 0 for every
      game.  A future schema revision should add an `interception` INTEGER
      column and re-ingest PBP to enable real turnover tracking.

  * Fumble tracking
      The play_by_play schema has no fumble_lost / fumble_recovery columns.
      Fumble-based turnover contribution is entirely absent from
      turnover_differential.

Bug fixes applied:
  * Pre-relocation team abbreviations (OAK, SD, STL)
      Fixed in v1.1: team abbreviations normalized in CTEs before aggregation.
      Use --fix-pre-relocation flag to update existing null rows.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

DATABASE_URL = (
    f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)

SEASONS = list(range(2014, 2025))

# ---------------------------------------------------------------------------
# Core SQL
# ---------------------------------------------------------------------------
# The query uses a base CTE (season_pbp) to filter play_by_play to the target
# season once, then derives each stat family in its own CTE, and finally
# assembles everything in the INSERT … SELECT.
#
# Design notes:
#   - season_pbp selects only the columns that any downstream CTE needs,
#     keeping the intermediate result set lean.
#   - game_teams is the driving table (one row per team per game) — all stat
#     CTEs are LEFT JOINed so a team always gets a row even with NULL stats.
#   - AVG() returns NULL when no qualifying rows exist, satisfying the
#     "NULL not zero" requirement for optional stats.
#   - Third-down conversion: CASE WHEN yards_gained >= ydstogo evaluates to
#     NULL when either operand is NULL, which SUM() treats as 0 — i.e., a
#     play with unknown outcome is counted as an attempt but not a conversion.
#     This is the least-surprising approximation given the data.
#   - Turnover differential: see module-level docstring for known limitation.

_INSERT_SEASON_SQL = text("""
WITH

-- ── Base: PBP rows for this season only ───────────────────────────────────
season_pbp AS (
    SELECT
        p.game_id,
        -- Normalize team abbreviations for pre-relocation teams
        CASE p.posteam
            WHEN 'OAK' THEN 'LV'
            WHEN 'SD'  THEN 'LAC'
            WHEN 'STL' THEN 'LA'
            ELSE p.posteam
        END AS posteam,
        CASE p.defteam
            WHEN 'OAK' THEN 'LV'
            WHEN 'SD'  THEN 'LAC'
            WHEN 'STL' THEN 'LA'
            ELSE p.defteam
        END AS defteam,
        p.down,
        p.ydstogo,
        p.yardline_100,
        p.epa,
        p.yards_gained,
        p.play_type,
        p.pass_attempt,
        p.rush_attempt
    FROM play_by_play p
    WHERE p.game_id IN (
        SELECT game_id FROM games WHERE season = :season
    )
),

-- ── 1. Offensive EPA per play ─────────────────────────────────────────────
off_epa AS (
    SELECT
        game_id,
        posteam                   AS team,
        AVG(epa)                  AS offensive_epa_per_play
    FROM season_pbp
    WHERE (pass_attempt = 1 OR rush_attempt = 1)
      AND epa       IS NOT NULL
      AND posteam   IS NOT NULL
    GROUP BY game_id, posteam
),

-- ── 2. Defensive EPA per play allowed ────────────────────────────────────
def_epa AS (
    SELECT
        game_id,
        defteam                   AS team,
        AVG(epa)                  AS defensive_epa_per_play_allowed
    FROM season_pbp
    WHERE (pass_attempt = 1 OR rush_attempt = 1)
      AND epa       IS NOT NULL
      AND defteam   IS NOT NULL
    GROUP BY game_id, defteam
),

-- ── 3a. Turnovers committed (interceptions thrown by posteam) ─────────────
-- NOTE: play_type = 'interception' does not exist in the stored schema.
-- In the source nflverse data, interceptions are play_type = 'pass' with a
-- separate flag column not captured at ingest time.  These CTEs will return
-- zero rows for every game, making turnover_differential = 0 throughout.
-- See module docstring for the full explanation and remediation path.
to_committed AS (
    SELECT
        game_id,
        posteam                   AS team,
        COUNT(*)                  AS committed
    FROM season_pbp
    WHERE play_type = 'interception'
      AND posteam IS NOT NULL
    GROUP BY game_id, posteam
),

-- ── 3b. Turnovers gained (interceptions caught by defteam) ───────────────
to_gained AS (
    SELECT
        game_id,
        defteam                   AS team,
        COUNT(*)                  AS gained
    FROM season_pbp
    WHERE play_type = 'interception'
      AND defteam IS NOT NULL
    GROUP BY game_id, defteam
),

-- ── 4. Third-down conversion rate (offense) ───────────────────────────────
third_off AS (
    SELECT
        game_id,
        posteam                   AS team,
        COUNT(*)                  AS attempts,
        SUM(
            CASE WHEN yards_gained >= ydstogo THEN 1 ELSE 0 END
        )                         AS conversions
    FROM season_pbp
    WHERE down    = 3
      AND posteam IS NOT NULL
    GROUP BY game_id, posteam
),

-- ── 5. Third-down conversion rate allowed (defense) ──────────────────────
third_def AS (
    SELECT
        game_id,
        defteam                   AS team,
        COUNT(*)                  AS attempts,
        SUM(
            CASE WHEN yards_gained >= ydstogo THEN 1 ELSE 0 END
        )                         AS conversions
    FROM season_pbp
    WHERE down    = 3
      AND defteam IS NOT NULL
    GROUP BY game_id, defteam
),

-- ── 6. Red-zone efficiency (offense) — average EPA inside opp 20 ─────────
rz_off AS (
    SELECT
        game_id,
        posteam                   AS team,
        AVG(epa)                  AS red_zone_efficiency_off
    FROM season_pbp
    WHERE yardline_100 <= 20
      AND (pass_attempt = 1 OR rush_attempt = 1)
      AND posteam IS NOT NULL
    GROUP BY game_id, posteam
),

-- ── 7. Red-zone efficiency (defense) — average EPA allowed inside own 20 ──
rz_def AS (
    SELECT
        game_id,
        defteam                   AS team,
        AVG(epa)                  AS red_zone_efficiency_def
    FROM season_pbp
    WHERE yardline_100 <= 20
      AND (pass_attempt = 1 OR rush_attempt = 1)
      AND defteam IS NOT NULL
    GROUP BY game_id, defteam
),

-- ── 8. Points scored / allowed — sourced directly from games table ────────
-- home row: home_team's perspective
-- away row: away_team's perspective
game_teams AS (
    SELECT game_id, home_team AS team,
           home_score         AS points_scored,
           away_score         AS points_allowed
    FROM games
    WHERE season = :season

    UNION ALL

    SELECT game_id, away_team AS team,
           away_score         AS points_scored,
           home_score         AS points_allowed
    FROM games
    WHERE season = :season
)

-- ── Final INSERT ──────────────────────────────────────────────────────────
INSERT INTO team_stats (
    game_id,
    team,
    offensive_epa_per_play,
    defensive_epa_per_play_allowed,
    turnover_differential,
    third_down_conv_rate_off,
    third_down_conv_rate_def,
    red_zone_efficiency_off,
    red_zone_efficiency_def,
    points_scored,
    points_allowed
)
SELECT
    gt.game_id,
    gt.team,

    oe.offensive_epa_per_play,
    de.defensive_epa_per_play_allowed,

    -- Turnover differential: gained − committed.
    -- COALESCE to 0 because a LEFT JOIN miss means zero turnovers of that type.
    COALESCE(tg.gained,     0)
        - COALESCE(tc.committed, 0)               AS turnover_differential,

    -- Third-down rates: NULL when a team had no third-down plays (e.g. very
    -- short games, forfeits, or data gaps).
    CASE WHEN tdo.attempts > 0
         THEN tdo.conversions::FLOAT / tdo.attempts
         ELSE NULL
    END                                           AS third_down_conv_rate_off,

    CASE WHEN tdd.attempts > 0
         THEN tdd.conversions::FLOAT / tdd.attempts
         ELSE NULL
    END                                           AS third_down_conv_rate_def,

    rzo.red_zone_efficiency_off,
    rzd.red_zone_efficiency_def,

    gt.points_scored,
    gt.points_allowed

FROM game_teams      gt
LEFT JOIN off_epa    oe  ON oe.game_id  = gt.game_id AND oe.team  = gt.team
LEFT JOIN def_epa    de  ON de.game_id  = gt.game_id AND de.team  = gt.team
LEFT JOIN to_committed tc ON tc.game_id = gt.game_id AND tc.team  = gt.team
LEFT JOIN to_gained   tg ON tg.game_id  = gt.game_id AND tg.team  = gt.team
LEFT JOIN third_off  tdo ON tdo.game_id = gt.game_id AND tdo.team = gt.team
LEFT JOIN third_def  tdd ON tdd.game_id = gt.game_id AND tdd.team = gt.team
LEFT JOIN rz_off     rzo ON rzo.game_id = gt.game_id AND rzo.team = gt.team
LEFT JOIN rz_def     rzd ON rzd.game_id = gt.game_id AND rzd.team = gt.team

ON CONFLICT (game_id, team) DO NOTHING
""")

_COUNT_SQL = text("SELECT COUNT(*) FROM team_stats")

# ---------------------------------------------------------------------------
# Fix for pre-relocation team abbreviation bug
# ---------------------------------------------------------------------------
_COUNT_NULL_SQL = text("""
SELECT COUNT(*)
FROM team_stats
WHERE offensive_epa_per_play IS NULL
""")

_FIND_AFFECTED_ROWS_SQL = text("""
SELECT game_id, team
FROM team_stats
WHERE offensive_epa_per_play IS NULL
ORDER BY game_id, team
""")

_UPDATE_SINGLE_ROW_SQL = text("""
UPDATE team_stats SET
    offensive_epa_per_play = (
        SELECT AVG(epa) FROM play_by_play
        WHERE game_id = :game_id
        AND (pass_attempt = 1 OR rush_attempt = 1)
        AND CASE posteam
                WHEN 'OAK' THEN 'LV'
                WHEN 'SD'  THEN 'LAC'
                WHEN 'STL' THEN 'LA'
                ELSE posteam
            END = :team
        AND epa IS NOT NULL
    ),
    defensive_epa_per_play_allowed = (
        SELECT AVG(epa) FROM play_by_play
        WHERE game_id = :game_id
        AND (pass_attempt = 1 OR rush_attempt = 1)
        AND CASE defteam
                WHEN 'OAK' THEN 'LV'
                WHEN 'SD'  THEN 'LAC'
                WHEN 'STL' THEN 'LA'
                ELSE defteam
            END = :team
        AND epa IS NOT NULL
    ),
    third_down_conv_rate_off = (
        SELECT CASE WHEN COUNT(*) > 0
               THEN SUM(CASE WHEN yards_gained >= ydstogo THEN 1.0
                        ELSE 0.0 END) / COUNT(*)
               ELSE NULL END
        FROM play_by_play
        WHERE game_id = :game_id AND down = 3
        AND CASE posteam
                WHEN 'OAK' THEN 'LV'
                WHEN 'SD'  THEN 'LAC'
                WHEN 'STL' THEN 'LA'
                ELSE posteam
            END = :team
    ),
    third_down_conv_rate_def = (
        SELECT CASE WHEN COUNT(*) > 0
               THEN SUM(CASE WHEN yards_gained >= ydstogo THEN 1.0
                        ELSE 0.0 END) / COUNT(*)
               ELSE NULL END
        FROM play_by_play
        WHERE game_id = :game_id AND down = 3
        AND CASE defteam
                WHEN 'OAK' THEN 'LV'
                WHEN 'SD'  THEN 'LAC'
                WHEN 'STL' THEN 'LA'
                ELSE defteam
            END = :team
    ),
    red_zone_efficiency_off = (
        SELECT CASE WHEN COUNT(*) > 0
               THEN AVG(epa) ELSE NULL END
        FROM play_by_play
        WHERE game_id = :game_id
        AND yardline_100 <= 20
        AND (pass_attempt = 1 OR rush_attempt = 1)
        AND CASE posteam
                WHEN 'OAK' THEN 'LV'
                WHEN 'SD'  THEN 'LAC'
                WHEN 'STL' THEN 'LA'
                ELSE posteam
            END = :team
    ),
    red_zone_efficiency_def = (
        SELECT CASE WHEN COUNT(*) > 0
               THEN AVG(epa) ELSE NULL END
        FROM play_by_play
        WHERE game_id = :game_id
        AND yardline_100 <= 20
        AND (pass_attempt = 1 OR rush_attempt = 1)
        AND CASE defteam
                WHEN 'OAK' THEN 'LV'
                WHEN 'SD'  THEN 'LAC'
                WHEN 'STL' THEN 'LA'
                ELSE defteam
            END = :team
    )
WHERE game_id = :game_id AND team = :team
AND offensive_epa_per_play IS NULL
""")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_team_stats(seasons: list = SEASONS) -> int:
    engine = create_engine(DATABASE_URL, future=True)
    total_inserted = 0

    print(f"\n{'=' * 60}")
    print(f"  build_team_stats  |  seasons {min(seasons)}\u2013{max(seasons)}")
    print(f"{'=' * 60}\n")

    for season in sorted(seasons):
        try:
            with engine.begin() as conn:
                before = conn.execute(_COUNT_SQL).scalar()
                conn.execute(_INSERT_SEASON_SQL, {"season": season})
                after  = conn.execute(_COUNT_SQL).scalar()

            inserted = after - before
            total_inserted += inserted
            print(
                f"  [team_stats] {season}: {inserted:>5} rows inserted"
                f"  (running total: {total_inserted:,})",
                flush=True,
            )

        except SQLAlchemyError as exc:
            print(
                f"  [team_stats] ERROR season {season}: {exc}",
                file=sys.stderr, flush=True,
            )

    print(f"\n{'=' * 60}")
    print(f"  DONE  —  total rows inserted into team_stats: {total_inserted:,}")
    print(f"{'=' * 60}\n")

    return total_inserted


def fix_pre_relocation_stats() -> int:
    """
    Fix team_stats rows for OAK, SD, STL with null EPA values.
    Updates only the 176 affected rows without re-running full build.
    """
    engine = create_engine(DATABASE_URL, future=True)

    print(f"\n{'=' * 60}")
    print("  fix_pre_relocation_stats")
    print(f"{'=' * 60}\n")

    try:
        # Count null rows before fix
        with engine.connect() as conn:
            before_null = conn.execute(_COUNT_NULL_SQL).scalar()

        print(f"  Null EPA rows before fix: {before_null}")

        if before_null == 0:
            print("  No null EPA rows found — nothing to fix")
            return 0

        # Get list of affected rows
        with engine.connect() as conn:
            affected_rows = conn.execute(_FIND_AFFECTED_ROWS_SQL).fetchall()

        print(f"  Found {len(affected_rows)} rows to fix")

        # Process all rows in a single transaction
        updated_rows = 0
        with engine.begin() as conn:
            for i, row in enumerate(affected_rows, 1):
                game_id = row.game_id
                team = row.team

                # Update single row
                result = conn.execute(
                    _UPDATE_SINGLE_ROW_SQL,
                    {"game_id": game_id, "team": team}
                )
                if result.rowcount > 0:
                    updated_rows += 1

                # Print progress every 20 rows
                if i % 20 == 0 or i == len(affected_rows):
                    print(f"  Processed {i}/{len(affected_rows)} rows...")

        # Count null rows after fix
        with engine.connect() as conn:
            after_null = conn.execute(_COUNT_NULL_SQL).scalar()

        print(f"\n  Rows updated: {updated_rows}")
        print(f"  Null EPA rows after fix: {after_null}")

        if after_null == 0:
            print(f"\n  ✓ All {updated_rows} null rows fixed successfully")
        else:
            print(f"\n  ⚠ {after_null} null rows remain after fix")

        return updated_rows

    except SQLAlchemyError as exc:
        print(f"  ERROR during fix: {exc}", file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    # Check for command line argument to run fix
    if len(sys.argv) > 1 and sys.argv[1] == "--fix-pre-relocation":
        fix_pre_relocation_stats()
    else:
        build_team_stats()
