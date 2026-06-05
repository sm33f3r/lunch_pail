"""
scripts/run_schema.py

Reads schema.sql from the project root and applies it to the PostgreSQL
database specified in .env.  Safe to run multiple times (idempotent).
"""

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
SCHEMA_FILE = PROJECT_ROOT / "schema.sql"
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
# Read schema
# ---------------------------------------------------------------------------
if not SCHEMA_FILE.exists():
    print(f"ERROR: schema file not found: {SCHEMA_FILE}", file=sys.stderr)
    sys.exit(1)

schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Apply schema
# ---------------------------------------------------------------------------
engine = create_engine(DATABASE_URL, future=True)

try:
    with engine.begin() as conn:
        conn.execute(text(schema_sql))
    print("Schema applied successfully.")
except SQLAlchemyError as exc:
    print(f"ERROR applying schema:\n{exc}", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Validation: count tables in the public schema
# ---------------------------------------------------------------------------
EXPECTED_TABLES = 11

try:
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type   = 'BASE TABLE'
                  AND table_catalog = :db_name
                """
            ),
            {"db_name": DB_NAME},
        )
        count = result.scalar()

    print(f"{count} tables found.")

    if count != EXPECTED_TABLES:
        print(
            f"WARNING: expected {EXPECTED_TABLES} tables, found {count}.",
            file=sys.stderr,
        )
        sys.exit(1)

except SQLAlchemyError as exc:
    print(f"ERROR during validation:\n{exc}", file=sys.stderr)
    sys.exit(1)
