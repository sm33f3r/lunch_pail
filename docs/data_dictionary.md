# Lunch Pail — Data Dictionary

**Version:** 1.0  
**Last Updated:** 2026-06-11  
**Phase:** 1 (Data Infrastructure)

---

## Overview

This document provides field-level definitions for all 11 tables in the Lunch Pail NFL prediction system database. Each table is sourced from either nflreadpy (historical NFL data), Open-Meteo API (weather), or manually maintained seed files (stadiums).

---

## Table: `games`

**Purpose:** Core game records – one row per regular season NFL game (2014–2024).  
**Source:** Derived from `nflreadpy.load_schedules()` during ingest.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `game_id` | VARCHAR(20) | nflverse game identifier | Primary key. Format: `YYYY_WW_HOME_AWAY` |
| `season` | INTEGER | NFL season year | 2014–2024 |
| `week` | INTEGER | Regular season week number | 1–18 (17 since 2021) |
| `home_team` | VARCHAR(10) | Home team abbreviation | Current NFL abbreviations |
| `away_team` | VARCHAR(10) | Away team abbreviation | Current NFL abbreviations |
| `home_score` | INTEGER | Final home team score | NULL for future/unplayed games |
| `away_score` | INTEGER | Final away team score | NULL for future/unplayed games |
| `result` | INTEGER | Game outcome | 1 = home win, 0 = away win, NULL = future |
| `game_date` | DATE | Date of game | Local stadium date |
| `game_time` | VARCHAR(10) | Kickoff time (local) | Format varies: "13:00", "20:20", "1:00 PM" |
| `stadium_id` | INTEGER | Foreign key to stadiums | References `stadiums.stadium_id` |
| `season_type` | VARCHAR(10) | Season segment | Always "REG" (regular season only) |
| `week_type` | VARCHAR(10) | Broadcast timing | "TNF", "SNF", "MNF", "SAT", "standard" |

---

## Table: `schedules`

**Purpose:** Game schedule and result data – mirrors nflverse schedule format.  
**Source:** `nflreadpy.load_schedules()` direct ingest.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `schedule_id` | SERIAL | Auto-incrementing primary key | |
| `game_id` | VARCHAR(20) | nflverse game identifier | Unique constraint, matches `games.game_id` |
| `season` | INTEGER | NFL season year | |
| `week` | INTEGER | Regular season week number | |
| `home_team` | VARCHAR(10) | Home team abbreviation | |
| `away_team` | VARCHAR(10) | Away team abbreviation | |
| `game_date` | DATE | Date of game | |
| `weekday` | VARCHAR(10) | Day of week | "Thursday", "Sunday", "Monday", "Saturday" |
| `gametime` | VARCHAR(10) | Kickoff time (ET) | Eastern Time zone |
| `home_score` | INTEGER | Final home team score | |
| `away_score` | INTEGER | Final away team score | |
| `result` | FLOAT | Spread-adjusted result | From betting markets, NULL if unavailable |
| `overtime` | BOOLEAN | Overtime indicator | TRUE if game went to OT |

---

## Table: `play_by_play`

**Purpose:** Play-level data – one row per offensive play.  
**Source:** `nflreadpy.load_pbp()` filtered to regular season 2014–2024.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `pbp_id` | SERIAL | Auto-incrementing primary key | |
| `game_id` | VARCHAR(20) | nflverse game identifier | Foreign key to `games.game_id` |
| `play_id` | INTEGER | Play sequence number | Unique within game |
| `posteam` | VARCHAR(10) | Team with possession | Offensive team for the play |
| `defteam` | VARCHAR(10) | Defensive team | |
| `down` | INTEGER | Down number | 1–4 |
| `ydstogo` | INTEGER | Yards to first down | |
| `yardline_100` | INTEGER | Yards from opponent's end zone | 1–99 (100 = own goal line) |
| `epa` | FLOAT | Expected Points Added | NULL for special teams, penalties |
| `wpa` | FLOAT | Win Probability Added | NULL ~3% of rows |
| `air_yards` | FLOAT | Pass air yards | NULL for non-pass plays |
| `yards_gained` | INTEGER | Net yards gained on play | Can be negative |
| `play_type` | VARCHAR(30) | Type of play | "pass", "run", "punt", "field_goal", etc. |
| `passer_player_id` | VARCHAR(20) | Passer GSIS ID | NULL for non-pass plays |
| `rusher_player_id` | VARCHAR(20) | Rusher GSIS ID | NULL for non-rush plays |
| `receiver_player_id` | VARCHAR(20) | Receiver GSIS ID | NULL for non-receptions |
| `qb_dropback` | INTEGER | QB dropback indicator | 1 = dropback, 0 = not |
| `qb_scramble` | INTEGER | QB scramble indicator | 1 = scramble, 0 = not |
| `pass_attempt` | INTEGER | Pass attempt indicator | 1 = pass attempt, 0 = not |
| `rush_attempt` | INTEGER | Rush attempt indicator | 1 = rush attempt, 0 = not |
| `penalty` | INTEGER | Penalty indicator | 1 = penalty accepted, 0 = not |

