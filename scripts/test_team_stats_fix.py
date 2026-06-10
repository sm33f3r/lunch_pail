"""
Test script to verify the team_stats pre-relocation abbreviation fix.
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

def test_pre_relocation_fix():
    """Test that OAK/SD/STL team_stats have non-null EPA after fix."""
    engine = create_engine(DATABASE_URL, future=True)

    print("Testing pre-relocation team abbreviation fix...")
    print("=" * 60)

    # Query 1: Count null EPA rows (all teams)
    null_query = text("""
        SELECT COUNT(*)
        FROM team_stats
        WHERE offensive_epa_per_play IS NULL
    """)

    # Query 2: Sample of fixed rows (should have non-null EPA after fix)
    sample_query = text("""
        SELECT
            ts.game_id,
            ts.team,
            g.season,
            g.week,
            ts.offensive_epa_per_play,
            ts.defensive_epa_per_play_allowed,
            ts.third_down_conv_rate_off,
            ts.red_zone_efficiency_off
        FROM team_stats ts
        JOIN games g ON ts.game_id = g.game_id
        WHERE ts.team IN ('OAK', 'SD', 'STL')
          AND g.season BETWEEN 2014 AND 2019
          AND ts.offensive_epa_per_play IS NOT NULL
        ORDER BY g.season, g.week, ts.team
        LIMIT 10
    """)

    # Query 3: Total null EPA rows (should be 0 after fix)
    total_null_query = text("""
        SELECT COUNT(*)
        FROM team_stats
        WHERE offensive_epa_per_play IS NULL
    """)

    try:
        with engine.connect() as conn:
            # Test 1: Check total null count
            print("\n1. Total null EPA rows in team_stats:")
            null_count = conn.execute(null_query).scalar()
            print(f"   {null_count} null EPA rows")

            if null_count > 0:
                print("   WARNING: Null EPA rows found")

                # Get breakdown
                breakdown_query = text("""
                    SELECT
                        ts.team,
                        COUNT(*) as null_count
                    FROM team_stats ts
                    WHERE ts.offensive_epa_per_play IS NULL
                    GROUP BY ts.team
                    ORDER BY ts.team
                """)
                breakdown = conn.execute(breakdown_query).fetchall()
                if breakdown:
                    print("   Breakdown by team:")
                    for row in breakdown:
                        print(f"     {row.team}: {row.null_count} rows")
            else:
                print("   ✓ No null EPA rows")

            # Test 2: Show sample of fixed rows
            print("\n2. Sample of fixed OAK/SD/STL rows (non-null EPA):")
            sample_results = conn.execute(sample_query).fetchall()

            if sample_results:
                for row in sample_results:
                    print(f"   {row.game_id} ({row.team})")
                    print(f"     Season {row.season}, Week {row.week}")
                    print(f"     Off EPA: {row.offensive_epa_per_play:.3f}")
                    print(f"     Def EPA allowed: {row.defensive_epa_per_play_allowed:.3f}")
                    print(f"     3rd down %: {row.third_down_conv_rate_off:.1%}")
                    print(f"     Red zone EPA: {row.red_zone_efficiency_off:.3f}")
            else:
                print("   No non-null EPA rows found for OAK/SD/STL 2014-2019")

            # Test 3: Total null count
            print("\n3. Total null EPA rows in team_stats:")
            total_null = conn.execute(total_null_query).scalar()
            print(f"   {total_null} total null EPA rows")

            if total_null == 0:
                print("   ✓ All team_stats rows have EPA values")
            else:
                print(f"   ⚠ {total_null} rows still have null EPA")

            # Test 4: Verify the bug cause
            print("\n4. Verifying bug root cause:")
            bug_verification_query = text("""
                -- Find games where play_by_play has LV/LAC/LA but games has OAK/SD/STL
                SELECT
                    g.game_id,
                    g.season,
                    g.week,
                    g.home_team as game_home,
                    g.away_team as game_away,
                    COUNT(DISTINCT p.posteam) as distinct_pbp_teams,
                    STRING_AGG(DISTINCT p.posteam, ', ') as pbp_teams
                FROM games g
                JOIN play_by_play p ON g.game_id = p.game_id
                WHERE g.season BETWEEN 2014 AND 2019
                  AND (
                    (g.home_team = 'OAK' AND 'LV' IN (p.posteam, p.defteam)) OR
                    (g.home_team = 'SD' AND 'LAC' IN (p.posteam, p.defteam)) OR
                    (g.home_team = 'STL' AND 'LA' IN (p.posteam, p.defteam))
                  )
                GROUP BY g.game_id, g.season, g.week, g.home_team, g.away_team
                LIMIT 5
            """)

            verification_results = conn.execute(bug_verification_query).fetchall()
            if verification_results:
                print("   ✓ Bug confirmed: PBP uses current abbreviations")
                for row in verification_results:
                    print(f"     {row.game_id}: games={row.game_home}/{row.game_away}, PBP teams={row.pbp_teams}")
            else:
                print("   No mismatched abbreviations found (unexpected)")

        print("\n" + "=" * 60)
        print("Test complete.")

        # Recommendations
        if null_results:
            print("\nRECOMMENDATION: Run fix command:")
            print("  python scripts/build_team_stats.py --fix-pre-relocation")

    except Exception as e:
        print(f"ERROR: {e}")
        return False

    return True

if __name__ == "__main__":
    success = test_pre_relocation_fix()
    sys.exit(0 if success else 1)