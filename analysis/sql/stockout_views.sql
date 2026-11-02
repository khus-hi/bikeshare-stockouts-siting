-- Stockout KPI views: empty/full minutes, estimated lost rides, peak problem stations.
-- Needs the GBFS views (ingest/sql/duckdb_views.sql) and trip views (ingest/sql/trips_views.sql)
-- first. Normally built by: python analysis/build_views.py
-- KPI definitions: docs/requirements.md section 4. All times below are naive timestamps,
-- either UTC (*_utc) or the station's local clock (*_local), so results never depend on
-- the DuckDB session's time zone.

-- 1. Observed time ---------------------------------------------------------------------
-- The collector stores a station row only when something changes, so "no row" can mean
-- "unchanged" or "we were not polling". The poll log settles it: time between two
-- consecutive successful polls of a system is observed if the gap is <= 15 minutes
-- (a single missed 5-minute poll is tolerated; longer gaps are excluded, per the KPI rule).
CREATE OR REPLACE VIEW poll_segments AS
SELECT system,
       poll_ts AT TIME ZONE 'UTC'  AS seg_start_utc,
       next_ts AT TIME ZONE 'UTC'  AS seg_end_utc
FROM (
    SELECT system, poll_ts,
           lead(poll_ts) OVER (PARTITION BY system ORDER BY poll_ts) AS next_ts
    FROM polls
    WHERE ok
)
WHERE next_ts IS NOT NULL
  AND next_ts - poll_ts <= INTERVAL 15 MINUTE;

-- Coverage per system per UTC day (share of the day inside an observed segment).
CREATE OR REPLACE VIEW poll_coverage_daily AS
SELECT system,
       CAST(seg_start_utc AS DATE) AS date_utc,
       round(sum(date_diff('second', seg_start_utc, seg_end_utc)) / 86400.0, 4) AS coverage_share
FROM poll_segments
GROUP BY ALL
ORDER BY system, date_utc;

-- 2. Station state in each observed segment ----------------------------------------------
-- Each segment takes the station's last saved state at or before the segment's start
-- (ASOF join). The search stays within the same UTC day: the collector writes every
-- station in the first successful poll of each day (keyframe), so a station that left the
-- feed stops being counted at the end of the last day it appeared.
CREATE OR REPLACE VIEW station_status_utc AS
SELECT system, station_id,
       poll_ts AT TIME ZONE 'UTC'               AS poll_utc,
       CAST(poll_ts AT TIME ZONE 'UTC' AS DATE) AS date_utc,
       num_bikes_available, num_docks_available, is_installed, is_renting, is_returning
FROM station_status_changes;

CREATE OR REPLACE VIEW station_segments AS
WITH station_days AS (
    SELECT DISTINCT system, station_id, date_utc FROM station_status_utc
), grid AS (
    SELECT d.system, d.station_id, d.date_utc, p.seg_start_utc, p.seg_end_utc
    FROM poll_segments p
    JOIN station_days d
      ON d.system = p.system AND d.date_utc = CAST(p.seg_start_utc AS DATE)
)
SELECT g.system, g.station_id, g.seg_start_utc, g.seg_end_utc,
       s.num_bikes_available, s.num_docks_available,
       (s.is_installed = 1 AND s.is_renting = 1 AND s.num_bikes_available = 0)  AS is_empty,
       (s.is_installed = 1 AND s.is_returning = 1 AND s.num_docks_available = 0) AS is_full,
       NOT (s.is_installed = 1 AND s.is_renting = 1)                            AS out_of_service
FROM grid g
ASOF JOIN station_status_utc s
  ON  s.system = g.system
  AND s.station_id = g.station_id
  AND s.date_utc = g.date_utc
  AND s.poll_utc <= g.seg_start_utc;

-- 3. Minutes empty / full per station per hour ------------------------------------------
-- Segments are at most 15 minutes, so one can cross at most one hour boundary: split it
-- into the part before and the part after.
CREATE OR REPLACE VIEW station_hour_status AS
WITH pieces AS (
    SELECT *, date_trunc('hour', seg_start_utc) AS hour_utc,
           seg_start_utc AS p0,
           least(seg_end_utc, date_trunc('hour', seg_start_utc) + INTERVAL 1 HOUR) AS p1
    FROM station_segments
    UNION ALL
    SELECT *, date_trunc('hour', seg_start_utc) + INTERVAL 1 HOUR AS hour_utc,
           date_trunc('hour', seg_start_utc) + INTERVAL 1 HOUR AS p0,
           seg_end_utc AS p1
    FROM station_segments
    WHERE seg_end_utc > date_trunc('hour', seg_start_utc) + INTERVAL 1 HOUR
), m AS (
    SELECT system, station_id, hour_utc,
           date_diff('second', p0, p1) / 60.0 AS minutes,
           is_empty, is_full, out_of_service
    FROM pieces
)
SELECT m.system,
       s.city,
       m.station_id,
       m.hour_utc,
       timezone(s.tz, timezone('UTC', m.hour_utc))                  AS hour_local,
       round(sum(minutes), 2)                                       AS observed_minutes,
       round(sum(minutes) FILTER (WHERE is_empty), 2)               AS minutes_empty,
       round(sum(minutes) FILTER (WHERE is_full), 2)                AS minutes_full,
       round(sum(minutes) FILTER (WHERE out_of_service), 2)         AS minutes_out_of_service