**Schema Limitation:** Missing interception/fumble flags – `turnover_differential` cannot be derived.

---

## Table: `team_stats`

**Purpose:** Per-game team performance statistics – two rows per game (home & away).  
**Source:** Derived from `play_by_play` via `scripts/build_team_stats.py`.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `stat_id` | SERIAL | Auto-incrementing primary key | |
| `game_id` | VARCHAR(20) | nflverse game identifier | Foreign key to `games.game_id` |
| `team` | VARCHAR(10) | Team abbreviation | |
| `offensive_epa_per_play` | FLOAT | Average EPA on offensive plays | NULL if no qualifying plays |
| `defensive_epa_per_play_allowed` | FLOAT | Average EPA allowed on defense | NULL if no qualifying plays |
| `turnover_differential` | INTEGER | Turnovers gained minus committed | **Currently always 0** – schema limitation |
| `third_down_conv_rate_off` | FLOAT | Offensive 3rd down conversion rate | NULL if no 3rd down attempts |
| `third_down_conv_rate_def` | FLOAT | Defensive 3rd down stop rate | NULL if no 3rd down attempts against |
| `red_zone_efficiency_off` | FLOAT | Average EPA in opponent's 20 | NULL if no red zone plays |
| `red_zone_efficiency_def` | FLOAT | Average EPA allowed in own 20 | NULL if no red zone plays against |
| `points_scored` | INTEGER | Points scored by team | From `games` table |
| `points_allowed` | INTEGER | Points allowed by team | From `games` table |

**Unique Constraint:** `(game_id, team)` – ensures exactly 2 rows per game.

---

## Table: `players`

**Purpose:** Player reference table – unique player IDs across data sources.  
**Source:** `nflreadpy.load_players()` plus deduplication during ingest.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `player_id` | VARCHAR(20) | GSIS ID (primary key) | nflverse standard player identifier |
| `player_name` | VARCHAR(100) | Player's full name | "Patrick Mahomes", "Justin Jefferson" |
| `position` | VARCHAR(10) | Primary position | "QB", "WR", "RB", "DE", "CB", etc. |
| `birth_date` | DATE | Date of birth | |
| `college` | VARCHAR(100) | College attended | |
| `draft_year` | INTEGER | NFL draft year | NULL for undrafted players |
| `draft_round` | INTEGER | Draft round | NULL for undrafted |
| `draft_pick` | INTEGER | Overall pick number | NULL for undrafted |

**Note:** Contains only players who appear in rosters, injuries, depth charts, or PBP.

---

## Table: `rosters`

**Purpose:** Weekly roster data – active/inactive players per team.  
**Source:** `nflreadpy.load_rosters_weekly()` for seasons 2014–2024.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `roster_id` | SERIAL | Auto-incrementing primary key | |
| `season` | INTEGER | NFL season year | |
| `week` | INTEGER | Regular season week number | |
| `team` | VARCHAR(10) | Team abbreviation | |
| `player_id` | VARCHAR(20) | GSIS ID | Foreign key to `players.player_id` |
| `player_name` | VARCHAR(100) | Player's full name | Redundant with `players` table |
| `position` | VARCHAR(10) | Position for that week | Can change week-to-week |
| `depth_chart_position` | VARCHAR(20) | Depth chart slot | "QB1", "WR2", "FS1", etc. |
| `jersey_number` | INTEGER | Jersey number | |
| `status` | VARCHAR(20) | Roster status | "ACT", "INACTIVE", "RESERVE/INJURED" |

**Unique Constraint:** `(season, week, team, player_id)` – prevents duplicates.

---

## Table: `injuries`

