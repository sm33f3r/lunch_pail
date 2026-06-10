# Lunch Pail — Schema Overview

**Version:** 1.0  
**Last Updated:** 2026-06-11  
**Database:** PostgreSQL 14+  
**Row Count:** ~1.2 million (2014–2024 seasons)

---

## Table Summary

| Table | Rows (est.) | Purpose | Primary Source | Refresh |
|-------|-------------|---------|----------------|---------|
| `games` | 2,816 | Core game records | nflreadpy schedules | Historical only |
| `schedules` | 2,816 | Schedule + results mirror | nflreadpy schedules | Historical only |
| `play_by_play` | 512,347 | Play-level data with EPA | nflreadpy PBP | Historical only |
| `team_stats` | 5,632 | Derived team performance | SQL aggregation | Derived from PBP |
| `players` | 12,847 | Player reference table | nflreadpy players | Historical only |
| `rosters` | 185,432 | Weekly roster status | nflreadpy rosters_weekly | Historical only |
| `injuries` | 17,387 | Injury report data | nflreadpy injuries | 2016–2024 only |
| `snap_counts` | 207,054 | Per-game snap percentages | nflreadpy snap_counts | Historical only |
| `depth_charts` | 120,753 | Weekly depth chart | nflreadpy depth_charts | Historical only |
| `stadiums` | 148 | Stadium metadata | Manual seed file | Static |
| `weather` | 2,816 | Game-time weather | Open-Meteo API | Historical only |

**Total:** ~1.17 million rows across 11 tables

---

## Entity Relationship Diagram

### Core Game Entities
```
games (1) ──── (1) schedules
   │
   ├─── (1) ──── (2) team_stats
   │
   ├─── (1) ──── (many) play_by_play
   │
   ├─── (1) ──── (1) weather
   │
   └─── (1) ──── (many) snap_counts
```

### Player & Team Entities
```
players (1) ──── (many) rosters
   │                    │
   ├─── (many) ──── (many) injuries
   │                    │
   ├─── (many) ──── (many) depth_charts
   │                    │
   └─── (many) ──── (many) snap_counts*
        * Different ID system
```

### Stadium & Weather
```
stadiums (1) ──── (many) games
   │
   └─── (1) ──── (many) weather
```

---

## Foreign Key Relationships

### Explicit (Database Constraints)

| Child Table | Foreign Key | Parent Table | Notes |
|-------------|-------------|--------------|-------|
| `games` | `stadium_id` | `stadiums` | Stadium for each game |
| `play_by_play` | `game_id` | `games` | Every play belongs to a game |
| `team_stats` | `game_id` | `games` | Stats per game per team |
| `rosters` | `player_id` | `players` | Player reference |
| `injuries` | `player_id` | `players` | Player reference |
| `depth_charts` | `player_id` | `players` | Player reference |
| `snap_counts` | `game_id` | `games` | Snap counts per game |
| `weather` | `game_id` | `games` | Weather per game |
| `weather` | `stadium_id` | `stadiums` | Stadium location |

### Implicit (Business Logic)

| Relationship | Join Logic | Notes |
|--------------|------------|-------|
| `games` ↔ `schedules` | `game_id` | 1:1 relationship |
| `rosters` ↔ `games` | `season, week, team` | Weekly context |
| `injuries` ↔ `games` | `season, week, team` | Weekly context |
| `depth_charts` ↔ `games` | `season, week, team` | Weekly context |

---

## Key Design Decisions

### 1. Historical Relocation Handling
**Problem:** Team abbreviations changed for relocated franchises:
- Oakland Raiders (OAK) → Las Vegas Raiders (LV) – 2020
- San Diego Chargers (SD) → Los Angeles Chargers (LAC) – 2017
- St. Louis Rams (STL) → Los Angeles Rams (LA) – 2016

**Solution:** `stadiums` table uses **current abbreviations only**. Games with old abbreviations join via CASE expression:
```sql
ON s.team = CASE g.home_team
    WHEN 'OAK' THEN 'LV'
    WHEN 'SD'  THEN 'LAC'
    WHEN 'STL' THEN 'LA'
    ELSE g.home_team
END
```

### 2. Weather Data Strategy
**Indoor vs Outdoor:** Binary `is_outdoor` flag determines weather treatment:
- `is_outdoor = TRUE` → Weather fields populated from Open-Meteo API
- `is_outdoor = FALSE` → Weather fields NULL (dome/retractable roof)

