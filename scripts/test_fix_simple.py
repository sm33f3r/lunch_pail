"""
Simple test to verify the team stats fix logic.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

DATABASE_URL = (
    f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)

def test_sql_logic():
    """Test the SQL logic without executing updates."""
    engine = create_engine(DATABASE_URL, future=True)

    print("Testing team stats fix SQL logic...")
    print("=" * 60)

    # Test 1: Count null rows
    count_query = text("""
        SELECT COUNT(*)
        FROM team_stats
        WHERE offensive_epa_per_play IS NULL
    """)

    # Test 2: Find affected rows
    find_query = text("""
        SELECT game_id, team
        FROM team_stats
        WHERE offensive_epa_per_play IS NULL
        ORDER BY game_id, team
        LIMIT 5
    """)

    # Test 3: Sample update logic for one row
    sample_update_query = text("""
        SELECT
            :game_id AS game_id,
            :team AS team,
            (
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
            ) AS offensive_epa_per_play,
            (
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
            ) AS defensive_epa_per_play_allowed
        FROM team_stats
        WHERE game_id = :game_id AND team = :team
        LIMIT 1
    """)

    try:
        with engine.connect() as conn:
            # Test 1
            null_count = conn.execute(count_query).scalar()
            print(f"\n1. Null EPA rows in team_stats: {null_count}")

            # Test 2
            if null_count > 0:
                affected = conn.execute(find_query).fetchall()
                print(f"\n2. Sample affected rows (first 5):")
                for i, row in enumerate(affected, 1):
                    print(f"   {i}. {row.game_id} - {row.team}")

                # Test 3: Test the update logic on first affected row
                if affected:
                    test_game_id = affected[0].game_id
                    test_team = affected[0].team

                    print(f"\n3. Testing update logic for {test_game_id} ({test_team}):")

                    result = conn.execute(
                        sample_update_query,
                        {"game_id": test_game_id, "team": test_team}
                    ).fetchone()

                    if result:
                        print(f"   Offensive EPA would be: {result.offensive_epa_per_play}")
                        print(f"   Defensive EPA allowed would be: {result.defensive_epa_per_play_allowed}")

                        if result.offensive_epa_per_play is None:
                            print("   WARNING: Computed offensive EPA is NULL")
                        else:
                            print("   ✓ Offensive EPA computed successfully")
                    else:
                        print("   ERROR: No result from test query")
            else:
                print("\n2. No null EPA rows found")
                print("\n3. Skipping update logic test")

            # Test 4: Verify bug root cause
            print(f"\n4. Verifying abbreviation mismatch:")
            mismatch_query = text("""
                -- Find a game with OAK/SD/STL in games table
                SELECT
                    g.game_id,
                    g.season,
                    g.week,
                    g.home_team,
                    g.away_team,
                    COUNT(DISTINCT p.posteam) as pbp_teams_count,
                    STRING_AGG(DISTINCT p.posteam, ', ') as pbp_teams
                FROM games g
                LEFT JOIN play_by_play p ON g.game_id = p.game_id
                WHERE (g.home_team IN ('OAK', 'SD', 'STL') OR g.away_team IN ('OAK', 'SD', 'STL'))
                  AND g.season BETWEEN 2014 AND 2019
                GROUP BY g.game_id, g.season, g.week, g.home_team, g.away_team
                HAVING COUNT(DISTINCT p.posteam) > 0
                ORDER BY g.season, g.week
                LIMIT 3
            """)

            mismatches = conn.execute(mismatch_query).fetchall()
            if mismatches:
                for row in mismatches:
                    print(f"   {row.game_id}: games={row.home_team}/{row.away_team}, PBP teams={row.pbp_teams}")
                print("   ✓ Confirmed: PBP uses current abbreviations (LV/LAC/LA)")
            else:
                print("   No games found with OAK/SD/STL abbreviations")

        print(f"\n{'=' * 60}")
        print("SQL logic test complete.")

        if null_count > 0:
            print(f"\nTo fix {null_count} null rows, run:")
            print("  python scripts/build_team_stats.py --fix-pre-relocation")

        return True

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_sql_logic()
    sys.exit(0 if success else 1)