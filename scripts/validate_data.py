"""
scripts/validate_data.py

Automated data quality checks for the Lunch Pail NFL prediction system.
Runs 10 validation checks across all tables (2014-2024 seasons).
Exits with non-zero code if any CRITICAL check fails.

Usage:
    python scripts/validate_data.py
"""

import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ---------------------------------------------------------------------------
# Environment / engine
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "lunch_pail")
DB_USER = os.environ.get("DB_USER", "lunch_pail_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "newpassword")

DATABASE_URL = (
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# Current NFL teams (as of 2024)
CURRENT_NFL_TEAMS = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LV", "LAC", "LA", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WAS"
]

# Season range
MIN_SEASON = 2014
MAX_SEASON = 2024
EXPECTED_SEASON_COUNT = MAX_SEASON - MIN_SEASON + 1

# ---------------------------------------------------------------------------
# Validation result tracking
# ---------------------------------------------------------------------------
class ValidationResult:
    def __init__(self, check_num: int, name: str, critical: bool):
        self.check_num = check_num
        self.name = name
        self.critical = critical
        self.passed = False
        self.warning = False
        self.failed = False
        self.message = ""
        self.details = {}

    def set_pass(self, message: str = "", details: Dict = None):
        self.passed = True
        self.message = message
        self.details = details or {}

    def set_warn(self, message: str, details: Dict = None):
        self.warning = True
        self.message = message
        self.details = details or {}

    def set_fail(self, message: str, details: Dict = None):
        self.failed = True
        self.message = message
        self.details = details or {}

    def get_status(self):
        if self.failed:
            return "FAIL"
        elif self.warning:
            return "WARN"
        else:
            return "PASS"

# ---------------------------------------------------------------------------
# Check 1 — Season coverage (CRITICAL)
# ---------------------------------------------------------------------------
def check_season_coverage(engine) -> ValidationResult:
    """Verify all 11 seasons (2014-2024) are present in games table."""
    result = ValidationResult(1, "Season coverage", critical=True)

    try:
        with engine.connect() as conn:
            # Get distinct seasons and game counts
            query = text("""
                SELECT season, COUNT(*) as game_count
                FROM games
                WHERE season BETWEEN :min AND :max
                GROUP BY season
                ORDER BY season
            """)
            rows = conn.execute(query, {"min": MIN_SEASON, "max": MAX_SEASON}).fetchall()

            seasons = {row.season: row.game_count for row in rows}
            missing_seasons = [s for s in range(MIN_SEASON, MAX_SEASON + 1) if s not in seasons]

            # Check season count
            if len(seasons) != EXPECTED_SEASON_COUNT:
                result.set_fail(
                    f"Missing seasons: Expected {EXPECTED_SEASON_COUNT} seasons, found {len(seasons)}",
                    {"missing_seasons": missing_seasons, "found_seasons": list(seasons.keys())}
                )
                return result

            # Check game counts per season
            low_count_seasons = []
            for season, count in seasons.items():
                if count < 200:  # Minimum expected games per season
                    low_count_seasons.append((season, count))

            if low_count_seasons:
                result.set_fail(
                    f"Seasons with low game counts (<200): {low_count_seasons}",
                    {"low_count_seasons": low_count_seasons, "all_seasons": seasons}
                )
                return result

            # All good
            total_games = sum(seasons.values())
            result.set_pass(
                f"All {EXPECTED_SEASON_COUNT} seasons present with {total_games:,} total games",
                {"seasons": seasons, "total_games": total_games}
            )

    except SQLAlchemyError as e:
        result.set_fail(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Check 2 — Game record completeness (CRITICAL)
# ---------------------------------------------------------------------------
def check_game_completeness(engine) -> ValidationResult:
    """Total games must be between 2,800 and 3,000."""
    result = ValidationResult(2, "Game record completeness", critical=True)

    try:
        with engine.connect() as conn:
            # Total games
            total_query = text("SELECT COUNT(*) FROM games WHERE season BETWEEN :min AND :max")
            total_games = conn.execute(total_query, {"min": MIN_SEASON, "max": MAX_SEASON}).scalar()

            # Games missing from schedules
            missing_sched_query = text("""
                SELECT COUNT(*)
                FROM games g
                LEFT JOIN schedules s ON g.game_id = s.game_id
                WHERE g.season BETWEEN :min AND :max AND s.game_id IS NULL
            """)
            missing_sched = conn.execute(missing_sched_query, {"min": MIN_SEASON, "max": MAX_SEASON}).scalar()

            # Games missing team_stats (should have 2 per game)
            missing_stats_query = text("""
                SELECT g.game_id
                FROM games g
                LEFT JOIN (
                    SELECT game_id, COUNT(*) as stat_count
                    FROM team_stats
                    GROUP BY game_id
                ) ts ON g.game_id = ts.game_id
                WHERE g.season BETWEEN :min AND :max
                  AND (ts.stat_count IS NULL OR ts.stat_count != 2)
                LIMIT 10
            """)
            missing_stats = conn.execute(missing_stats_query, {"min": MIN_SEASON, "max": MAX_SEASON}).fetchall()

            # Check total game count
            if total_games < 2800 or total_games > 3000:
                result.set_fail(
                    f"Total games out of range: {total_games} (expected 2,800-3,000)",
                    {"total_games": total_games, "missing_in_schedules": missing_sched,
                     "games_missing_stats": [row.game_id for row in missing_stats]}
                )
                return result

            # Check schedules
            if missing_sched > 0:
                result.set_fail(
                    f"{missing_sched} games missing from schedules table",
                    {"total_games": total_games, "missing_in_schedules": missing_sched}
                )
                return result

            # Check team_stats
            if missing_stats:
                result.set_fail(
                    f"{len(missing_stats)} games missing or incomplete team_stats rows",
                    {"total_games": total_games, "sample_missing": [row.game_id for row in missing_stats]}
                )
                return result

            result.set_pass(
                f"Game completeness: {total_games:,} total games, all have schedules and team_stats",
                {"total_games": total_games}
            )

    except SQLAlchemyError as e:
        result.set_fail(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Check 3 — Play-by-play coverage (CRITICAL)
# ---------------------------------------------------------------------------
def check_pbp_coverage(engine) -> ValidationResult:
    """Every game_id in games must have at least 1 play in play_by_play."""
    result = ValidationResult(3, "Play-by-play coverage", critical=True)

    try:
        with engine.connect() as conn:
            # Games missing PBP
            missing_pbp_query = text("""
                SELECT COUNT(*)
                FROM games g
                LEFT JOIN (
                    SELECT DISTINCT game_id FROM play_by_play
                ) pbp ON g.game_id = pbp.game_id
                WHERE g.season BETWEEN :min AND :max AND pbp.game_id IS NULL
            """)
            missing_pbp = conn.execute(missing_pbp_query, {"min": MIN_SEASON, "max": MAX_SEASON}).scalar()

            # Total PBP rows
            total_pbp_query = text("SELECT COUNT(*) FROM play_by_play")
            total_pbp = conn.execute(total_pbp_query).scalar()

            # EPA null rate
            epa_null_query = text("""
                SELECT
                    COUNT(*) as total_rows,
                    COUNT(CASE WHEN epa IS NULL THEN 1 END) as null_epa,
                    ROUND(100.0 * COUNT(CASE WHEN epa IS NULL THEN 1 END) / COUNT(*), 2) as null_pct
                FROM play_by_play
            """)
            epa_null_result = conn.execute(epa_null_query).fetchone()

            # Check missing PBP
            if missing_pbp > 0:
                result.set_fail(
                    f"{missing_pbp} games missing play-by-play data",
                    {"missing_pbp_games": missing_pbp, "total_pbp_rows": total_pbp}
                )
                return result

            # Check total PBP rows
            if total_pbp < 480000 or total_pbp > 560000:
                result.set_warn(
                    f"Total PBP rows: {total_pbp:,} (expected 480,000-560,000)",
                    {"total_pbp_rows": total_pbp}
                )

            # Check EPA null rate
            null_pct = epa_null_result.null_pct if epa_null_result else 100
            if null_pct >= 20:
                result.set_fail(
                    f"EPA null rate too high: {null_pct}% (max 20%)",
                    {"total_rows": epa_null_result.total_rows, "null_epa": epa_null_result.null_epa, "null_pct": null_pct}
                )
                return result

            if null_pct > 5:
                result.set_warn(
                    f"EPA null rate: {null_pct}% (above 5% threshold)",
                    {"total_rows": epa_null_result.total_rows, "null_epa": epa_null_result.null_epa, "null_pct": null_pct}
                )

            result.set_pass(
                f"PBP coverage: {total_pbp:,} rows, EPA null rate: {null_pct}%",
                {"total_pbp_rows": total_pbp, "epa_null_rate": null_pct}
            )

    except SQLAlchemyError as e:
        result.set_fail(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Check 4 — Injury data coverage (WARNING)
# ---------------------------------------------------------------------------
def check_injury_coverage(engine) -> ValidationResult:
    """Injury records must exist for all 11 seasons."""
    result = ValidationResult(4, "Injury data coverage", critical=False)

    try:
        with engine.connect() as conn:
            # Distinct seasons with injury data
            seasons_query = text("""
                SELECT DISTINCT season
                FROM injuries
                WHERE season BETWEEN :min AND :max
                ORDER BY season
            """)
            seasons = [row.season for row in conn.execute(seasons_query, {"min": MIN_SEASON, "max": MAX_SEASON}).fetchall()]

            # Game status null rate per season
            status_null_query = text("""
                SELECT
                    season,
                    COUNT(*) as total_rows,
                    COUNT(CASE WHEN game_status IS NULL THEN 1 END) as null_status,
                    ROUND(100.0 * COUNT(CASE WHEN game_status IS NULL THEN 1 END) / COUNT(*), 2) as null_pct
                FROM injuries
                WHERE season BETWEEN :min AND :max
                GROUP BY season
                ORDER BY season
            """)
            status_null_results = conn.execute(status_null_query, {"min": MIN_SEASON, "max": MAX_SEASON}).fetchall()

            missing_seasons = [s for s in range(MIN_SEASON, MAX_SEASON + 1) if s not in seasons]

            details = {
                "found_seasons": seasons,
                "missing_seasons": missing_seasons,
                "game_status_null_rates": {row.season: {"total": row.total_rows, "null": row.null_status, "pct": row.null_pct}
                                          for row in status_null_results}
            }

            if missing_seasons:
                result.set_warn(
                    f"Injury data missing for seasons: {missing_seasons}",
                    details
                )
            else:
                result.set_pass(
                    f"Injury data present for all {len(seasons)} seasons",
                    details
                )

    except SQLAlchemyError as e:
        result.set_warn(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Check 5 — Snap count coverage (WARNING)
# ---------------------------------------------------------------------------
def check_snap_count_coverage(engine) -> ValidationResult:
    """Snap count records must exist for all 11 seasons."""
    result = ValidationResult(5, "Snap count coverage", critical=False)

    try:
        with engine.connect() as conn:
            # Distinct seasons with snap counts
            seasons_query = text("""
                SELECT DISTINCT season
                FROM snap_counts
                WHERE season BETWEEN :min AND :max
                ORDER BY season
            """)
            seasons = [row.season for row in conn.execute(seasons_query, {"min": MIN_SEASON, "max": MAX_SEASON}).fetchall()]

            # Average rows per season
            avg_rows_query = text("""
                SELECT
                    season,
                    COUNT(*) as row_count
                FROM snap_counts
                WHERE season BETWEEN :min AND :max
                GROUP BY season
                ORDER BY season
            """)
            row_counts = conn.execute(avg_rows_query, {"min": MIN_SEASON, "max": MAX_SEASON}).fetchall()

            total_rows = sum(row.row_count for row in row_counts)
            avg_rows = total_rows / len(row_counts) if row_counts else 0

            missing_seasons = [s for s in range(MIN_SEASON, MAX_SEASON + 1) if s not in seasons]

            details = {
                "found_seasons": seasons,
                "missing_seasons": missing_seasons,
                "row_counts_per_season": {row.season: row.row_count for row in row_counts},
                "total_rows": total_rows,
                "avg_rows_per_season": avg_rows
            }

            if missing_seasons:
                result.set_warn(
                    f"Snap count data missing for seasons: {missing_seasons}",
                    details
                )
            else:
                result.set_pass(
                    f"Snap count data present for all {len(seasons)} seasons, average {avg_rows:.0f} rows per season",
                    details
                )

    except SQLAlchemyError as e:
        result.set_warn(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Check 6 — Weather completeness (CRITICAL)
# ---------------------------------------------------------------------------
def check_weather_completeness(engine) -> ValidationResult:
    """Every game_id in games must have exactly one row in weather."""
    result = ValidationResult(6, "Weather completeness", critical=True)

    try:
        with engine.connect() as conn:
            # Games missing weather
            missing_weather_query = text("""
                SELECT COUNT(*)
                FROM games g
                LEFT JOIN weather w ON g.game_id = w.game_id
                WHERE g.season BETWEEN :min AND :max AND w.game_id IS NULL
            """)
            missing_weather = conn.execute(missing_weather_query, {"min": MIN_SEASON, "max": MAX_SEASON}).scalar()

            # Games with multiple weather rows
            duplicate_weather_query = text("""
                SELECT game_id, COUNT(*) as count
                FROM weather
                GROUP BY game_id
                HAVING COUNT(*) > 1
                LIMIT 10
            """)
            duplicate_weather = conn.execute(duplicate_weather_query).fetchall()

            # Outdoor games with null temperature
            outdoor_null_temp_query = text("""
                SELECT COUNT(*)
                FROM weather
                WHERE is_outdoor = true AND temperature_f IS NULL
            """)
            outdoor_null_temp = conn.execute(outdoor_null_temp_query).scalar()

            # Indoor row count
            indoor_count_query = text("""
                SELECT COUNT(*)
                FROM weather
                WHERE is_outdoor = false
            """)
            indoor_count = conn.execute(indoor_count_query).scalar()

            # Check missing weather
            if missing_weather > 0:
                result.set_fail(
                    f"{missing_weather} games missing weather data",
                    {"missing_weather": missing_weather}
                )
                return result

            # Check duplicate weather
            if duplicate_weather:
                result.set_fail(
                    f"{len(duplicate_weather)} games have multiple weather rows",
                    {"sample_duplicates": [row.game_id for row in duplicate_weather]}
                )
                return result

            # Check outdoor null temperature
            if outdoor_null_temp > 0:
                result.set_fail(
                    f"{outdoor_null_temp} outdoor games have null temperature",
                    {"outdoor_null_temp": outdoor_null_temp}
                )
                return result

            # Check indoor count
            if indoor_count < 800 or indoor_count > 900:
                result.set_warn(
                    f"Indoor game count: {indoor_count} (expected 800-900)",
                    {"indoor_count": indoor_count}
                )

            result.set_pass(
                f"Weather completeness: All games covered, {indoor_count} indoor games",
                {"indoor_count": indoor_count, "outdoor_null_temp": outdoor_null_temp}
            )

    except SQLAlchemyError as e:
        result.set_fail(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Check 7 — Stadium coverage (CRITICAL)
# ---------------------------------------------------------------------------
def check_stadium_coverage(engine) -> ValidationResult:
    """All 32 current NFL teams must be present in stadiums table."""
    result = ValidationResult(7, "Stadium coverage", critical=True)

    try:
        with engine.connect() as conn:
            # Teams in stadiums table
            stadium_teams_query = text("""
                SELECT DISTINCT team
                FROM stadiums
                WHERE season_end IS NULL OR season_end >= :current_year
            """)
            stadium_teams = [row.team for row in conn.execute(stadium_teams_query, {"current_year": MAX_SEASON}).fetchall()]

            # Games with unmatched stadium (using ingest_weather.py alias logic)
            unmatched_games_query = text("""
                SELECT COUNT(*)
                FROM games g
                LEFT JOIN stadiums s ON s.team = CASE g.home_team
                    WHEN 'OAK' THEN 'LV'
                    WHEN 'SD' THEN 'LAC'
                    WHEN 'STL' THEN 'LA'
                    ELSE g.home_team
                END AND g.season >= s.season_start
                AND (s.season_end IS NULL OR g.season <= s.season_end)
                WHERE g.season BETWEEN :min AND :max AND s.stadium_id IS NULL
            """)
            unmatched_games = conn.execute(unmatched_games_query, {"min": MIN_SEASON, "max": MAX_SEASON}).scalar()

            missing_teams = [team for team in CURRENT_NFL_TEAMS if team not in stadium_teams]

            # Check missing teams
            if missing_teams:
                result.set_fail(
                    f"Missing teams in stadiums table: {missing_teams}",
                    {"missing_teams": missing_teams, "stadium_teams": stadium_teams}
                )
                return result

            # Check unmatched games
            if unmatched_games > 0:
                result.set_fail(
                    f"{unmatched_games} games have unmatched stadium",
                    {"unmatched_games": unmatched_games}
                )
                return result

            result.set_pass(
                f"All {len(CURRENT_NFL_TEAMS)} NFL teams present in stadiums table, no unmatched games",
                {"stadium_teams": stadium_teams}
            )

    except SQLAlchemyError as e:
        result.set_fail(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Check 8 — Player ID integrity (WARNING)
# ---------------------------------------------------------------------------
def check_player_id_integrity(engine) -> ValidationResult:
    """Report counts of null player_id in various tables."""
    result = ValidationResult(8, "Player ID integrity", critical=False)

    try:
        with engine.connect() as conn:
            # Roster null player_id
            roster_null_query = text("""
                SELECT COUNT(*)
                FROM rosters
                WHERE player_id IS NULL AND season BETWEEN :min AND :max
            """)
            roster_null = conn.execute(roster_null_query, {"min": MIN_SEASON, "max": MAX_SEASON}).scalar()

            # Injury null player_id
            injury_null_query = text("""
                SELECT COUNT(*)
                FROM injuries
                WHERE player_id IS NULL AND season BETWEEN :min AND :max
            """)
            injury_null = conn.execute(injury_null_query, {"min": MIN_SEASON, "max": MAX_SEASON}).scalar()

            # Snap count null player_id
            snap_null_query = text("""
                SELECT COUNT(*)
                FROM snap_counts
                WHERE player_id IS NULL AND season BETWEEN :min AND :max
            """)
            snap_null = conn.execute(snap_null_query, {"min": MIN_SEASON, "max": MAX_SEASON}).scalar()

            details = {
                "roster_null_player_id": roster_null,
                "injury_null_player_id": injury_null,
                "snap_count_null_player_id": snap_null
            }

            if roster_null > 0 or injury_null > 0 or snap_null > 0:
                result.set_warn(
                    f"Null player_id counts: rosters={roster_null}, injuries={injury_null}, snap_counts={snap_null}",
                    details
                )
            else:
                result.set_pass(
                    "No null player_id values found",
                    details
                )

    except SQLAlchemyError as e:
        result.set_warn(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Check 9 — Team stats integrity (CRITICAL)
# ---------------------------------------------------------------------------
def check_team_stats_integrity(engine) -> ValidationResult:
    """Every game must have exactly 2 team_stats rows."""
    result = ValidationResult(9, "Team stats integrity", critical=True)

    try:
        with engine.connect() as conn:
            # Games with wrong number of team_stats rows
            wrong_count_query = text("""
                SELECT game_id, COUNT(*) as stat_count
                FROM team_stats
                GROUP BY game_id
                HAVING COUNT(*) != 2
                LIMIT 10
            """)
            wrong_count = conn.execute(wrong_count_query).fetchall()

            # Null values in critical fields - with detailed breakdown
            null_fields_query = text("""
                SELECT
                    COUNT(CASE WHEN offensive_epa_per_play IS NULL THEN 1 END) as null_off_epa,
                    COUNT(CASE WHEN defensive_epa_per_play_allowed IS NULL THEN 1 END) as null_def_epa
                FROM team_stats
            """)
            null_fields = conn.execute(null_fields_query).fetchone()

            # Detailed breakdown of null EPA by team and season
            null_breakdown_query = text("""
                SELECT
                    ts.team,
                    g.season,
                    COUNT(*) as null_count
                FROM team_stats ts
                JOIN games g ON ts.game_id = g.game_id
                WHERE ts.offensive_epa_per_play IS NULL
                  AND g.season BETWEEN :min AND :max
                GROUP BY ts.team, g.season
                ORDER BY ts.team, g.season
            """)
            null_breakdown = conn.execute(null_breakdown_query, {"min": MIN_SEASON, "max": MAX_SEASON}).fetchall()

            # Check wrong count
            if wrong_count:
                result.set_fail(
                    f"{len(wrong_count)} games have wrong number of team_stats rows (not 2)",
                    {"sample_problems": [{"game_id": row.game_id, "count": row.stat_count} for row in wrong_count]}
                )
                return result

            # Check null fields
            if null_fields.null_off_epa > 0 or null_fields.null_def_epa > 0:
                # Create detailed breakdown
                breakdown = {}
                for row in null_breakdown:
                    key = f"{row.team} (season {row.season})"
                    breakdown[key] = row.null_count

                result.set_fail(
                    f"Null values in team_stats: offensive_epa_per_play={null_fields.null_off_epa}, "
                    f"defensive_epa_per_play_allowed={null_fields.null_def_epa}",
                    {
                        "null_off_epa": null_fields.null_off_epa,
                        "null_def_epa": null_fields.null_def_epa,
                        "breakdown_by_team_season": breakdown,
                        "fix_suggestion": "Run: python scripts/build_team_stats.py --fix-pre-relocation"
                    }
                )
                return result

            result.set_pass(
                "All games have exactly 2 team_stats rows, no null values in critical fields",
                {}
            )

    except SQLAlchemyError as e:
        result.set_fail(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Check 10 — Depth chart coverage (WARNING)
# ---------------------------------------------------------------------------
def check_depth_chart_coverage(engine) -> ValidationResult:
    """Depth chart records must exist for all 11 seasons."""
    result = ValidationResult(10, "Depth chart coverage", critical=False)

    try:
        with engine.connect() as conn:
            # Distinct seasons with depth charts
            seasons_query = text("""
                SELECT DISTINCT season
                FROM depth_charts
                WHERE season BETWEEN :min AND :max
                ORDER BY season
            """)
            seasons = [row.season for row in conn.execute(seasons_query, {"min": MIN_SEASON, "max": MAX_SEASON}).fetchall()]

            # Average rows per season
            avg_rows_query = text("""
                SELECT
                    season,
                    COUNT(*) as row_count
                FROM depth_charts
                WHERE season BETWEEN :min AND :max
                GROUP BY season
                ORDER BY season
            """)
            row_counts = conn.execute(avg_rows_query, {"min": MIN_SEASON, "max": MAX_SEASON}).fetchall()

            total_rows = sum(row.row_count for row in row_counts)
            avg_rows = total_rows / len(row_counts) if row_counts else 0

            missing_seasons = [s for s in range(MIN_SEASON, MAX_SEASON + 1) if s not in seasons]

            details = {
                "found_seasons": seasons,
                "missing_seasons": missing_seasons,
                "row_counts_per_season": {row.season: row.row_count for row in row_counts},
                "total_rows": total_rows,
                "avg_rows_per_season": avg_rows
            }

            if missing_seasons:
                result.set_warn(
                    f"Depth chart data missing for seasons: {missing_seasons}",
                    details
                )
            else:
                result.set_pass(
                    f"Depth chart data present for all {len(seasons)} seasons, average {avg_rows:.0f} rows per season",
                    details
                )

    except SQLAlchemyError as e:
        result.set_warn(f"Database error: {e}")

    return result

# ---------------------------------------------------------------------------
# Row count summary
# ---------------------------------------------------------------------------
def get_table_row_counts(engine) -> Dict[str, int]:
    """Get row counts for all tables."""
    tables = [
        "games", "schedules", "play_by_play", "team_stats", "players",
        "rosters", "injuries", "snap_counts", "depth_charts",
        "stadiums", "weather"
    ]

    counts = {}
    try:
        with engine.connect() as conn:
            for table in tables:
                query = text(f"SELECT COUNT(*) FROM {table}")
                count = conn.execute(query).scalar()
                counts[table] = count
    except SQLAlchemyError:
        pass

    return counts

# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------
def main():
    print(f"\n{'=' * 70}")
    print("  Lunch Pail — Data Quality Validation")
    print(f"  Seasons: {MIN_SEASON}-{MAX_SEASON}")
    print(f"{'=' * 70}\n")

    try:
        engine = create_engine(DATABASE_URL, future=True)

        # Test connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("[OK] Database connection successful\n")
    except Exception as e:
        print(f"[ERROR] Database connection failed: {e}")
        sys.exit(1)

    # Run all checks
    checks = [
        check_season_coverage(engine),
        check_game_completeness(engine),
        check_pbp_coverage(engine),
        check_injury_coverage(engine),
        check_snap_count_coverage(engine),
        check_weather_completeness(engine),
        check_stadium_coverage(engine),
        check_player_id_integrity(engine),
        check_team_stats_integrity(engine),
        check_depth_chart_coverage(engine),
    ]

    # Print results
    for check in checks:
        status = check.get_status()
        icon = "[OK]" if status == "PASS" else "[WARN]" if status == "WARN" else "[FAIL]"
        critical = "CRITICAL" if check.critical else "WARNING"
        print(f"CHECK {check.check_num:2d} [{status}] {check.name} ({critical})")
        print(f"      {check.message}")
        if check.details and (check.warning or check.failed):
            for key, value in check.details.items():
                if isinstance(value, dict):
                    print(f"      {key}:")
                    for k, v in value.items():
                        print(f"        {k}: {v}")
                elif isinstance(value, list):
                    print(f"      {key}: {value}")
                else:
                    print(f"      {key}: {value}")
        print()

    # Summary
    passed = sum(1 for c in checks if c.passed)
    warnings = sum(1 for c in checks if c.warning)
    failed = sum(1 for c in checks if c.failed)
    critical_failed = sum(1 for c in checks if c.failed and c.critical)

    print(f"{'=' * 70}")
    print("SUMMARY:")
    print(f"  {passed} passed, {warnings} warnings, {failed} failed")
    print(f"  {critical_failed} CRITICAL failures")
    print(f"{'=' * 70}")

    # Row counts
    print("\nTABLE ROW COUNTS:")
    counts = get_table_row_counts(engine)
    for table, count in counts.items():
        print(f"  {table:20} {count:>9,}")

    print(f"\n{'=' * 70}")

    # Exit code
    if critical_failed > 0:
        print("[FAIL] Validation failed: CRITICAL checks failed")
        sys.exit(1)
    else:
        print("[OK] Validation passed")
        sys.exit(0)

if __name__ == "__main__":
    main()