**Purpose:** Historical injury report data – weekly player injury status.  
**Source:** `nflreadpy.load_injuries()` for seasons **2016–2024 only**.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `injury_id` | SERIAL | Auto-incrementing primary key | |
| `season` | INTEGER | NFL season year | **2016–2024 only** |
| `week` | INTEGER | Regular season week number | |
| `team` | VARCHAR(10) | Team abbreviation | |
| `player_id` | VARCHAR(20) | GSIS ID | Foreign key to `players.player_id` |
| `player_name` | VARCHAR(100) | Player's full name | |
| `position` | VARCHAR(10) | Player position | |
| `report_primary_injury` | VARCHAR(100) | Primary injury description | "Hamstring", "Ankle", "Concussion" |
| `practice_status_wed` | VARCHAR(20) | Wednesday practice status | **All from same field** – nflreadpy limitation |
| `practice_status_thu` | VARCHAR(20) | Thursday practice status | **All from same field** – nflreadpy limitation |
| `practice_status_fri` | VARCHAR(20) | Friday practice status | **All from same field** – nflreadpy limitation |
| `game_status` | VARCHAR(20) | Game day status | "Questionable", "Doubtful", "Out", "IR" |

**Unique Constraint:** `(season, week, team, player_id)`  
**Note:** Phase 4 adds ESPN endpoint with true day-by-day practice status.

---

## Table: `snap_counts`

**Purpose:** Per-game player snap counts – offensive/defensive snap percentages.  
**Source:** `nflreadpy.load_snap_counts()` for seasons 2014–2024.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `snap_id` | SERIAL | Auto-incrementing primary key | |
| `game_id` | VARCHAR(20) | nflverse game identifier | Foreign key to `games.game_id` |
| `season` | INTEGER | NFL season year | |
| `week` | INTEGER | Regular season week number | |
| `team` | VARCHAR(10) | Team abbreviation | |
| `player_id` | VARCHAR(20) | **PFR Player ID** | **Different system than `gsis_id`** |
| `player_name` | VARCHAR(100) | Player's full name | |
| `position` | VARCHAR(10) | Player position | |
| `offense_snaps` | INTEGER | Offensive snaps played | |
| `offense_pct` | FLOAT | Offensive snap percentage | 0.0–100.0 |
| `defense_snaps` | INTEGER | Defensive snaps played | |
| `defense_pct` | FLOAT | Defensive snap percentage | 0.0–100.0 |

**Unique Constraint:** `(game_id, player_id)`  
**Critical Note:** Uses `pfr_player_id` system – requires crosswalk to join with other tables.

---

## Table: `depth_charts`

**Purpose:** Weekly depth chart data – player positional depth.  
**Source:** `nflreadpy.load_depth_charts()` for seasons 2014–2024.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `depth_id` | SERIAL | Auto-incrementing primary key | |
| `season` | INTEGER | NFL season year | |
| `week` | INTEGER | Regular season week number | |
| `team` | VARCHAR(10) | Team abbreviation | |
| `player_id` | VARCHAR(20) | GSIS ID | Foreign key to `players.player_id` |
| `player_name` | VARCHAR(100) | Player's full name | |
| `position` | VARCHAR(10) | Position group | "QB", "WR", "OLB", etc. |
| `depth_team` | INTEGER | Depth team number | 1 = first team, 2 = second team, etc. |
| `depth_position` | VARCHAR(20) | Specific depth slot | "QB1", "LT1", "NB2", etc. |

**Unique Constraint:** `(season, week, team, player_id, depth_position)`

---

## Table: `stadiums`

**Purpose:** Static stadium metadata – historical relocation records.  
**Source:** Manually maintained seed file `data/stadiums.csv`.

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `stadium_id` | SERIAL | Auto-incrementing primary key | |
| `team` | VARCHAR(10) | Team abbreviation | Current abbreviation (LV, LAC, LA) |
| `stadium_name` | VARCHAR(100) | Stadium name | "SoFi Stadium", "Lambeau Field" |
| `city` | VARCHAR(100) | City location | |
| `state` | VARCHAR(50) | State location | |
| `latitude` | FLOAT | Geographic coordinates | For weather API queries |
| `longitude` | FLOAT | Geographic coordinates | For weather API queries |
| `timezone` | VARCHAR(50) | Local timezone | "America/New_York", "America/Chicago" |
| `surface` | VARCHAR(20) | Playing surface | "grass", "turf" |
| `roof_type` | VARCHAR(20) | Roof configuration | "open", "dome", "retractable" |
| `is_outdoor` | BOOLEAN | Outdoor indicator | FALSE for dome/retractable closed |
| `season_start` | INTEGER | First season at this stadium | |
| `season_end` | INTEGER | Last season at this stadium | NULL = current stadium |

