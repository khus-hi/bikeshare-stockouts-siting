-- Optional Postgres sink for the GBFS collector (switched on by POSTGRES_DSN in .env).
-- Parquet files under data/gbfs/ remain the source of truth; these tables mirror them.

CREATE TABLE IF NOT EXISTS station_status_changes (
    system               TEXT        NOT NULL,
    poll_ts              TIMESTAMPTZ NOT NULL,
    feed_last_updated    TIMESTAMPTZ,
    station_id           TEXT        NOT NULL,
    num_bikes_available  INTEGER,
    num_ebikes_available INTEGER,
    num_bikes_disabled   INTEGER,
    num_docks_available  INTEGER,
    num_docks_disabled   INTEGER,
    is_installed         INTEGER,
    is_renting           INTEGER,
    is_returning         INTEGER,
    last_reported        TIMESTAMPTZ,
    is_keyframe          BOOLEAN,
    PRIMARY KEY (system, station_id, poll_ts)
);

CREATE TABLE IF NOT EXISTS station_information_snapshots (
    system      TEXT        NOT NULL,
    poll_ts     TIMESTAMPTZ NOT NULL,
    station_id  TEXT        NOT NULL,
    short_name  TEXT,
    name        TEXT,
    lat         DOUBLE PRECISION,
    lon         DOUBLE PRECISION,
    capacity    INTEGER,
    region_id   TEXT,
    PRIMARY KEY (system, station_id, poll_ts)
);

CREATE TABLE IF NOT EXISTS polls (
    system                TEXT        NOT NULL,
    poll_ts               TIMESTAMPTZ NOT NULL,
    ok                    BOOLEAN     NOT NULL,
    error                 TEXT,
    feed_last_updated     TIMESTAMPTZ,
    n_stations            INTEGER,
    n_status_rows_written INTEGER,
    is_keyframe           BOOLEAN,
    info_changed          BOOLEAN,
    fetch_seconds         DOUBLE PRECISION,
    PRIMARY KEY (system, poll_ts)
);