FROM m
JOIN systems s USING (system)
GROUP BY ALL;
-- FILTER sums are NULL when no minutes qualify; wrap in the view below.

CREATE OR REPLACE VIEW station_hour_stockouts AS
SELECT h.system, h.city, h.station_id,
       st.short_name AS station_code, st.name AS station_name, st.capacity, st.lat, st.lon,
       h.hour_utc, h.hour_local,
       h.observed_minutes,
       coalesce(h.minutes_empty, 0)          AS minutes_empty,
       coalesce(h.minutes_full, 0)           AS minutes_full,
       coalesce(h.minutes_out_of_service, 0) AS minutes_out_of_service
FROM station_hour_status h
LEFT JOIN stations st USING (system, station_id);

-- 4. Typical demand per station and hour of week -----------------------------------------
-- demand_per_hour (lambda in docs/requirements.md) = average departures in that hour of the week (ISO day 1=Mon..7=Sun, local hour),
-- from trip history in the same calendar month(s) as the snapshots, before the snapshots
-- began (e.g. snapshots in Nov 2026 -> trips from Nov 2025). Hours with zero departures
-- count as zeros: the denominator is the number of times that hour of the week occurred
-- between the station's first and last trip in the reference period.
-- Range: Poisson 95% interval on the count (count +/- 1.96*sqrt(count)) / n; when the
-- count is 0 the upper bound uses the "rule of three" (3 / n).
-- Caveat: trip history only shows rides that happened. Hours when the station was empty
-- in the reference period pull it down, so lost rides are a conservative estimate.
CREATE OR REPLACE VIEW demand_reference_trips AS
SELECT t.system, t.start_station_id AS station_code, t.started_at
FROM trips_clean t
WHERE t.start_station_id IS NOT NULL
  AND month(t.started_at) IN (SELECT DISTINCT month(hour_local) FROM station_hour_status)
  AND t.started_at < (SELECT min(hour_local) FROM station_hour_status);

CREATE OR REPLACE VIEW typical_demand AS
WITH span AS (
    SELECT system, station_code,
           CAST(min(started_at) AS DATE) AS d0, CAST(max(started_at) AS DATE) AS d1
    FROM demand_reference_trips
    GROUP BY ALL
), days AS (
    SELECT system, station_code,
           CAST(unnest(generate_series(CAST(d0 AS TIMESTAMP), CAST(d1 AS TIMESTAMP), INTERVAL 1 DAY)) AS DATE) AS d
    FROM span
), n_by_dow AS (
    SELECT system, station_code, isodow(d) AS dow, count(*) AS n_hours
    FROM days GROUP BY ALL
), counts AS (
    SELECT system, station_code, isodow(started_at) AS dow, hour(started_at) AS hr,
           count(*) AS departures
    FROM demand_reference_trips GROUP BY ALL
), grid AS (
    SELECT n.*, h.hr FROM n_by_dow n CROSS JOIN (SELECT range AS hr FROM range(24)) h
)
SELECT g.system, g.station_code, g.dow, g.hr,
       g.n_hours,
       coalesce(c.departures, 0) AS departures,
       coalesce(c.departures, 0) / g.n_hours AS demand_per_hour,
       greatest(coalesce(c.departures, 0) - 1.96 * sqrt(coalesce(c.departures, 0)), 0) / g.n_hours AS demand_low,
       CASE WHEN coalesce(c.departures, 0) = 0 THEN 3.0 / g.n_hours
            ELSE (c.departures + 1.96 * sqrt(c.departures)) / g.n_hours END AS demand_high
FROM grid g
LEFT JOIN counts c USING (system, station_code, dow, hr);

