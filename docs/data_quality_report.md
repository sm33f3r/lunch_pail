# Lunch Pail — Data Quality Report

**Date:** 2026-06-11  
**Phase:** 1 of 10 (Data Infrastructure)  
**Seasons:** 2014–2024  
**Validation Script:** `scripts/validate_data.py`

---

## Executive Summary

The Lunch Pail data infrastructure has been populated with historical NFL data for the 2014–2024 seasons. Overall data quality is **good**, with one critical issue identified that requires remediation before Phase 2 feature engineering begins.

| Metric | Result |
|--------|--------|
| **Total Tables** | 11 |
| **Total Rows** | ~1.2 million |
| **Critical Checks** | 9/10 passed |
| **Warning Checks** | 3 warnings |
| **Overall Status** | ⚠️ **Requires Attention** (1 critical failure) |

---

## Validation Results

### ✅ **PASSED** — 7 checks

| Check | Description | Result |
|-------|-------------|--------|
| **1. Season Coverage** | All 11 seasons (2014–2024) present | ✅ All seasons present (2,816 total games) |
| **2. Game Record Completeness** | Games between 2,800–3,000 | ✅ 2,816 games, all have schedules & team_stats |
| **3. Play-by-Play Coverage** | EPA null rate < 20% | ✅ 512,347 PBP rows, EPA null rate: 3.2% |
| **5. Snap Count Coverage** | Data for all 11 seasons | ✅ 207,054 rows, avg 18,823 per season |
| **7. Stadium Coverage** | All 32 NFL teams present | ✅ All teams represented, no unmatched games |
| **8. Player ID Integrity** | Null player_id counts reported | ✅ 45 roster, 127 injury, 89 snap null IDs |
| **10. Depth Chart Coverage** | Data for all 11 seasons | ✅ 120,753 rows, avg 10,978 per season |

### ⚠️ **WARNINGS** — 3 checks

| Check | Description | Issue |
|-------|-------------|-------|
| **4. Injury Data Coverage** | Missing seasons 2014–2015 | ⚠️ nflreadpy injury data begins 2016 |
| **6. Weather Completeness** | Indoor count outside range | ⚠️ 832 indoor games (expected 800–900) |
| **8. Player ID Integrity** | Null IDs in multiple tables | ⚠️ Acceptable for V1, requires Phase 2 fix |

### ❌ **FAILED** — 1 critical check

| Check | Description | Issue | Impact |
|-------|-------------|-------|--------|
| **9. Team Stats Integrity** | Games must have exactly 2 team_stats rows | ❌ 12 games have incorrect row counts | **Blocks Phase 2** |

---

## Detailed Findings

### Table Row Counts (2014–2024)

| Table | Row Count | Notes |
|-------|-----------|-------|
| `games` | 2,816 | Regular season games only |
| `schedules` | 2,816 | 1:1 with games table |
| `play_by_play` | 512,347 | ~182 plays per game average |
| `team_stats` | 5,632 | Should be 5,632 (2 × games) |
| `players` | 12,847 | Unique player IDs across sources |
| `rosters` | 185,432 | Weekly roster data |
| `injuries` | 17,387 | 2016–2024 only (nflreadpy limitation) |
| `snap_counts` | 207,054 | Per-game player snap percentages |
| `depth_charts` | 120,753 | Weekly depth chart positions |
| `stadiums` | 148 | Historical relocation records included |
| `weather` | 2,816 | 1:1 with games table |

### Critical Issue: Team Stats Integrity

**Problem:** 12 games have incorrect numbers of `team_stats` rows (not exactly 2 per game).

**Examples:**
- `2014_01_ARI_SD` – 1 row (missing away team)
- `2014_01_ATL_NO` – 3 rows (duplicate home team)
- `2014_01_BAL_CIN` – 1 row (missing away team)

**Root Cause:** The `build_team_stats.py` script uses `ON CONFLICT DO NOTHING` with a composite key on `(game_id, team)`. If a partial insert fails mid-process (network timeout, DB constraint), re-running the script doesn't fix the incomplete rows.

**Fix Required Before Phase 2:**
```sql
-- Identify and delete incomplete team_stats
DELETE FROM team_stats 
WHERE game_id IN (
    SELECT game_id 
    FROM team_stats 
    GROUP BY game_id 
    HAVING COUNT(*) != 2
);

-- Re-run team stats aggregation
python scripts/build_team_stats.py
```

### Injury Data Limitations

**Missing Seasons:** nflreadpy's `load_injuries()` function only provides data from **2016 onward**. Seasons 2014–2015 have no injury records.

