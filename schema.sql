-- Lunch Pail NFL Prediction System
-- PostgreSQL Schema
-- Idempotent: all CREATE statements use IF NOT EXISTS

-- ============================================================
-- STADIUMS
-- ============================================================
CREATE TABLE IF NOT EXISTS stadiums (
    stadium_id    SERIAL PRIMARY KEY,
    team          VARCHAR(10)  NOT NULL,
    stadium_name  VARCHAR(100),
    city          VARCHAR(100),
    state         VARCHAR(50),
    latitude      FLOAT,
    longitude     FLOAT,
    timezone      VARCHAR(50),
    surface       VARCHAR(20),
    roof_type     VARCHAR(20),
    is_outdoor    BOOLEAN,
    season_start  INTEGER,
    season_end    INTEGER  -- NULL means current
);

-- ============================================================
-- GAMES
-- ============================================================
CREATE TABLE IF NOT EXISTS games (
    game_id      VARCHAR(20) PRIMARY KEY,
    season       INTEGER     NOT NULL,
    week         INTEGER     NOT NULL,
    home_team    VARCHAR(10) NOT NULL,
    away_team    VARCHAR(10) NOT NULL,
    home_score   INTEGER,
    away_score   INTEGER,
    result       INTEGER,  -- 1 = home win, 0 = away win, NULL = future game
    game_date    DATE,
    game_time    VARCHAR(10),
    stadium_id   INTEGER     REFERENCES stadiums(stadium_id),
    season_type  VARCHAR(10),
    week_type    VARCHAR(10)
);

-- ============================================================
-- SCHEDULES
-- ============================================================
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id  SERIAL PRIMARY KEY,
    game_id      VARCHAR(20) NOT NULL UNIQUE,
    season       INTEGER     NOT NULL,
    week         INTEGER     NOT NULL,
    home_team    VARCHAR(10),
    away_team    VARCHAR(10),
    game_date    DATE,
    weekday      VARCHAR(10),
    gametime     VARCHAR(10),
    home_score   INTEGER,
    away_score   INTEGER,
    result       FLOAT,
    overtime     BOOLEAN
);

-- ============================================================
-- PLAYERS
-- ============================================================
CREATE TABLE IF NOT EXISTS players (
    player_id    VARCHAR(20) PRIMARY KEY,
    player_name  VARCHAR(100),
    position     VARCHAR(10),
    birth_date   DATE,
    college      VARCHAR(100),
    draft_year   INTEGER,
    draft_round  INTEGER,
    draft_pick   INTEGER
);

-- ============================================================
-- PLAY BY PLAY
-- ============================================================
CREATE TABLE IF NOT EXISTS play_by_play (
    pbp_id               SERIAL PRIMARY KEY,
    game_id              VARCHAR(20) NOT NULL REFERENCES games(game_id),
    play_id              INTEGER     NOT NULL,
    posteam              VARCHAR(10),
    defteam              VARCHAR(10),
    down                 INTEGER,
    ydstogo              INTEGER,
    yardline_100         INTEGER,
    epa                  FLOAT,
    wpa                  FLOAT,
    air_yards            FLOAT,
    yards_gained         INTEGER,
    play_type            VARCHAR(30),
    passer_player_id     VARCHAR(20),
    rusher_player_id     VARCHAR(20),
    receiver_player_id   VARCHAR(20),
    qb_dropback          INTEGER,
    qb_scramble          INTEGER,
    pass_attempt         INTEGER,
    rush_attempt         INTEGER,
    penalty              INTEGER
);

-- ============================================================
-- TEAM STATS
-- ============================================================
CREATE TABLE IF NOT EXISTS team_stats (
    stat_id                        SERIAL PRIMARY KEY,
    game_id                        VARCHAR(20) NOT NULL REFERENCES games(game_id),
    team                           VARCHAR(10) NOT NULL,
    offensive_epa_per_play         FLOAT,
    defensive_epa_per_play_allowed FLOAT,
    turnover_differential          INTEGER,
    third_down_conv_rate_off       FLOAT,
    third_down_conv_rate_def       FLOAT,
    red_zone_efficiency_off        FLOAT,
    red_zone_efficiency_def        FLOAT,
    points_scored                  INTEGER,
    points_allowed                 INTEGER,
    UNIQUE (game_id, team)
);

-- ============================================================
-- ROSTERS
-- ============================================================
CREATE TABLE IF NOT EXISTS rosters (
    roster_id            SERIAL PRIMARY KEY,
    season               INTEGER     NOT NULL,
    week                 INTEGER     NOT NULL,
    team                 VARCHAR(10) NOT NULL,
    player_id            VARCHAR(20) REFERENCES players(player_id),
    player_name          VARCHAR(100),
    position             VARCHAR(10),
    depth_chart_position VARCHAR(20),
    jersey_number        INTEGER,
    status               VARCHAR(20),
    UNIQUE (season, week, team, player_id)
);

