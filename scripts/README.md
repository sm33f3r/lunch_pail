# Lunch Pail Scripts

This directory contains all data pipeline scripts for the Lunch Pail NFL prediction system.

## Core Ingestion Pipeline

### Phase 1 – Historical Data (2014–2024)

Run scripts in this order:

1. **Schema Setup**
   ```bash
   python scripts/run_schema.py
   ```

2. **Stadium Metadata** (static seed)
   ```bash
   python scripts/seed_stadiums.py
   ```

3. **Core NFL Data** (nflreadpy)
   ```bash
   python ingest.py
   ```

4. **Team Stats Aggregation** (derived from PBP)
   ```bash
   python scripts/build_team_stats.py
   ```

5. **Weather Data** (Open-Meteo API)
   ```bash
   python scripts/ingest_weather.py
   ```

6. **Weather Patch** (if needed)
   ```bash
   python scripts/patch_null_weather.py
   ```

7. **Validation** (data quality)
   ```bash
   python scripts/validate_data.py
   ```

## Script Details

### `ingest.py`
Main ingestion script for nflreadpy data:
- Play-by-play (EPA/WPA)
- Schedules & games
- Rosters (weekly)
- Injuries (2016–2024)
- Snap counts
- Depth charts
- Players (reference table)

**Idempotent:** Safe to re-run – uses `ON CONFLICT DO NOTHING`.

### `scripts/build_team_stats.py`
Derives team-level statistics from play-by-play data:
- Offensive/defensive EPA per play
- Third down conversion rates
- Red zone efficiency
- Points scored/allowed

**Pure SQL:** Runs entirely in PostgreSQL, no Python data loading.

### `scripts/ingest_weather.py`
Fetches historical weather from Open-Meteo API:
- Groups games by location for batch API calls
- Indoor games: `is_outdoor = FALSE`, weather fields NULL
- Outdoor games: temperature, wind, precipitation
- Graceful failure handling with retries

### `scripts/patch_null_weather.py`
Fixes outdoor games with null weather data:
- Required due to `ON CONFLICT DO NOTHING` limitation
- Updates existing null rows in place
- Same batching logic as ingest

### `scripts/validate_data.py`
Comprehensive data quality validation:
- 10 checks (7 critical, 3 warning)
- Season coverage, completeness, integrity
- Exit code 0 if all critical pass, 1 otherwise
- Prints table row counts

**Usage in CI/CD:** `python scripts/validate_data.py`

### `scripts/run_schema.py`
Applies PostgreSQL schema from `schema.sql`:
- Idempotent (`CREATE IF NOT EXISTS`)
- Creates all tables, indexes, constraints
- Verifies 11 tables created

### `scripts/seed_stadiums.py`
Loads static stadium metadata:
- All 32 current NFL teams
- Historical relocations (OAK→LV, SD→LAC, STL→LA)
- Coordinates for weather API
- Indoor/outdoor classification

## Dependencies

All scripts require:
- PostgreSQL 14+ with `lunch_pail` database
- Python 3.9+ with packages in `requirements.txt`
- `.env` file with database credentials
- Internet access (nflreadpy, Open-Meteo API)

## Environment Variables

Create `.env` file in project root:
```bash
DB_HOST=localhost
DB_PORT=5432
DB_NAME=lunch_pail
DB_USER=lunch_pail_user
DB_PASSWORD=your_password_here
```

## Common Issues

### Database Connection Failed
- Check PostgreSQL is running
- Verify `.env` file exists and has correct credentials
- Test: `python scripts/test_db.py`

### Weather API Failures
- Open-Meteo may have rate limits
- Script includes retry logic
- Alternative sources documented in code comments

### nflreadpy Import Errors
- Ensure nflreadpy is installed: `pip install nflreadpy`
- Check internet connectivity
- nflreadpy requires pandas, polars

## Testing

### Quick Validation
```bash
# Test database connection
python scripts/test_db.py

# Run full validation
python scripts/validate_data.py
```

### Mock Validation (for documentation)
```bash
python scripts/run_validation_mock.py
```

## Output Files

### Documentation (after validation)
- `docs/data_quality_report.md` – Validation results summary
- `docs/data_dictionary.md` – Field-level definitions
- `docs/schema.md` – Schema overview and relationships

### Logs
- Ingestion scripts print progress to stdout
- Errors to stderr
- No persistent log files (Phase 1 design)

## Phase 2 Additions

Planned for Phase 2 feature engineering:

1. **Feature derivation scripts**
   - Game-level features
   - Team rolling averages
   - Player availability metrics

2. **Enhanced validation**
   - Statistical distribution checks
   - Outlier detection
   - Trend analysis

3. **Data quality dashboard**
   - Web-based monitoring
   - Alerting for data issues
   - Historical quality trends

---

*Last updated: Phase 1 completion (2026-06-11)*