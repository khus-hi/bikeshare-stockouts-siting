-- Views over the collector's Parquet files. Run from the repo root:
--   duckdb data/gbfs.duckdb < ingest/sql/duckdb_views.sql
-- The glob matches both today's small part files and compacted daily files.

CREATE OR REPLACE VIEW station_status_changes AS
SELECT * EXCLUDE (date) FROM read_parquet('data/gbfs/station_status/*/*/*.parquet', hive_partitioning = true);

CREATE OR REPLACE VIEW station_information_snapshots AS
SELECT * EXCLUDE (date) FROM read_parquet('data/gbfs/station_information/*/*/*.parquet', hive_partitioning = true);

CREATE OR REPLACE VIEW polls AS
SELECT * EXCLUDE (date) FROM read_parquet('data/gbfs/polls/*/*.parquet', hive_partitioning = true);

-- Latest known info per station.
CREATE OR REPLACE VIEW stations AS
SELECT * FROM station_information_snapshots
QUALIFY row_number() OVER (PARTITION BY system, station_id ORDER BY poll_ts DESC) = 1;

-- Each saved row holds until the next saved row for the same station ("valid_to").
-- Intervals that span a failed or missing poll still need to be excluded with the
-- polls table, as defined for the KPIs in docs/requirements.md.
CREATE OR REPLACE VIEW station_status_intervals AS
SELECT *,
       lead(poll_ts) OVER (PARTITION BY system, station_id ORDER BY poll_ts) AS valid_to
FROM station_status_changes;