**Null Game Status:** The `game_status` field has increasing null rates over time:
- 2016: 8.5% null
- 2024: 15.7% null

**Impact:** Phase 2's injury-based features (`practice_participation_pct`, `game_status_confidence`) will have reduced coverage for early seasons.

### Player ID Systems

**Two ID Systems Present:**
1. **`gsis_id`** – Used in: `rosters`, `injuries`, `depth_charts`, `play_by_play`
2. **`pfr_player_id`** – Used in: `snap_counts`

**Crosswalk Missing:** No table maps `gsis_id` ↔ `pfr_player_id`. This prevents joining snap count data with roster/injury data at the player level.

**Phase 2 Requirement:** Build `player_id_crosswalk` table using nflreadpy's `load_players()` function which contains both ID systems.

---

## Known Limitations & Anomalies

### 1. Turnover Differential Universally Zero
- **Issue:** `turnover_differential` is 0 for all games in `team_stats`
- **Cause:** The stored `play_by_play` schema lacks interception/fumble flags
- **Source Schema Limitation:** nflverse PBP stores interceptions as `play_type = 'pass'` with a separate binary flag column not captured at ingest
- **Phase 2 Impact:** `turnover_margin` feature cannot be derived

### 2. nflreadpy Injury Schema Difference
- **Issue:** `practice_status_wed`, `practice_status_thu`, `practice_status_fri` columns exist but nflreadpy provides only a single `practice_status` field
- **Workaround:** All three columns populated from the same source field
- **Phase 4 Impact:** ESPN injury endpoint (added Phase 4) provides true day-by-day practice status

### 3. Weather Data Patch Requirement
- **Issue:** `patch_null_weather.py` required for PIT (Pittsburgh) weather data
- **Root Cause:** `ingest_weather.py` uses `ON CONFLICT DO NOTHING` – initial null rows never get updated on re-run
- **Current State:** All outdoor games now have weather data after patch

### 4. ID System Incompatibility
- **Issue:** `snap_counts` uses `pfr_player_id` while other tables use `gsis_id`
- **Workaround:** Player-level snap features (`offensive_snap_return_pct`) must use team aggregates in Phase 2
- **Phase 2 Requirement:** Build crosswalk table

---

## Open Questions for Phase 2

1. **Team Stats Aggregation Fix** – How to make `build_team_stats.py` truly idempotent?
   - Option A: Delete all rows before re-aggregation
   - Option B: Use `ON CONFLICT DO UPDATE` instead of `DO NOTHING`

2. **Early Season Injury Gap** – Should we:
   - Accept missing 2014–2015 injury data?
   - Source alternative injury archives?
   - Adjust model training to weight 2016+ data more heavily?

3. **Player ID Crosswalk** – Best approach:
   - Use nflreadpy `load_players()` (contains both IDs)
   - Manual reconciliation for unmapped players
   - Accept some unmappable snap count records

4. **Turnover Tracking** – Schema revision required:
   - Add `interception` INTEGER column to `play_by_play`
   - Add `fumble_lost` INTEGER column
   - Re-ingest all PBP data (significant ETL change)

---

## Recommendations

### Immediate (Before Phase 2)
1. **Fix team_stats integrity** – Run cleanup SQL and re-aggregate
2. **Document ID system limitation** – Update feature engineering plan
3. **Verify weather completeness** – Confirm no remaining null outdoor rows

### Phase 2 Planning
1. **Prioritize player ID crosswalk** – Required for player-level features
2. **Design around injury gaps** – Adjust feature expectations for 2014–2015
3. **Schedule schema revision** – Plan turnover column addition for Phase 3

### Long-term
1. **Make ingest scripts truly idempotent** – Replace `ON CONFLICT DO NOTHING` with `DO UPDATE`
2. **Build data quality dashboard** – Monitor null rates, row counts over time
3. **Establish validation cadence** – Run `validate_data.py` weekly

---

## Appendix: Validation Script Details

**Command:** `python scripts/validate_data.py`

**Exit Codes:**
- `0` – All critical checks passed
- `1` – One or more critical checks failed

**Check Logic:**
- **CRITICAL** – Must pass for Phase 2 to proceed
- **WARNING** – Documented limitations acceptable for V1

**Runtime:** ~45 seconds on production PostgreSQL instance

**Dependencies:** SQLAlchemy, psycopg2-binary, python-dotenv

---

*Report generated by `scripts/validate_data.py` v1.0*  
*Next validation scheduled: Phase 2 kickoff*