**API Efficiency:** Games grouped by `(latitude, longitude, timezone)` for batch API calls.

### 3. Team Stats Derivation
**Pure SQL Transformation:** `team_stats` derived entirely from `play_by_play` via CTE-based aggregation. No Python memory usage.

**Idempotent Ingestion:** `ON CONFLICT (game_id, team) DO NOTHING` allows safe re-runs.

### 4. Composite Keys for Weekly Data
Tables with weekly player data (`rosters`, `injuries`, `depth_charts`) use composite keys:
- `(season, week, team, player_id)` – ensures one row per player per week
- Prevents duplicates during re-ingestion
- Enables efficient `season/week` partitioning in queries

---

## ID System Architecture

### Two Player ID Systems

```
┌─────────────────┐    ┌─────────────────┐
│  GSIS ID System │    │  PFR ID System  │
│  (nflverse)     │    │  (PFR)          │
├─────────────────┤    ├─────────────────┤
│ • players       │    │ • snap_counts   │
│ • rosters       │    │                 │
│ • injuries      │    │ Format:         │
│ • depth_charts  │    │ "MahoPa00"      │
│ • play_by_play  │    │                 │
│                 │    │ Note: No direct │
│ Format:         │    │ join possible   │
│ "00-0012345"    │    │ without crosswalk│
└─────────────────┘    └─────────────────┘
```

**Phase 2 Requirement:** Build `player_id_crosswalk` table mapping `gsis_id` ↔ `pfr_player_id`.

### Game ID Standardization
All game references use nflverse standard format: `YYYY_WW_HOME_AWAY`
- Consistent across all tables
- Enables easy joins
- Human readable

---

## Index Strategy

### Primary Indexes (Automatically Created)
- All `SERIAL` primary keys
- All `PRIMARY KEY` constraints

### Secondary Indexes (Performance)

| Table | Index Columns | Purpose |
|-------|---------------|---------|
| `games` | `season` | Season-based queries |
| `games` | `week` | Week-based queries |
| `games` | `home_team`, `away_team` | Team-based queries |
| `play_by_play` | `game_id` | Game play retrieval |
| `play_by_play` | `posteam`, `defteam` | Team play analysis |
| `team_stats` | `game_id` | Game stats lookup |
| `team_stats` | `team` | Team performance history |
| `rosters` | `season`, `week`, `team` | Weekly roster queries |
| `rosters` | `player_id` | Player career view |
| `injuries` | `season`, `week`, `team` | Weekly injury reports |
| `snap_counts` | `game_id` | Game snap analysis |
| `snap_counts` | `team` | Team snap trends |
| `depth_charts` | `season`, `week`, `team` | Weekly depth chart |
| `weather` | `game_id` | Game weather lookup |

**Index Philosophy:** Optimize for common query patterns:
1. "All data for season X"
2. "All data for team Y in season X"
3. "All data for game Z"
4. "Player X's career/season/week"

---

## Data Flow & Ingestion Order

### Phase 1 Ingestion Sequence
```
1. stadiums (seed)        ───┐
2. games                   ←──┘
3. schedules               ←──┐
4. play_by_play            │  │
5. players (deduplicated)  ←──┘
6. rosters                 │
7. injuries                │
8. snap_counts             │
9. depth_charts            │
10. team_stats (derived)   ←──┐
11. weather                ←──┘
```

**Dependency Rules:**
- `games` requires `stadiums` (for `stadium_id`)
- `team_stats` requires `play_by_play` (source data)
- `weather` requires `games` and `stadiums` (for coordinates)
- Player tables (`rosters`, `injuries`, etc.) require `players` (reference)

### Idempotent Design
All ingest scripts use `ON CONFLICT DO NOTHING` or `ON CONFLICT DO UPDATE`:
- Safe to re-run after failures
- No duplicate rows
- Partial progress preserved

---

## Known Schema Limitations

### 1. Turnover Tracking Missing
**Issue:** `play_by_play` schema lacks interception/fumble flags.

**Current State:** `turnover_differential` in `team_stats` is universally 0.

**Required Fix:** Add columns and re-ingest:
```sql
ALTER TABLE play_by_play ADD COLUMN interception INTEGER;
ALTER TABLE play_by_play ADD COLUMN fumble_lost INTEGER;
-- Then re-run ingest.py for all seasons
```

### 2. Injury Practice Status Duplication
**Issue:** nflreadpy provides single `practice_status` field.

**Current Schema:** Three columns (`practice_status_wed`, `_thu`, `_fri`) all populated from same source.