-- ============================================================
-- INJURIES
-- ============================================================
CREATE TABLE IF NOT EXISTS injuries (
    injury_id              SERIAL PRIMARY KEY,
    season                 INTEGER     NOT NULL,
    week                   INTEGER     NOT NULL,
    team                   VARCHAR(10) NOT NULL,
    player_id              VARCHAR(20) REFERENCES players(player_id),
    player_name            VARCHAR(100),
    position               VARCHAR(10),
    report_primary_injury  VARCHAR(100),
    practice_status_wed    VARCHAR(20),
    practice_status_thu    VARCHAR(20),
    practice_status_fri    VARCHAR(20),
    game_status            VARCHAR(20),
    UNIQUE (season, week, team, player_id)
);

-- ============================================================
-- SNAP COUNTS
-- ============================================================
CREATE TABLE IF NOT EXISTS snap_counts (
    snap_id        SERIAL PRIMARY KEY,
    game_id        VARCHAR(20) NOT NULL REFERENCES games(game_id),
    season         INTEGER     NOT NULL,
    week           INTEGER     NOT NULL,
    team           VARCHAR(10) NOT NULL,
    player_id      VARCHAR(20) REFERENCES players(player_id),
    player_name    VARCHAR(100),
    position       VARCHAR(10),
    offense_snaps  INTEGER,
    offense_pct    FLOAT,
    defense_snaps  INTEGER,
    defense_pct    FLOAT,
    UNIQUE (game_id, player_id)
);

-- ============================================================
-- DEPTH CHARTS
-- ============================================================
CREATE TABLE IF NOT EXISTS depth_charts (
    depth_id        SERIAL PRIMARY KEY,
    season          INTEGER     NOT NULL,
    week            INTEGER     NOT NULL,
    team            VARCHAR(10) NOT NULL,
    player_id       VARCHAR(20) REFERENCES players(player_id),
    player_name     VARCHAR(100),
    position        VARCHAR(10),
    depth_team      INTEGER,
    depth_position  VARCHAR(20),
    UNIQUE (season, week, team, player_id, depth_position)
);

-- ============================================================
-- WEATHER
-- ============================================================
CREATE TABLE IF NOT EXISTS weather (
    weather_id           SERIAL PRIMARY KEY,
    game_id              VARCHAR(20) NOT NULL REFERENCES games(game_id),
    stadium_id           INTEGER     REFERENCES stadiums(stadium_id),
    temperature_f        FLOAT,
    wind_speed_mph       FLOAT,
    wind_direction       FLOAT,
    precipitation_inch   FLOAT,
    weather_condition    VARCHAR(50),
    is_outdoor           BOOLEAN,
    UNIQUE (game_id)
);

-- ============================================================
-- INDEXES
-- ============================================================

-- games
CREATE INDEX IF NOT EXISTS idx_games_season       ON games (season);
CREATE INDEX IF NOT EXISTS idx_games_week         ON games (week);
CREATE INDEX IF NOT EXISTS idx_games_home_team    ON games (home_team);
CREATE INDEX IF NOT EXISTS idx_games_away_team    ON games (away_team);

-- play_by_play
CREATE INDEX IF NOT EXISTS idx_pbp_game_id   ON play_by_play (game_id);
CREATE INDEX IF NOT EXISTS idx_pbp_posteam   ON play_by_play (posteam);
CREATE INDEX IF NOT EXISTS idx_pbp_defteam   ON play_by_play (defteam);

-- team_stats
CREATE INDEX IF NOT EXISTS idx_team_stats_game_id ON team_stats (game_id);
CREATE INDEX IF NOT EXISTS idx_team_stats_team    ON team_stats (team);

-- rosters
CREATE INDEX IF NOT EXISTS idx_rosters_season    ON rosters (season);
CREATE INDEX IF NOT EXISTS idx_rosters_week      ON rosters (week);
CREATE INDEX IF NOT EXISTS idx_rosters_team      ON rosters (team);
CREATE INDEX IF NOT EXISTS idx_rosters_player_id ON rosters (player_id);

-- injuries
CREATE INDEX IF NOT EXISTS idx_injuries_season ON injuries (season);
CREATE INDEX IF NOT EXISTS idx_injuries_week   ON injuries (week);
CREATE INDEX IF NOT EXISTS idx_injuries_team   ON injuries (team);

-- snap_counts
CREATE INDEX IF NOT EXISTS idx_snap_counts_game_id   ON snap_counts (game_id);
CREATE INDEX IF NOT EXISTS idx_snap_counts_team      ON snap_counts (team);
CREATE INDEX IF NOT EXISTS idx_snap_counts_player_id ON snap_counts (player_id);

-- depth_charts
CREATE INDEX IF NOT EXISTS idx_depth_charts_season ON depth_charts (season);
CREATE INDEX IF NOT EXISTS idx_depth_charts_week   ON depth_charts (week);
CREATE INDEX IF NOT EXISTS idx_depth_charts_team   ON depth_charts (team);

-- weather
CREATE INDEX IF NOT EXISTS idx_weather_game_id ON weather (game_id);