-- 5. Estimated lost rides per station-hour ------------------------------------------------
-- lost = demand_per_hour(station, hour of week) * (minutes empty / 60). Unobserved minutes count
-- as not empty, so a partly-covered hour can only understate. Stations with no trips in
-- the reference period (new, renamed or code changed) get NULL and has_demand_estimate = false.
CREATE OR REPLACE VIEW lost_rides_station_hour AS
SELECT h.*,
       d.demand_per_hour, d.demand_low, d.demand_high, d.n_hours AS demand_n_hours,
       d.demand_per_hour IS NOT NULL AS has_demand_estimate,
       d.demand_per_hour * h.minutes_empty / 60.0 AS est_lost_rides,
       d.demand_low  * h.minutes_empty / 60.0 AS est_lost_rides_low,
       d.demand_high * h.minutes_empty / 60.0 AS est_lost_rides_high
FROM station_hour_stockouts h
LEFT JOIN typical_demand d
  ON  d.system = h.system
  AND d.station_code = h.station_code
  AND d.dow = isodow(h.hour_local)
  AND d.hr = hour(h.hour_local);

-- Per-station totals over the whole snapshot period.
CREATE OR REPLACE VIEW station_stockout_summary AS
SELECT system, city, station_id, station_code, station_name, capacity, lat, lon,
       round(sum(observed_minutes) / 60, 1)   AS observed_hours,
       round(sum(minutes_empty) / 60, 2)      AS station_hours_empty,
       round(sum(minutes_full) / 60, 2)       AS station_hours_full,
       round(sum(minutes_empty) / sum(observed_minutes), 4) AS share_empty,
       round(sum(minutes_full) / sum(observed_minutes), 4)  AS share_full,
       bool_and(has_demand_estimate)          AS has_demand_estimate,
       round(sum(est_lost_rides), 1)          AS est_lost_rides,
       round(sum(est_lost_rides_low), 1)      AS est_lost_rides_low,
       round(sum(est_lost_rides_high), 1)     AS est_lost_rides_high
FROM lost_rides_station_hour
GROUP BY ALL;

-- 6. Top 20 problem stations per city, weekday peaks --------------------------------------
-- Weekdays Mon-Fri (local); AM peak 7:00-9:00 (hours 7, 8), PM peak 16:00-19:00 (16, 17, 18).
-- Ranked by the share of observed peak time the station was empty or full, among stations
-- observed for at least half of the system's best-covered station in that window (so a
-- station that only existed for a day can't top the list). Ties: more estimated lost rides first.
CREATE OR REPLACE VIEW peak_problem_stations AS
WITH peak AS (
    SELECT *,
           CASE WHEN hour(hour_local) IN (7, 8) THEN 'weekday 7-9am' ELSE 'weekday 4-7pm' END AS peak_window
    FROM lost_rides_station_hour
    WHERE isodow(hour_local) <= 5
      AND hour(hour_local) IN (7, 8, 16, 17, 18)
), agg AS (
    SELECT system, city, peak_window, station_id, station_code, station_name, capacity,
           sum(observed_minutes) AS observed_minutes,
           sum(minutes_empty)    AS minutes_empty,
           sum(minutes_full)     AS minutes_full,
           sum(est_lost_rides)       AS est_lost_rides,
           sum(est_lost_rides_low)   AS est_lost_rides_low,
           sum(est_lost_rides_high)  AS est_lost_rides_high
    FROM peak
    GROUP BY ALL
), eligible AS (
    SELECT * FROM agg
    QUALIFY observed_minutes >= 0.5 * max(observed_minutes) OVER (PARTITION BY system, peak_window)
)
SELECT system, city, peak_window,
       row_number() OVER w AS rank,
       station_code, station_name, capacity,
       round(observed_minutes / 60, 1)                       AS observed_hours,
       round(minutes_empty / 60, 2)                          AS hours_empty,
       round(minutes_full / 60, 2)                           AS hours_full,
       round((minutes_empty + minutes_full) / observed_minutes, 4) AS stockout_share,
       round(est_lost_rides, 1)      AS est_lost_rides,
       round(est_lost_rides_low, 1)  AS est_lost_rides_low,
       round(est_lost_rides_high, 1) AS est_lost_rides_high,
       station_id
FROM eligible
WINDOW w AS (PARTITION BY system, peak_window
             ORDER BY (minutes_empty + minutes_full) / observed_minutes DESC,
                      est_lost_rides DESC NULLS LAST, station_code)
QUALIFY row_number() OVER w <= 20
ORDER BY system, peak_window, rank;

-- Diagnostics: how many GBFS stations can be matched to trip-history station codes.
CREATE OR REPLACE VIEW station_match_rate AS
SELECT system,
       count(*) AS gbfs_stations,
       count(*) FILTER (WHERE station_code IN (SELECT station_code FROM typical_demand t
                                               WHERE t.system = s.system)) AS with_demand_estimate,
       round(with_demand_estimate / gbfs_stations, 3) AS share_matched
FROM (SELECT DISTINCT system, station_id, station_code FROM station_hour_stockouts) s
GROUP BY system;