**Phase 4 Solution:** ESPN endpoint provides true day-by-day status.

### 3. Snap Counts ID Incompatibility
**Issue:** `snap_counts` uses `pfr_player_id` while other tables use `gsis_id`.

**Workaround:** Team-level aggregates only in Phase 2 features.

**Phase 2 Solution:** Build crosswalk table using `nflreadpy.load_players()`.

### 4. Weather Condition Field Unused
**Issue:** `weather_condition` column exists but contains NULL values.

**Reason:** Open-Meteo WMO codes not requested in initial implementation.

**Future Enhancement:** Add WMO code parameter to API calls.

---

## Query Patterns & Examples

### Basic Game Information
```sql
-- Get all games for a team in a season
SELECT g.*, s.stadium_name, s.city, s.state
FROM games g
JOIN stadiums s ON g.stadium_id = s.stadium_id
WHERE g.season = 2023
  AND (g.home_team = 'KC' OR g.away_team = 'KC')
ORDER BY g.week;
```

### Team Performance Analysis
```sql
-- Team stats with game context
SELECT 
    g.season, g.week,
    g.home_team, g.away_team,
    g.home_score, g.away_score,
    ts.team,
    ts.offensive_epa_per_play,
    ts.defensive_epa_per_play_allowed
FROM team_stats ts
JOIN games g ON ts.game_id = g.game_id
WHERE ts.team = 'SF'
  AND g.season = 2022
ORDER BY g.week;
```

### Player Weekly Status
```sql
-- Combine roster, injury, depth for a player
SELECT 
    r.season, r.week, r.team,
    r.position, r.depth_chart_position, r.status,
    i.report_primary_injury, i.game_status,
    d.depth_team, d.depth_position
FROM rosters r
LEFT JOIN injuries i 
    ON r.season = i.season 
    AND r.week = i.week 
    AND r.team = i.team 
    AND r.player_id = i.player_id
LEFT JOIN depth_charts d
    ON r.season = d.season 
    AND r.week = d.week 
    AND r.team = d.team 
    AND r.player_id = d.player_id
WHERE r.player_id = '00-0012345'
  AND r.season = 2023
ORDER BY r.week;
```

### Weather Impact Analysis
```sql
-- Games with extreme weather
SELECT 
    g.season, g.week, g.home_team, g.away_team,
    w.temperature_f, w.wind_speed_mph, w.precipitation_inch,
    g.home_score, g.away_score
FROM weather w
JOIN games g ON w.game_id = g.game_id
WHERE w.is_outdoor = true
  AND (w.temperature_f < 20 OR w.wind_speed_mph > 20)
ORDER BY w.temperature_f;
```

---

## Phase 2 Schema Additions

### Planned Tables
| Table | Purpose | Notes |
|-------|---------|-------|
| `player_id_crosswalk` | Maps GSIS ↔ PFR IDs | Required for player-level features |
| `features_game` | Derived game-level features | Training dataset |
| `features_team` | Derived team-level features | Rolling averages, trends |
| `features_player` | Derived player-level features | If crosswalk completed |

### Planned Views
| View | Purpose |
|------|---------|
| `v_current_season` | Current season data only |
| `v_team_performance` | Team stats with opponent adjustment |
| `v_player_availability` | Combined roster/injury/depth status |

---

## Maintenance & Evolution

### Version Control
- Schema defined in `schema.sql` (idempotent `CREATE IF NOT EXISTS`)
- All changes via migration scripts
- Backward compatibility required for Phase 2

### Monitoring
- `scripts/validate_data.py` – data quality checks
- Row counts logged after each ingest
- Null rate monitoring for critical fields

### Backup Strategy
- Full database backup before schema changes
- Game-level incremental backup possible
- Player data append-only (historical)

---

## Contact & Updates

**Schema Owner:** Lunch Pail data engineering team  
**Change Process:** 
1. Update `schema.sql` with `ALTER TABLE` statements
2. Create migration script in `scripts/migrations/`
3. Test on development database
4. Run validation script
5. Update this document

**Related Documentation:**
- [`docs/data_dictionary.md`](data_dictionary.md) – Field-level definitions
- [`docs/data_quality_report.md`](data_quality_report.md) – Quality assessment
- `roadmaps/lunch_pail_phase1_roadmap.md` – Phase 1 objectives

---

*Schema version 1.0 – Phase 1 complete*  
*Next major revision: Phase 2 feature engineering*