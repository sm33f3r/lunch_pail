"""
scripts/seed_stadiums.py

Loads data/stadiums.csv into the stadiums table in PostgreSQL.
Idempotent: safe to re-run — existing rows (matched on team + season_start)
are silently skipped via ON CONFLICT DO NOTHING.
"""

import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CSV_FILE = PROJECT_ROOT / "data" / "stadiums.csv"
ENV_FILE = PROJECT_ROOT / ".env"

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
load_dotenv(ENV_FILE)

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ["DB_PORT"]
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

DATABASE_URL = (
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# ---------------------------------------------------------------------------
# Read and coerce CSV rows
# ---------------------------------------------------------------------------
if not CSV_FILE.exists():
    print(f"ERROR: CSV file not found: {CSV_FILE}", file=sys.stderr)
    sys.exit(1)


def _coerce(row: dict) -> dict:
    """Convert CSV string values to the correct Python types for psycopg2."""
    def _int(v):
        return int(v) if v and v.strip() else None

    def _float(v):
        return float(v) if v and v.strip() else None

    def _bool(v):
        if not v or not v.strip():
            return None
        return v.strip().lower() == "true"

    def _str(v):
        return v.strip() if v and v.strip() else None

    return {
        "team":         _str(row["team"]),
        "stadium_name": _str(row["stadium_name"]),
        "city":         _str(row["city"]),
        "state":        _str(row["state"]),
        "latitude":     _float(row["latitude"]),
        "longitude":    _float(row["longitude"]),
        "timezone":     _str(row["timezone"]),
        "surface":      _str(row["surface"]),
        "roof_type":    _str(row["roof_type"]),
        "is_outdoor":   _bool(row["is_outdoor"]),
        "season_start": _int(row["season_start"]),
        "season_end":   _int(row["season_end"]),
    }


with CSV_FILE.open(newline="", encoding="utf-8") as fh:
    reader = csv.DictReader(fh)
    rows = [_coerce(r) for r in reader]

print(f"Read {len(rows)} rows from {CSV_FILE.name}.")

# ---------------------------------------------------------------------------
# Connect and insert
# ---------------------------------------------------------------------------
# Unique index on (team, season_start) is required for ON CONFLICT to work.
# CREATE INDEX IF NOT EXISTS makes this step idempotent.
ENSURE_INDEX_SQL = text("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_stadiums_team_season_start
    ON stadiums (team, season_start)
""")

INSERT_SQL = text("""
    INSERT INTO stadiums (
        team, stadium_name, city, state,
        latitude, longitude, timezone,
        surface, roof_type, is_outdoor,
        season_start, season_end
    ) VALUES (
        :team, :stadium_name, :city, :state,
        :latitude, :longitude, :timezone,
        :surface, :roof_type, :is_outdoor,
        :season_start, :season_end
    )
    ON CONFLICT (team, season_start) DO NOTHING
""")

COUNT_SQL = text("SELECT COUNT(*) FROM stadiums")

engine = create_engine(DATABASE_URL, future=True)

try:
    with engine.begin() as conn:
        conn.execute(ENSURE_INDEX_SQL)

        before = conn.execute(COUNT_SQL).scalar()

        # executemany-style: pass a list of dicts
        conn.execute(INSERT_SQL, rows)

        after = conn.execute(COUNT_SQL).scalar()

    inserted = after - before
    print(f"Rows inserted:              {inserted}")
    print(f"Total rows in stadiums:     {after}")

except SQLAlchemyError as exc:
    print(f"ERROR during seed:\n{exc}", file=sys.stderr)
    sys.exit(1)
