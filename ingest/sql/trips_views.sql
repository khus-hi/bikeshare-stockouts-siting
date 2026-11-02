-- Views over the normalized trip-history Parquet written by ingest/trips_download.py.
-- Run from the repo root (paths are relative), normally via: python analysis/build_views.py

-- One row per system: the city label and the local time zone its trip files use.
CREATE OR REPLACE TABLE systems AS
SELECT * FROM (VALUES
    ('bluebikes', 'boston',  'America/New_York'),
    ('citibike',  'nyc',     'America/New_York'),
    ('divvy',     'chicago', 'America/Chicago')
) AS t(system, city, tz);

-- All trips, all systems. `system` and `city` are stored inside each file, so hive
-- partitioning is off (the folder names only organise the files).
-- started_at / ended_at are local wall-clock time; *_utc are the same instants in UTC.
CREATE OR REPLACE VIEW trips AS
SELECT * FROM read_parquet('data/trips/parquet/*/*/*.parquet', hive_partitioning = false);

-- Trips used for demand: valid times, positive duration, under 24 hours.
-- (The operators already drop trips under 60 s and staff test rides before publishing.)
CREATE OR REPLACE VIEW trips_clean AS
SELECT *, date_diff('second', started_at, ended_at) / 60.0 AS duration_min
FROM trips
WHERE started_at IS NOT NULL
  AND ended_at IS NOT NULL
  AND ended_at > started_at
  AND ended_at < started_at + INTERVAL 24 HOUR;

-- Hourly departures and arrivals per station (local clock hour).
-- Sparse: a station-hour with no trips has no row, i.e. 0 departures and 0 arrivals.
-- A departure counts in the hour it started, an arrival in the hour it ended.
-- station_id here is the operator's public station code, which equals the GBFS
-- station_information.short_name (e.g. Bluebikes M32006, Citi Bike 6140.05, Divvy CHI00252).
CREATE OR REPLACE VIEW station_hour_trips AS
WITH dep AS (
    SELECT system, start_station_id AS station_code, date_trunc('hour', started_at) AS hour_local,
           count(*) AS departures
    FROM trips_clean WHERE start_station_id IS NOT NULL
    GROUP BY ALL
), arr AS (
    SELECT system, end_station_id AS station_code, date_trunc('hour', ended_at) AS hour_local,
           count(*) AS arrivals
    FROM trips_clean WHERE end_station_id IS NOT NULL
    GROUP BY ALL
)
SELECT system,
       s.city,
       station_code,
       hour_local,
       timezone('UTC', timezone(s.tz, hour_local)) AS hour_utc,
       coalesce(dep.departures, 0) AS departures,
       coalesce(arr.arrivals, 0)   AS arrivals,
       coalesce(arr.arrivals, 0) - coalesce(dep.departures, 0) AS net_inflow
FROM dep
FULL OUTER JOIN arr USING (system, station_code, hour_local)
JOIN systems s USING (system);

-- Data-quality summary per system and month, for the README and sanity checks.
CREATE OR REPLACE VIEW trips_quality AS
SELECT system,
       strftime(started_at, '%Y-%m') AS month,
       count(*)                                          AS trips,
       count(*) FILTER (WHERE start_station_id IS NULL)  AS no_start_station,
       count(*) FILTER (WHERE end_station_id IS NULL)    AS no_end_station,
       count(*) FILTER (WHERE ended_at <= started_at
                          OR ended_at >= started_at + INTERVAL 24 HOUR) AS bad_duration,
       round(median(date_diff('second', started_at, ended_at)) / 60.0, 1) AS median_minutes
FROM trips
WHERE started_at IS NOT NULL
GROUP BY ALL
ORDER BY system, month;