**Historical Relocations Covered:**
- Raiders: Oakland (OAK) → Las Vegas (LV) – 2020
- Chargers: San Diego (SD) → Los Angeles (LAC) – 2017
- Rams: St. Louis (STL) → Los Angeles (LA) – 2016
- 49ers: Candlestick Park → Levi's Stadium – 2014

**Alias Handling:** Games with old abbreviations (OAK, SD, STL) join via CASE expression.

---

## Table: `weather`

**Purpose:** Historical game-time weather conditions.  
**Source:** Open-Meteo Historical Weather API (free tier).

| Field | Data Type | Description | Notes |
|-------|-----------|-------------|-------|
| `weather_id` | SERIAL | Auto-incrementing primary key | |
| `game_id` | VARCHAR(20) | nflverse game identifier | Foreign key to `games.game_id` |
| `stadium_id` | INTEGER | Stadium identifier | Foreign key to `stadiums.stadium_id` |
| `temperature_f` | FLOAT | Temperature in Fahrenheit | NULL for indoor games |
| `wind_speed_mph` | FLOAT | Wind speed in mph | NULL for indoor games |
| `wind_direction` | FLOAT | Wind direction in degrees | 0–359, NULL for indoor |
| `precipitation_inch` | FLOAT | Precipitation in inches | NULL for indoor games |
| `weather_condition` | VARCHAR(50) | General condition | Currently NULL – WMO codes not requested |
| `is_outdoor` | BOOLEAN | Outdoor game indicator | TRUE = outdoor, FALSE = dome/retractable |

**Unique Constraint:** `(game_id)` – ensures one row per game.  
**Data Strategy:** Indoor games have `is_outdoor = FALSE` and all weather fields NULL.

---

## Key Relationships

### Foreign Keys
- `games.stadium_id` → `stadiums.stadium_id`
- `play_by_play.game_id` → `games.game_id`
- `team_stats.game_id` → `games.game_id`
- `rosters.player_id` → `players.player_id`
- `injuries.player_id` → `players.player_id`
- `snap_counts.game_id` → `games.game_id`
- `depth_charts.player_id` → `players.player_id`
- `weather.game_id` → `games.game_id`
- `weather.stadium_id` → `stadiums.stadium_id`

### Composite Keys
- `rosters`: `(season, week, team, player_id)`
- `injuries`: `(season, week, team, player_id)`
- `team_stats`: `(game_id, team)`
- `snap_counts`: `(game_id, player_id)`
- `depth_charts`: `(season, week, team, player_id, depth_position)`

---

## ID System Notes

### Player ID Systems
1. **GSIS ID (`player_id` in most tables)**
   - Format: `00-0012345`
   - Used by: nflverse core data
   - Present in: `players`, `rosters`, `injuries`, `depth_charts`, `play_by_play`

2. **PFR Player ID (`player_id` in `snap_counts`)**
   - Format: `MahoPa00`
   - Used by: Pro Football Reference
   - Present in: `snap_counts` only

**Crosswalk Requirement:** Phase 2 must build mapping table to join snap counts with other player data.

### Game ID Format
- `YYYY_WW_HOME_AWAY`
- Example: `2023_01_KC_DET`
- Consistent across nflverse ecosystem
- `WW` is 2-digit week (01–18)

---

## Data Quality Notes

### Known Gaps
1. **Injury Data:** Missing 2014–2015 seasons (nflreadpy limitation)
2. **Turnover Tracking:** Schema lacks interception/fumble flags
3. **Player ID Mapping:** No `gsis_id` ↔ `pfr_player_id` crosswalk
4. **Practice Status:** Single field duplicated across Wed/Thu/Fri columns

### Completeness
- **100%:** Games, schedules, play-by-play, weather (2014–2024)
- **100%:** Snap counts, depth charts (2014–2024)
- **82%:** Injury data (2016–2024 only)
- **100%:** Stadium metadata (all historical relocations)

---

## Schema Evolution

### Phase 1 (Current)
- Complete historical data 2014–2024
- All tables populated
- Basic validation in place

### Phase 2 Planned
- Add `player_id_crosswalk` table
- Feature engineering views
- Advanced validation rules

### Phase 3 Proposed
- Add interception/fumble columns to `play_by_play`
- Add WMO weather condition codes
- Add betting line data

---

*Data dictionary version 1.0 – Phase 1 complete*  
*Maintained by: Lunch Pail data engineering team*  
*Contact: Update this document when schema changes*