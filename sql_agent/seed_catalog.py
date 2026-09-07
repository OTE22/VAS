"""
Verified seed catalog for the SQL knowledge base
================================================

Every entry is a (question, sql, purpose) triple that has been EXECUTED
against the live schema (scratch `catalog_check.py`, placeholders
substituted) and read for meaning. Nothing here is learned from a
conversation: the knowledge base holds only verified examples, and this
module is where the verified ones live.

Placeholders the SQL specialist substitutes with the user's literals:
  PERSON_NAME   - the person the user named (matched case-insensitively)
  OTHER_PERSON  - the second person in a two-person question
  CAMERA_NAME   - the camera / location the user named
Example dates are literal ("2026-08-17"); the purpose says to substitute.

Rules every entry follows:
  * NULL, '', 'Unknown' and 'person_<n>' face names are placeholders for an
    unidentified face and are never counted or listed as a person.
  * Counts come from `detections`, never from `pipelines.total_detections`
    (a lagging cache).
  * A camera is named by COALESCE(p.location_name, p.pipeline_id).
  * Absence ("never", "no detections") uses LEFT JOIN + HAVING or NOT EXISTS;
    an inner join can never return a camera with nothing.
  * Durations come from EXTRACT(EPOCH FROM (later - earlier)) / 60 (minutes).
  * Aliases always use AS, and never a bare `day` / `hour` / `week`.
"""

IDENT = ("f.name IS NOT NULL AND f.name <> '' AND LOWER(f.name) NOT LIKE 'unknown%' "
         "AND LOWER(f.name) NOT LIKE 'person_%'")
UNID = ("(f.name IS NULL OR f.name = '' OR LOWER(f.name) LIKE 'unknown%' "
        "OR LOWER(f.name) LIKE 'person_%')")
CAM = "COALESCE(p.location_name, p.pipeline_id)"
FDP = ("FROM faces f\nJOIN detections d ON f.detection_id = d.id\n"
       "JOIN pipelines p ON p.pipeline_id = d.pipeline_id")
DP = "FROM detections d\nJOIN pipelines p ON p.pipeline_id = d.pipeline_id"
PERSON = "LOWER(f.name) LIKE LOWER('%PERSON_NAME%')"
CAMF = "LOWER(COALESCE(p.location_name, p.pipeline_id)) LIKE LOWER('%CAMERA_NAME%')"
GROUP_CAM = "GROUP BY p.pipeline_id, p.location_name"


def E(question, sql, purpose):
    return {"question": question, "sql": sql.strip(), "purpose": purpose}


CATALOG = []

# ---------------------------------------------------------------------------
# 1. The ten truths of the 2026-09-06 unseen battery (the bot failed 4.5/10)
# ---------------------------------------------------------------------------
CATALOG += [
E("Which camera has the highest share of unidentified faces among its detections, and what is that share as a percentage",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS faces,
    COUNT(*) FILTER (WHERE {UNID}) AS unidentified_faces,
    ROUND(100.0 * COUNT(*) FILTER (WHERE {UNID}) / COUNT(*), 1) AS unidentified_percent
{FDP}
{GROUP_CAM}
ORDER BY unidentified_percent DESC, faces DESC""",
"Unidentified share per camera as a percentage of that camera's faces (placeholders = unidentified); highest first, one row per camera"),
E("How many detections happened on each day of the week, and which weekday is the busiest",
f"""SELECT TRIM(TO_CHAR(d.timestamp, 'Day')) AS weekday, EXTRACT(DOW FROM d.timestamp)::int AS weekday_number,
    COUNT(*) AS detections
FROM detections d
GROUP BY 1, 2
ORDER BY detections DESC""",
"Detections per weekday (0 = Sunday); the first row is the busiest weekday"),
E("What is the longest time PERSON_NAME went without being detected, and between which two detections did that gap occur",
f"""WITH ordered AS (
    SELECT d.timestamp AS seen_at, LAG(d.timestamp) OVER (ORDER BY d.timestamp) AS previous_seen_at
    FROM faces f JOIN detections d ON f.detection_id = d.id
    WHERE {PERSON})
SELECT previous_seen_at, seen_at, ROUND(EXTRACT(EPOCH FROM (seen_at - previous_seen_at)) / 60, 1) AS gap_minutes
FROM ordered
WHERE previous_seen_at IS NOT NULL
ORDER BY gap_minutes DESC
LIMIT 1""",
"The longest gap between one person's consecutive detections with both endpoints: LAG over their detections, one row"),
E("Which cameras have seen both PERSON_NAME and OTHER_PERSON",
f"""SELECT {CAM} AS camera_name
FROM pipelines p
WHERE EXISTS (SELECT 1 FROM detections d JOIN faces f ON f.detection_id = d.id
              WHERE d.pipeline_id = p.pipeline_id AND {PERSON})
  AND EXISTS (SELECT 1 FROM detections d JOIN faces f ON f.detection_id = d.id
              WHERE d.pipeline_id = p.pipeline_id AND LOWER(f.name) LIKE LOWER('%OTHER_PERSON%'))
ORDER BY camera_name""",
"Cameras that saw BOTH people (set intersection with two EXISTS); empty means no camera saw both"),
E("In what order did PERSON_NAME move between cameras on 2026-08-17? List each change of camera with the time",
f"""WITH steps AS (
    SELECT d.timestamp AS seen_at, {CAM} AS camera_name,
        LAG({CAM}) OVER (ORDER BY d.timestamp) AS previous_camera
    {FDP}
    WHERE {PERSON} AND DATE(d.timestamp) = DATE '2026-08-17')
SELECT seen_at, previous_camera, camera_name
FROM steps
WHERE previous_camera IS DISTINCT FROM camera_name
ORDER BY seen_at""",
"A person's camera-to-camera movement on one day: only the rows where the camera CHANGED, in time order (substitute the date)"),
E("On average how many detections does a camera record per day on the days it is active, and which camera exceeds that average by the most",
f"""WITH per_camera_day AS (
    SELECT {CAM} AS camera_name, DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
    {DP}
    {GROUP_CAM}, DATE(d.timestamp)),
per_camera AS (
    SELECT camera_name, ROUND(AVG(detections), 1) AS avg_per_active_day FROM per_camera_day GROUP BY camera_name),
overall AS (
    SELECT ROUND(AVG(detections), 1) AS overall_avg_per_camera_day FROM per_camera_day)
SELECT per_camera.camera_name, per_camera.avg_per_active_day, overall.overall_avg_per_camera_day,
    ROUND(per_camera.avg_per_active_day - overall.overall_avg_per_camera_day, 1) AS above_average_by
FROM per_camera
CROSS JOIN overall
ORDER BY above_average_by DESC""",
"Average detections per camera per ACTIVE day (a day with at least one detection) and each camera's distance above it"),
E("At which hour of the day is the average similarity score of recognized faces the highest, and what is that average",
f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day,
    ROUND(AVG(f.similarity)::numeric, 3) AS avg_similarity, COUNT(*) AS recognized_faces
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY 1
ORDER BY avg_similarity DESC""",
"Average similarity of IDENTIFIED faces per hour of day, best first (placeholders excluded)"),
E("What percentage of all detections happened during the three busiest hours of the day combined",
f"""WITH per_hour AS (
    SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
    FROM detections d GROUP BY 1),
top3 AS (SELECT * FROM per_hour ORDER BY detections DESC LIMIT 3)
SELECT STRING_AGG(hour_of_day::text || ':00 (' || detections || ')', ', ' ORDER BY detections DESC) AS busiest_hours,
    SUM(detections) AS detections_in_top3,
    (SELECT SUM(detections) FROM per_hour) AS total_detections,
    ROUND(100.0 * SUM(detections) / (SELECT SUM(detections) FROM per_hour), 1) AS percent_of_all
FROM top3""",
"Share of all detections that fall in the three busiest hours: top-3 hour sum over the total, one row"),
E("Which camera saw PERSON_NAME, and how many minutes passed between their first and last detection there",
f"""SELECT {CAM} AS camera_name, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen,
    ROUND(EXTRACT(EPOCH FROM (MAX(d.timestamp) - MIN(d.timestamp))) / 60, 1) AS span_minutes,
    COUNT(*) AS detections
{FDP}
WHERE {PERSON}
{GROUP_CAM}
ORDER BY detections DESC""",
"Per camera: first and last sighting of one person and the minutes between them (EPOCH difference / 60)"),
E("Which week had the most detections, and how many were there in that week",
f"""SELECT DATE_TRUNC('week', d.timestamp)::date AS week_starting_monday, COUNT(*) AS detections
FROM detections d
GROUP BY 1
ORDER BY detections DESC
LIMIT 1""",
"The busiest ISO week (Monday start) and its count, one row"),
]

# ---------------------------------------------------------------------------
# 1b. The third battery (2026-09-06): shapes the model could not build unaided
# ---------------------------------------------------------------------------
# 1c. The fourth battery (2026-09-06): truths the model mis-built or mis-read
# ---------------------------------------------------------------------------
CATALOG += [
E("On the day with the most detections, what percentage of that day's detections happened in its single busiest hour",
f"""WITH busiest_day AS (
    SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS day_total
    FROM detections d
    GROUP BY DATE(d.timestamp)
    ORDER BY day_total DESC, detection_day
    LIMIT 1),
busiest_hour AS (
    SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS hour_total
    FROM detections d
    WHERE DATE(d.timestamp) = (SELECT detection_day FROM busiest_day)
    GROUP BY EXTRACT(HOUR FROM d.timestamp)
    ORDER BY hour_total DESC, hour_of_day
    LIMIT 1)
SELECT bd.detection_day, bd.day_total, bh.hour_of_day, bh.hour_total,
    ROUND(100.0 * bh.hour_total / bd.day_total, 1) AS percentage_in_busiest_hour
FROM busiest_day bd, busiest_hour bh""",
"The busiest day first (a DATE, compared with DATE), then that day's busiest hour, then the share; one row"),
E("Which camera has the largest share of all unidentified faces, and what percentage of the unidentified faces is that",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS unidentified_faces,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS share_of_all_unidentified_percent
{FDP}
WHERE {UNID}
{GROUP_CAM}
ORDER BY unidentified_faces DESC, camera_name""",
"Share of ALL unidentified faces per camera (denominator = every unidentified face, via SUM() OVER ()), largest first; ties share the top count. Not the share of unidentified faces WITHIN a camera"),
E("Which cameras had detections on exactly one day, and which day was it for each",
f"""SELECT {CAM} AS camera_name, MIN(DATE(d.timestamp)) AS the_only_day, COUNT(*) AS detections
{DP}
{GROUP_CAM}
HAVING COUNT(DISTINCT DATE(d.timestamp)) = 1
ORDER BY camera_name""",
"Cameras whose detections all fall on ONE calendar day (HAVING COUNT(DISTINCT DATE) = 1), with that day; one row per camera"),
E("Which day of the week has the highest average number of detections per day, counting only days that had detections",
f"""WITH per_day AS (
    SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
    FROM detections d
    GROUP BY DATE(d.timestamp))
SELECT TRIM(TO_CHAR(detection_day, 'Day')) AS weekday, COUNT(*) AS active_days,
    SUM(detections) AS detections, ROUND(AVG(detections), 1) AS average_per_active_day
FROM per_day
GROUP BY TRIM(TO_CHAR(detection_day, 'Day')), EXTRACT(DOW FROM detection_day)
ORDER BY average_per_active_day DESC, weekday""",
"Average detections per ACTIVE day for each weekday (a day with no detections does not count), highest first; one row per weekday"),
E("How many detections of identified people happened outside 08:00 to 18:00, and what percentage of all identified detections is that",
f"""SELECT COUNT(*) AS identified_detections,
    COUNT(*) FILTER (WHERE EXTRACT(HOUR FROM d.timestamp) < 8 OR EXTRACT(HOUR FROM d.timestamp) >= 18) AS outside_hours,
    ROUND(100.0 * COUNT(*) FILTER (WHERE EXTRACT(HOUR FROM d.timestamp) < 8 OR EXTRACT(HOUR FROM d.timestamp) >= 18) / COUNT(*), 1) AS outside_hours_percent
FROM faces f
JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}""",
"Identified detections outside 08:00-18:00 (hour < 8 or hour >= 18) and their share of all identified detections; one row"),
E("What was the longest gap in hours between two consecutive detections of PERSON_NAME, and which cameras were those two detections at",
f"""WITH ordered AS (
    SELECT d.timestamp AS seen_at, {CAM} AS camera_name,
        LAG(d.timestamp) OVER (ORDER BY d.timestamp) AS previous_seen_at,
        LAG({CAM}) OVER (ORDER BY d.timestamp) AS previous_camera
    {FDP}
    WHERE {PERSON})
SELECT previous_seen_at, seen_at, previous_camera, camera_name,
    ROUND(EXTRACT(EPOCH FROM (seen_at - previous_seen_at)) / 3600, 2) AS gap_hours
FROM ordered
WHERE previous_seen_at IS NOT NULL
ORDER BY gap_hours DESC
LIMIT 1""",
"Longest gap between one person's consecutive detections: LAG over ALL their detections in time order (never partitioned by detection), with the camera before and after; one row"),
E("Compare the average recognition similarity of PERSON_NAME's faces with OTHER_PERSON's faces",
f"""SELECT f.name, COUNT(*) AS faces,
    ROUND(AVG(f.similarity)::numeric, 3) AS average_similarity,
    ROUND(MIN(f.similarity)::numeric, 3) AS lowest_similarity,
    ROUND(MAX(f.similarity)::numeric, 3) AS highest_similarity
FROM faces f
WHERE {PERSON} OR LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')
GROUP BY f.name
ORDER BY average_similarity DESC""",
"Two people's similarity statistics side by side: one row per person from ONE pass over faces (never a self-join of faces on detection_id)"),
E("How many detections happened in the 24 hours after PERSON_NAME's first detection, and how many of those were at the same camera as that first detection",
f"""WITH first_seen AS (
    SELECT d.timestamp AS first_at, d.pipeline_id AS first_pipeline_id
    FROM faces f
    JOIN detections d ON f.detection_id = d.id
    WHERE {PERSON}
    ORDER BY d.timestamp
    LIMIT 1)
SELECT fs.first_at, (SELECT {CAM} FROM pipelines p WHERE p.pipeline_id = fs.first_pipeline_id) AS first_camera,
    COUNT(*) AS detections_in_24h,
    COUNT(*) FILTER (WHERE d.pipeline_id = fs.first_pipeline_id) AS at_the_same_camera
FROM first_seen fs
JOIN detections d ON d.timestamp > fs.first_at AND d.timestamp <= fs.first_at + INTERVAL '24 hours'
GROUP BY fs.first_at, fs.first_pipeline_id""",
"Window after a person's first detection: the first row carries BOTH its time and its pipeline_id, so the same-camera count compares against a real column; one row"),
]

# ---------------------------------------------------------------------------
CATALOG += [
E("Which pair of cameras is most often visited one after the other by the same identified person, and how many times",
f"""WITH steps AS (
    SELECT f.name, {CAM} AS camera_name,
        LAG({CAM}) OVER (PARTITION BY f.name ORDER BY d.timestamp) AS previous_camera
    {FDP}
    WHERE {IDENT})
SELECT previous_camera, camera_name AS next_camera, COUNT(*) AS transitions
FROM steps
WHERE previous_camera IS NOT NULL AND previous_camera <> camera_name
GROUP BY previous_camera, camera_name
ORDER BY transitions DESC, previous_camera, next_camera""",
"Camera-to-camera transitions across all identified people (LAG per person), most frequent pair first; ties share the top count"),
E("What was the longest streak of consecutive days with at least one detection, and on which dates did it start and end",
f"""WITH active_days AS (SELECT DISTINCT DATE(d.timestamp) AS detection_day FROM detections d),
runs AS (SELECT detection_day, detection_day - (ROW_NUMBER() OVER (ORDER BY detection_day))::int AS run_id FROM active_days)
SELECT MIN(detection_day) AS streak_start, MAX(detection_day) AS streak_end, COUNT(*) AS consecutive_days
FROM runs
GROUP BY run_id
ORDER BY consecutive_days DESC, streak_start
LIMIT 1""",
"Longest run of consecutive active days (gaps-and-islands: day minus its row number is constant within a run)"),
E("Which day had the biggest increase in detections compared with the previous day, and by how many",
f"""WITH per_day AS (SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections FROM detections d GROUP BY 1),
changes AS (SELECT detection_day, detections,
    LAG(detection_day) OVER (ORDER BY detection_day) AS previous_day,
    detections - LAG(detections) OVER (ORDER BY detection_day) AS increase
    FROM per_day)
SELECT detection_day, previous_day, detections, increase
FROM changes
WHERE increase IS NOT NULL
ORDER BY increase DESC
LIMIT 1""",
"The day with the largest rise over the previous active day (LAG over daily counts), one row"),
E("For each identified person, what percentage of their detections happened at the camera they visit most",
f"""WITH per_camera AS (
    SELECT f.name, {CAM} AS camera_name, COUNT(*) AS detections_there
    {FDP}
    WHERE {IDENT}
    GROUP BY f.name, p.pipeline_id, p.location_name),
ranked AS (
    SELECT name, camera_name, detections_there,
        SUM(detections_there) OVER (PARTITION BY name) AS total_detections,
        ROW_NUMBER() OVER (PARTITION BY name ORDER BY detections_there DESC, camera_name) AS rank_for_person
    FROM per_camera)
SELECT name, camera_name AS most_visited_camera, detections_there, total_detections,
    ROUND(100.0 * detections_there / total_detections, 1) AS percent_at_most_visited
FROM ranked
WHERE rank_for_person = 1
ORDER BY name""",
"One row per identified person: their most-visited camera and the share of their detections there (ROW_NUMBER per person)"),
E("Rank the cameras by the average number of faces per detection, counting only cameras with at least 5 detections",
f"""SELECT {CAM} AS camera_name, COUNT(DISTINCT d.id) AS detections, COUNT(f.id) AS faces,
    ROUND(COUNT(f.id)::numeric / COUNT(DISTINCT d.id), 2) AS faces_per_detection
FROM detections d
JOIN pipelines p ON p.pipeline_id = d.pipeline_id
LEFT JOIN faces f ON f.detection_id = d.id
{GROUP_CAM}
HAVING COUNT(DISTINCT d.id) >= 5
ORDER BY faces_per_detection DESC, detections DESC""",
"Faces per detection per camera with a minimum-detections threshold in HAVING (substitute the 5); ties are listed together"),
E("Which identified person was seen at the most different cameras within a single day, and on which day",
f"""SELECT f.name, DATE(d.timestamp) AS detection_day, COUNT(DISTINCT d.pipeline_id) AS cameras_that_day
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY f.name, DATE(d.timestamp)
ORDER BY cameras_that_day DESC, detection_day
LIMIT 1""",
"Person-day with the most distinct cameras (group by person AND day), one row; ties resolve to the earliest day"),
E("Which hours of the day had detections on more than one distinct day",
f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(DISTINCT DATE(d.timestamp)) AS distinct_days, COUNT(*) AS detections
FROM detections d
GROUP BY 1
HAVING COUNT(DISTINCT DATE(d.timestamp)) > 1
ORDER BY hour_of_day""",
"Hours that recur across days: HAVING on a COUNT(DISTINCT day) per hour"),
E("How many detections happened across all cameras in the 60 minutes after PERSON_NAME's very first detection",
f"""WITH anchor AS (
    SELECT MIN(d.timestamp) AS first_seen
    FROM faces f JOIN detections d ON f.detection_id = d.id
    WHERE {PERSON})
SELECT anchor.first_seen, COUNT(d.id) AS detections_in_window, COUNT(DISTINCT d.pipeline_id) AS cameras_involved
FROM anchor
LEFT JOIN detections d ON d.timestamp > anchor.first_seen AND d.timestamp <= anchor.first_seen + INTERVAL '60 minutes'
GROUP BY anchor.first_seen""",
"Detections in a window that starts AT the person's first detection (the window runs from the anchor, not from anchor + 60 minutes); substitute the minutes"),
E("What percentage of PERSON_NAME's detections happened at the camera that has the most detections overall",
f"""WITH busiest AS (
    SELECT d.pipeline_id FROM detections d GROUP BY d.pipeline_id ORDER BY COUNT(*) DESC LIMIT 1)
SELECT (SELECT {CAM} FROM pipelines p WHERE p.pipeline_id = busiest.pipeline_id) AS busiest_camera,
    COUNT(*) AS person_detections,
    COUNT(*) FILTER (WHERE d.pipeline_id = busiest.pipeline_id) AS at_busiest_camera,
    ROUND(100.0 * COUNT(*) FILTER (WHERE d.pipeline_id = busiest.pipeline_id) / COUNT(*), 1) AS percent_at_busiest
FROM faces f
JOIN detections d ON f.detection_id = d.id
CROSS JOIN busiest
WHERE {PERSON}
GROUP BY busiest.pipeline_id""",
"Share of one person's detections at the globally busiest camera: the busiest camera is a one-row CTE cross-joined in, then FILTER"),
E("What is the median number of detections per day, counting only days that had detections",
f"""WITH per_day AS (SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections FROM detections d GROUP BY 1)
SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY detections) AS median_detections_per_day,
    ROUND(AVG(detections), 1) AS mean_detections_per_day, COUNT(*) AS active_days
FROM per_day""",
"Median of the daily counts: PERCENTILE_CONT(0.5) WITHIN GROUP, never MEDIAN() and never with OVER"),
]

# ---------------------------------------------------------------------------
# 2. One person, many questions
# ---------------------------------------------------------------------------
CATALOG += [
E("When was PERSON_NAME first seen and last seen",
f"""SELECT f.name, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
GROUP BY f.name""",
"First and last sighting of one person with their detection count, one row"),
E("Where was PERSON_NAME last seen and when",
f"""SELECT f.name, {CAM} AS camera_name, d.timestamp AS last_seen, f.similarity
{FDP}
WHERE {PERSON}
ORDER BY d.timestamp DESC
LIMIT 1""",
"The most recent detection of one person: camera and time, one row"),
E("Where was PERSON_NAME first seen",
f"""SELECT f.name, {CAM} AS camera_name, d.timestamp AS first_seen
{FDP}
WHERE {PERSON}
ORDER BY d.timestamp ASC
LIMIT 1""",
"The earliest detection of one person: camera and time, one row"),
E("How many times has PERSON_NAME been detected at each camera",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS detections, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen
{FDP}
WHERE {PERSON}
{GROUP_CAM}
ORDER BY detections DESC""",
"One person's detections per camera, busiest camera first"),
E("Which camera has seen PERSON_NAME the most",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS detections
{FDP}
WHERE {PERSON}
{GROUP_CAM}
ORDER BY detections DESC
LIMIT 1""",
"The single camera with the most detections of one person"),
E("How many different cameras have seen PERSON_NAME",
f"""SELECT f.name, COUNT(DISTINCT d.pipeline_id) AS cameras_seen, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
GROUP BY f.name""",
"Distinct camera count for one person, one row"),
E("How many times was PERSON_NAME detected on each day",
f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
GROUP BY 1
ORDER BY detection_day""",
"One person's detections per day across all cameras, in date order"),
E("At what hours of the day is PERSON_NAME usually detected",
f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
GROUP BY 1
ORDER BY detections DESC, hour_of_day""",
"One person's detections per hour of day, most frequent hour first"),
E("On which days of the week is PERSON_NAME seen most",
f"""SELECT TRIM(TO_CHAR(d.timestamp, 'Day')) AS weekday, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
GROUP BY 1, EXTRACT(DOW FROM d.timestamp)
ORDER BY detections DESC""",
"One person's detections per weekday, most frequent first"),
E("Show PERSON_NAME's full detection timeline with camera and time",
f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, f.similarity, f.face_image_path
{FDP}
WHERE {PERSON}
ORDER BY d.timestamp ASC""",
"Every detection of one person in time order: the tracking report"),
E("Show PERSON_NAME's detections between 2026-08-17 and 2026-08-18",
f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, f.similarity
{FDP}
WHERE {PERSON}
  AND d.timestamp >= TIMESTAMP '2026-08-17 00:00:00'
  AND d.timestamp <  TIMESTAMP '2026-08-19 00:00:00'
ORDER BY d.timestamp""",
"One person's detections inside a date range (substitute the two dates; the end bound is the day after the last day)"),
E("What is the average time in minutes between PERSON_NAME's consecutive detections",
f"""WITH ordered AS (
    SELECT d.timestamp AS seen_at, LAG(d.timestamp) OVER (ORDER BY d.timestamp) AS previous_seen_at
    FROM faces f JOIN detections d ON f.detection_id = d.id
    WHERE {PERSON})
SELECT ROUND(AVG(EXTRACT(EPOCH FROM (seen_at - previous_seen_at)) / 60)::numeric, 1) AS avg_gap_minutes,
    COUNT(*) AS gaps
FROM ordered
WHERE previous_seen_at IS NOT NULL""",
"Average gap in minutes between one person's consecutive detections, one row"),
E("What is the shortest time between two detections of PERSON_NAME",
f"""WITH ordered AS (
    SELECT d.timestamp AS seen_at, LAG(d.timestamp) OVER (ORDER BY d.timestamp) AS previous_seen_at
    FROM faces f JOIN detections d ON f.detection_id = d.id
    WHERE {PERSON})
SELECT previous_seen_at, seen_at, ROUND(EXTRACT(EPOCH FROM (seen_at - previous_seen_at)), 1) AS gap_seconds
FROM ordered
WHERE previous_seen_at IS NOT NULL
ORDER BY gap_seconds ASC
LIMIT 1""",
"The shortest gap between one person's consecutive detections, in seconds, with both endpoints"),
E("How long did PERSON_NAME stay at each camera",
f"""SELECT {CAM} AS camera_name, DATE(d.timestamp) AS detection_day,
    MIN(d.timestamp) AS arrived, MAX(d.timestamp) AS left_at,
    ROUND(EXTRACT(EPOCH FROM (MAX(d.timestamp) - MIN(d.timestamp))) / 60, 1) AS minutes_present,
    COUNT(*) AS detections
{FDP}
WHERE {PERSON}
{GROUP_CAM}, DATE(d.timestamp)
ORDER BY detection_day, arrived""",
"Dwell time per camera per day for one person: first to last detection there in minutes"),
E("Which camera did PERSON_NAME visit first each day",
f"""SELECT DISTINCT ON (DATE(d.timestamp)) DATE(d.timestamp) AS detection_day, {CAM} AS first_camera, d.timestamp AS first_seen
{FDP}
WHERE {PERSON}
ORDER BY DATE(d.timestamp), d.timestamp ASC""",
"The first camera of each day for one person (DISTINCT ON the day, earliest time)"),
E("Which camera did PERSON_NAME visit last each day",
f"""SELECT DISTINCT ON (DATE(d.timestamp)) DATE(d.timestamp) AS detection_day, {CAM} AS last_camera, d.timestamp AS last_seen
{FDP}
WHERE {PERSON}
ORDER BY DATE(d.timestamp), d.timestamp DESC""",
"The last camera of each day for one person (DISTINCT ON the day, latest time)"),
E("Show PERSON_NAME's movement between cameras with the time spent between each step",
f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name,
    LAG({CAM}) OVER (ORDER BY d.timestamp) AS previous_camera,
    ROUND(EXTRACT(EPOCH FROM (d.timestamp - LAG(d.timestamp) OVER (ORDER BY d.timestamp))) / 60, 1) AS minutes_since_previous
{FDP}
WHERE {PERSON}
ORDER BY d.timestamp""",
"One person's path: each detection with the previous camera and the minutes since the previous detection"),
E("Has PERSON_NAME ever been seen at CAMERA_NAME",
f"""SELECT COUNT(*) AS detections, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen
{FDP}
WHERE {PERSON} AND {CAMF}""",
"Whether and how often one person was seen at one camera: a zero count means never"),
E("When was PERSON_NAME last seen at CAMERA_NAME",
f"""SELECT d.timestamp AS last_seen, {CAM} AS camera_name, f.similarity
{FDP}
WHERE {PERSON} AND {CAMF}
ORDER BY d.timestamp DESC
LIMIT 1""",
"The most recent sighting of one person at one camera"),
E("On which days was PERSON_NAME seen",
f"""SELECT DISTINCT DATE(d.timestamp) AS detection_day
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
ORDER BY detection_day""",
"The distinct days on which one person was detected"),
E("How many days has it been since PERSON_NAME was last seen",
f"""SELECT f.name, MAX(d.timestamp) AS last_seen,
    ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(d.timestamp))) / 86400, 1) AS days_since_last_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
GROUP BY f.name""",
"Days elapsed since one person's last detection"),
E("What is PERSON_NAME's average, best and worst similarity score",
f"""SELECT f.name, COUNT(*) AS detections, ROUND(AVG(f.similarity)::numeric, 3) AS avg_similarity,
    MAX(f.similarity) AS best_similarity, MIN(f.similarity) AS worst_similarity
FROM faces f
WHERE {PERSON}
GROUP BY f.name""",
"Similarity statistics for one person's matches"),
E("Show PERSON_NAME's detection with the highest similarity",
f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, f.similarity, f.face_image_path
{FDP}
WHERE {PERSON}
ORDER BY f.similarity DESC
LIMIT 1""",
"The single best-matching detection of one person"),
E("Show PERSON_NAME's detections with low similarity below 0.6",
f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, f.similarity
{FDP}
WHERE {PERSON} AND f.similarity < 0.6
ORDER BY f.similarity ASC""",
"Weak matches for one person (substitute the threshold)"),
E("How many times was PERSON_NAME detected per week",
f"""SELECT DATE_TRUNC('week', d.timestamp)::date AS week_starting_monday, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
GROUP BY 1
ORDER BY week_starting_monday""",
"One person's weekly detection counts"),
E("How many times was PERSON_NAME detected per month",
f"""SELECT TO_CHAR(DATE_TRUNC('month', d.timestamp), 'YYYY-MM') AS month, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
GROUP BY 1
ORDER BY month""",
"One person's monthly detection counts"),
E("Was PERSON_NAME seen today, and where",
f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, f.similarity
{FDP}
WHERE {PERSON} AND DATE(d.timestamp) = CURRENT_DATE
ORDER BY d.timestamp""",
"Today's detections of one person; no rows means not seen today"),
E("Which day was PERSON_NAME's busiest day and at which cameras",
f"""WITH per_day AS (
    SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
    FROM faces f JOIN detections d ON f.detection_id = d.id
    WHERE {PERSON}
    GROUP BY 1
    ORDER BY detections DESC
    LIMIT 1)
SELECT per_day.detection_day, per_day.detections AS detections_that_day, {CAM} AS camera_name, COUNT(*) AS detections_at_camera
FROM per_day
JOIN detections d ON DATE(d.timestamp) = per_day.detection_day
JOIN faces f ON f.detection_id = d.id
JOIN pipelines p ON p.pipeline_id = d.pipeline_id
WHERE {PERSON}
GROUP BY per_day.detection_day, per_day.detections, p.pipeline_id, p.location_name
ORDER BY detections_at_camera DESC""",
"One person's busiest day with the per-camera breakdown of that day"),
E("What is the total time span between PERSON_NAME's first and last detection ever",
f"""SELECT f.name, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen,
    ROUND(EXTRACT(EPOCH FROM (MAX(d.timestamp) - MIN(d.timestamp))) / 3600, 1) AS span_hours
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}
GROUP BY f.name""",
"Hours between one person's first and last detection overall"),
E("Show PERSON_NAME's detections at CAMERA_NAME in order",
f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, f.similarity
{FDP}
WHERE {PERSON} AND {CAMF}
ORDER BY d.timestamp""",
"One person's timeline restricted to one camera"),
E("How many detections of PERSON_NAME happened at night between 22:00 and 06:00",
f"""SELECT COUNT(*) AS night_detections,
    COUNT(*) FILTER (WHERE EXTRACT(HOUR FROM d.timestamp) >= 22 OR EXTRACT(HOUR FROM d.timestamp) < 6) AS between_22_and_06
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON}""",
"Night-time detections of one person (hour >= 22 or < 6) against their total"),
]

# ---------------------------------------------------------------------------
# 3. Two people
# ---------------------------------------------------------------------------
CATALOG += [
E("Which cameras have seen PERSON_NAME but never OTHER_PERSON",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS person_name_detections
{FDP}
WHERE {PERSON}
  AND NOT EXISTS (SELECT 1 FROM detections d2 JOIN faces f2 ON f2.detection_id = d2.id
                  WHERE d2.pipeline_id = p.pipeline_id AND LOWER(f2.name) LIKE LOWER('%OTHER_PERSON%'))
{GROUP_CAM}
ORDER BY person_name_detections DESC""",
"Set difference of cameras: saw the first person, never the second; one row per camera"),
E("Who was detected more often, PERSON_NAME or OTHER_PERSON",
f"""SELECT f.name, COUNT(*) AS detections, COUNT(DISTINCT d.pipeline_id) AS cameras_seen,
    MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON} OR LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')
GROUP BY f.name
ORDER BY detections DESC""",
"Side-by-side counts for two people, most detected first"),
E("Were PERSON_NAME and OTHER_PERSON ever detected at the same camera within 10 minutes of each other",
f"""SELECT {CAM} AS camera_name, d1.timestamp AS person_name_at, d2.timestamp AS other_person_at,
    ROUND(ABS(EXTRACT(EPOCH FROM (d1.timestamp - d2.timestamp))) / 60, 1) AS minutes_apart
FROM faces f1 JOIN detections d1 ON f1.detection_id = d1.id
JOIN detections d2 ON d2.pipeline_id = d1.pipeline_id
JOIN faces f2 ON f2.detection_id = d2.id
JOIN pipelines p ON p.pipeline_id = d1.pipeline_id
WHERE LOWER(f1.name) LIKE LOWER('%PERSON_NAME%') AND LOWER(f2.name) LIKE LOWER('%OTHER_PERSON%')
  AND ABS(EXTRACT(EPOCH FROM (d1.timestamp - d2.timestamp))) <= 600
ORDER BY minutes_apart""",
"Co-presence of two named people at one camera inside a window (substitute the minutes as seconds)"),
E("On which days were both PERSON_NAME and OTHER_PERSON seen",
f"""SELECT DATE(d.timestamp) AS detection_day,
    COUNT(*) FILTER (WHERE {PERSON}) AS person_name_detections,
    COUNT(*) FILTER (WHERE LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')) AS other_person_detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON} OR LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')
GROUP BY 1
HAVING COUNT(*) FILTER (WHERE {PERSON}) > 0
   AND COUNT(*) FILTER (WHERE LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')) > 0
ORDER BY detection_day""",
"Days on which two people were both detected, with each one's count"),
E("How much time passed between PERSON_NAME's last detection and OTHER_PERSON's first detection",
f"""SELECT
    (SELECT MAX(d.timestamp) FROM faces f JOIN detections d ON f.detection_id = d.id WHERE {PERSON}) AS person_name_last_seen,
    (SELECT MIN(d.timestamp) FROM faces f JOIN detections d ON f.detection_id = d.id WHERE LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')) AS other_person_first_seen,
    ROUND(EXTRACT(EPOCH FROM (
        (SELECT MIN(d.timestamp) FROM faces f JOIN detections d ON f.detection_id = d.id WHERE LOWER(f.name) LIKE LOWER('%OTHER_PERSON%'))
      - (SELECT MAX(d.timestamp) FROM faces f JOIN detections d ON f.detection_id = d.id WHERE {PERSON}))) / 3600, 1) AS hours_between""",
"Hours from one person's last sighting to another's first (negative means they overlap)"),
E("Compare PERSON_NAME and OTHER_PERSON camera by camera",
f"""SELECT {CAM} AS camera_name,
    COUNT(*) FILTER (WHERE {PERSON}) AS person_name_detections,
    COUNT(*) FILTER (WHERE LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')) AS other_person_detections
{FDP}
WHERE {PERSON} OR LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')
{GROUP_CAM}
ORDER BY camera_name""",
"Per-camera counts for two people side by side"),
E("Who else was detected at the same camera within 10 minutes of any of PERSON_NAME's detections",
f"""SELECT DISTINCT f2.name AS other_person, {CAM} AS camera_name,
    MIN(ROUND(ABS(EXTRACT(EPOCH FROM (d2.timestamp - d1.timestamp))) / 60, 1)) AS closest_minutes_apart
FROM faces f1 JOIN detections d1 ON f1.detection_id = d1.id
JOIN detections d2 ON d2.pipeline_id = d1.pipeline_id AND d2.id <> d1.id
    AND d2.timestamp BETWEEN d1.timestamp - INTERVAL '10 minutes' AND d1.timestamp + INTERVAL '10 minutes'
JOIN faces f2 ON f2.detection_id = d2.id
JOIN pipelines p ON p.pipeline_id = d1.pipeline_id
WHERE LOWER(f1.name) LIKE LOWER('%PERSON_NAME%')
  AND f2.name IS NOT NULL AND f2.name <> '' AND LOWER(f2.name) NOT LIKE 'unknown%' AND LOWER(f2.name) NOT LIKE 'person_%'
  AND LOWER(f2.name) NOT LIKE LOWER('%PERSON_NAME%')
GROUP BY f2.name, p.pipeline_id, p.location_name
ORDER BY closest_minutes_apart""",
"Other IDENTIFIED people at the same camera within N minutes of one person's detections (self-join on pipeline_id); placeholders excluded"),
E("Which people have been seen at the same cameras as PERSON_NAME",
f"""SELECT f2.name AS other_person, COUNT(DISTINCT d2.pipeline_id) AS shared_cameras
FROM faces f2 JOIN detections d2 ON f2.detection_id = d2.id
WHERE d2.pipeline_id IN (SELECT d.pipeline_id FROM faces f JOIN detections d ON f.detection_id = d.id WHERE {PERSON})
  AND f2.name IS NOT NULL AND f2.name <> '' AND LOWER(f2.name) NOT LIKE 'unknown%' AND LOWER(f2.name) NOT LIKE 'person_%'
  AND LOWER(f2.name) NOT LIKE LOWER('%PERSON_NAME%')
GROUP BY f2.name
ORDER BY shared_cameras DESC""",
"Identified people who share at least one camera with the named person, by number of shared cameras"),
]

# ---------------------------------------------------------------------------
# 4. One camera
# ---------------------------------------------------------------------------
CATALOG += [
E("How many detections has CAMERA_NAME recorded in total",
f"""SELECT {CAM} AS camera_name, COUNT(d.id) AS detections, MIN(d.timestamp) AS first_detection, MAX(d.timestamp) AS last_detection
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
WHERE {CAMF}
{GROUP_CAM}""",
"Total detections at one camera counted from detections (LEFT JOIN so a silent camera returns 0)"),
E("Which identified people has CAMERA_NAME seen and how often",
f"""SELECT f.name, COUNT(*) AS detections, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen
{FDP}
WHERE {CAMF} AND {IDENT}
GROUP BY f.name
ORDER BY detections DESC""",
"Identified people at one camera with counts (placeholders excluded)"),
E("How many different identified people has CAMERA_NAME seen",
f"""SELECT {CAM} AS camera_name,
    COUNT(DISTINCT CASE WHEN {IDENT} THEN f.name END) AS identified_people,
    COUNT(*) FILTER (WHERE {UNID}) AS unidentified_faces
{FDP}
WHERE {CAMF}
{GROUP_CAM}""",
"Distinct identified people and unidentified faces at one camera"),
E("What share of the faces at CAMERA_NAME are unidentified",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS faces,
    COUNT(*) FILTER (WHERE {UNID}) AS unidentified_faces,
    ROUND(100.0 * COUNT(*) FILTER (WHERE {UNID}) / COUNT(*), 1) AS unidentified_percent
{FDP}
WHERE {CAMF}
{GROUP_CAM}""",
"Unidentified percentage at one camera"),
E("Show CAMERA_NAME's detections per day",
f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
{DP}
WHERE {CAMF}
GROUP BY 1
ORDER BY detection_day""",
"Daily detection counts at one camera"),
E("What is the busiest hour at CAMERA_NAME",
f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
{DP}
WHERE {CAMF}
GROUP BY 1
ORDER BY detections DESC
LIMIT 1""",
"The hour of day with the most detections at one camera"),
E("Show the hourly detection profile of CAMERA_NAME",
f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
{DP}
WHERE {CAMF}
GROUP BY 1
ORDER BY hour_of_day""",
"Detections per hour of day at one camera, in hour order"),
E("When did CAMERA_NAME last record a detection and how long ago",
f"""SELECT {CAM} AS camera_name, MAX(d.timestamp) AS last_detection,
    ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(d.timestamp))) / 3600, 1) AS hours_since_last_detection
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
WHERE {CAMF}
{GROUP_CAM}""",
"Idle time of one camera since its last detection (NULL last_detection means never)"),
E("What is the average processing time at CAMERA_NAME",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS detections,
    ROUND(AVG(d.processing_time_ms)::numeric, 1) AS avg_processing_ms,
    MAX(d.processing_time_ms) AS max_processing_ms
{DP}
WHERE {CAMF}
{GROUP_CAM}""",
"Processing-time statistics for one camera's detections"),
E("How many faces per detection does CAMERA_NAME average",
f"""SELECT {CAM} AS camera_name, COUNT(DISTINCT d.id) AS detections, COUNT(f.id) AS faces,
    ROUND(COUNT(f.id)::numeric / NULLIF(COUNT(DISTINCT d.id), 0), 2) AS faces_per_detection
FROM detections d
JOIN pipelines p ON p.pipeline_id = d.pipeline_id
LEFT JOIN faces f ON f.detection_id = d.id
WHERE {CAMF}
{GROUP_CAM}""",
"Faces per detection at one camera"),
E("Who was the last person identified at CAMERA_NAME",
f"""SELECT f.name, d.timestamp AS seen_at, f.similarity
{FDP}
WHERE {CAMF} AND {IDENT}
ORDER BY d.timestamp DESC
LIMIT 1""",
"The most recent IDENTIFIED face at one camera"),
E("Is CAMERA_NAME active and when was it created",
f"""SELECT {CAM} AS camera_name, p.pipeline_id, p.is_active, p.created_at, p.updated_at, p.timezone
FROM pipelines p
WHERE {CAMF}""",
"Status and creation date of one camera (is_active is 1 or 0)"),
E("Which people were seen at CAMERA_NAME more than 3 times",
f"""SELECT f.name, COUNT(*) AS detections
{FDP}
WHERE {CAMF} AND {IDENT}
GROUP BY f.name
HAVING COUNT(*) > 3
ORDER BY detections DESC""",
"Frequent identified visitors of one camera (substitute the threshold)"),
E("Show the latest 20 detections at CAMERA_NAME with the names recognised",
f"""SELECT d.timestamp AS seen_at, d.id AS detection_id, COALESCE(f.name, 'Unknown') AS name, f.similarity
FROM detections d
JOIN pipelines p ON p.pipeline_id = d.pipeline_id
LEFT JOIN faces f ON f.detection_id = d.id
WHERE {CAMF}
ORDER BY d.timestamp DESC
LIMIT 20""",
"Recent activity at one camera with recognised names (LEFT JOIN keeps face-less detections)"),
E("What was CAMERA_NAME's busiest day",
f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
{DP}
WHERE {CAMF}
GROUP BY 1
ORDER BY detections DESC
LIMIT 1""",
"The day with the most detections at one camera"),
E("Compare CAMERA_NAME's detections today with yesterday",
f"""SELECT {CAM} AS camera_name,
    COUNT(*) FILTER (WHERE DATE(d.timestamp) = CURRENT_DATE) AS today,
    COUNT(*) FILTER (WHERE DATE(d.timestamp) = CURRENT_DATE - 1) AS yesterday
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
WHERE {CAMF}
{GROUP_CAM}""",
"Today versus yesterday at one camera, one row"),
]

# ---------------------------------------------------------------------------
# 5. All cameras
# ---------------------------------------------------------------------------
CATALOG += [
E("Rank all cameras by number of detections",
f"""SELECT {CAM} AS camera_name, COUNT(d.id) AS detections
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
{GROUP_CAM}
ORDER BY detections DESC""",
"Every camera with its real detection count, busiest first (silent cameras show 0)"),
E("Which camera has the fewest detections",
f"""SELECT {CAM} AS camera_name, COUNT(d.id) AS detections
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
{GROUP_CAM}
ORDER BY detections ASC, camera_name
LIMIT 5""",
"Least active cameras including those with zero detections"),
E("Which cameras have never recorded a detection",
f"""SELECT {CAM} AS camera_name, p.is_active, p.created_at
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
{GROUP_CAM}, p.is_active, p.created_at
HAVING COUNT(d.id) = 0
ORDER BY camera_name""",
"Cameras with no detections at all: LEFT JOIN + HAVING COUNT = 0"),
E("Which cameras have not recorded any detection in the last 7 days",
f"""SELECT {CAM} AS camera_name, MAX(d.timestamp) AS last_detection
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
{GROUP_CAM}
HAVING MAX(d.timestamp) IS NULL OR MAX(d.timestamp) < NOW() - INTERVAL '7 days'
ORDER BY last_detection DESC NULLS LAST""",
"Cameras silent for a period (substitute the days): a camera that never detected anything is included"),
E("How many cameras are active and how many are inactive",
f"""SELECT COUNT(*) FILTER (WHERE p.is_active = 1) AS active_cameras,
    COUNT(*) FILTER (WHERE p.is_active = 0 OR p.is_active IS NULL) AS inactive_cameras,
    COUNT(*) AS total_cameras
FROM pipelines p""",
"Active versus inactive camera counts"),
E("Which active cameras have recorded nothing in the last 24 hours",
f"""SELECT {CAM} AS camera_name, MAX(d.timestamp) AS last_detection
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
WHERE p.is_active = 1
{GROUP_CAM}
HAVING MAX(d.timestamp) IS NULL OR MAX(d.timestamp) < NOW() - INTERVAL '24 hours'
ORDER BY last_detection DESC NULLS LAST""",
"Active cameras that appear dead: no detection in the window"),
E("Show each camera's last detection time",
f"""SELECT {CAM} AS camera_name, MAX(d.timestamp) AS last_detection, COUNT(d.id) AS detections
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
{GROUP_CAM}
ORDER BY last_detection DESC NULLS LAST""",
"Most recent detection per camera, newest first"),
E("Which cameras have seen more than one identified person",
f"""SELECT {CAM} AS camera_name, COUNT(DISTINCT f.name) AS identified_people
{FDP}
WHERE {IDENT}
{GROUP_CAM}
HAVING COUNT(DISTINCT f.name) > 1
ORDER BY identified_people DESC""",
"Cameras with at least two distinct identified people"),
E("Which camera has seen the most different identified people",
f"""SELECT {CAM} AS camera_name, COUNT(DISTINCT f.name) AS identified_people
{FDP}
WHERE {IDENT}
{GROUP_CAM}
ORDER BY identified_people DESC, camera_name
LIMIT 1""",
"The camera with the most distinct identified people"),
E("Which cameras have identified nobody",
f"""SELECT {CAM} AS camera_name, COUNT(d.id) AS detections
FROM pipelines p
LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id
LEFT JOIN faces f ON f.detection_id = d.id AND {IDENT}
{GROUP_CAM}
HAVING COUNT(f.id) = 0
ORDER BY detections DESC""",
"Cameras with detections but no identified person (the identified filter sits in the LEFT JOIN condition)"),
E("Show the top 3 cameras and their share of all detections",
f"""WITH per_camera AS (
    SELECT {CAM} AS camera_name, COUNT(*) AS detections {DP} {GROUP_CAM})
SELECT camera_name, detections,
    ROUND(100.0 * detections / (SELECT SUM(detections) FROM per_camera), 1) AS percent_of_all
FROM per_camera
ORDER BY detections DESC
LIMIT 3""",
"Top cameras with each one's percentage of all detections"),
E("What percentage of all detections does each camera account for",
f"""WITH per_camera AS (
    SELECT {CAM} AS camera_name, COUNT(*) AS detections {DP} {GROUP_CAM})
SELECT camera_name, detections,
    ROUND(100.0 * detections / (SELECT SUM(detections) FROM per_camera), 1) AS percent_of_all
FROM per_camera
ORDER BY detections DESC""",
"Every camera's share of all detections"),
E("Which camera has the highest average similarity for recognized faces",
f"""SELECT {CAM} AS camera_name, ROUND(AVG(f.similarity)::numeric, 3) AS avg_similarity, COUNT(*) AS recognized_faces
{FDP}
WHERE {IDENT}
{GROUP_CAM}
ORDER BY avg_similarity DESC""",
"Average match quality per camera over identified faces, best first"),
E("Which cameras were created this week",
f"""SELECT {CAM} AS camera_name, p.pipeline_id, p.created_at, p.is_active
FROM pipelines p
WHERE p.created_at >= DATE_TRUNC('week', NOW())
ORDER BY p.created_at DESC""",
"Cameras created since Monday"),
E("Which cameras were created in the last 30 days",
f"""SELECT {CAM} AS camera_name, p.pipeline_id, p.created_at, p.is_active
FROM pipelines p
WHERE p.created_at > NOW() - INTERVAL '30 days'
ORDER BY p.created_at DESC""",
"Recently created cameras (substitute the days)"),
E("Which camera has the slowest average processing time",
f"""SELECT {CAM} AS camera_name, ROUND(AVG(d.processing_time_ms)::numeric, 1) AS avg_processing_ms, COUNT(*) AS detections
{DP}
WHERE d.processing_time_ms IS NOT NULL
{GROUP_CAM}
ORDER BY avg_processing_ms DESC
LIMIT 5""",
"Cameras ranked by average processing time, slowest first"),
E("Which cameras have a location set and which do not",
f"""SELECT p.pipeline_id, p.location_name, p.latitude, p.longitude,
    CASE WHEN p.location_name IS NULL OR p.location_name = '' THEN 'no location name' ELSE 'named' END AS location_status
FROM pipelines p
ORDER BY location_status, p.pipeline_id""",
"Cameras with and without a location name and coordinates"),
E("What is each camera's busiest day",
f"""SELECT DISTINCT ON (p.pipeline_id) {CAM} AS camera_name, DATE(d.timestamp) AS busiest_day, COUNT(*) AS detections
{DP}
{GROUP_CAM}, DATE(d.timestamp)
ORDER BY p.pipeline_id, detections DESC""",
"For every camera, the single day with the most detections (DISTINCT ON per camera)"),
E("How many detections per camera per day",
f"""SELECT DATE(d.timestamp) AS detection_day, {CAM} AS camera_name, COUNT(*) AS detections
{DP}
GROUP BY 1, p.pipeline_id, p.location_name
ORDER BY detection_day, detections DESC""",
"Day-by-camera matrix of detection counts"),
E("Which cameras saw someone in the last 15 minutes",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS detections, MAX(d.timestamp) AS latest
{DP}
WHERE d.timestamp > NOW() - INTERVAL '15 minutes'
{GROUP_CAM}
ORDER BY latest DESC""",
"Cameras with very recent activity"),
E("Which cameras had detections every day this week",
f"""SELECT {CAM} AS camera_name, COUNT(DISTINCT DATE(d.timestamp)) AS active_days
{DP}
WHERE d.timestamp >= DATE_TRUNC('week', NOW())
{GROUP_CAM}
HAVING COUNT(DISTINCT DATE(d.timestamp)) = (CURRENT_DATE - DATE_TRUNC('week', NOW())::date + 1)
ORDER BY camera_name""",
"Cameras active on every day of the current week so far"),
E("How many active days does each camera have",
f"""SELECT {CAM} AS camera_name, COUNT(DISTINCT DATE(d.timestamp)) AS active_days,
    MIN(DATE(d.timestamp)) AS first_day, MAX(DATE(d.timestamp)) AS last_day
{DP}
{GROUP_CAM}
ORDER BY active_days DESC""",
"Number of distinct days with at least one detection per camera"),
E("Which cameras are within 1 km of CAMERA_NAME",
f"""SELECT other.pipeline_id, COALESCE(other.location_name, other.pipeline_id) AS camera_name,
    ROUND((6371 * ACOS(LEAST(1.0, COS(RADIANS(base.latitude)) * COS(RADIANS(other.latitude))
        * COS(RADIANS(other.longitude) - RADIANS(base.longitude))
        + SIN(RADIANS(base.latitude)) * SIN(RADIANS(other.latitude)))))::numeric, 2) AS distance_km
FROM pipelines base
JOIN pipelines other ON other.pipeline_id <> base.pipeline_id
WHERE LOWER(COALESCE(base.location_name, base.pipeline_id)) LIKE LOWER('%CAMERA_NAME%')
  AND base.latitude IS NOT NULL AND other.latitude IS NOT NULL
  AND 6371 * ACOS(LEAST(1.0, COS(RADIANS(base.latitude)) * COS(RADIANS(other.latitude))
        * COS(RADIANS(other.longitude) - RADIANS(base.longitude))
        + SIN(RADIANS(base.latitude)) * SIN(RADIANS(other.latitude)))) <= 1
ORDER BY distance_km""",
"Cameras within a radius of a named camera by great-circle distance (substitute the km)"),
]

# ---------------------------------------------------------------------------
# 6. Time analytics over all detections
# ---------------------------------------------------------------------------
CATALOG += [
E("How many detections per day over the last 30 days",
f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
FROM detections d
WHERE d.timestamp > NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY detection_day""",
"Daily trend for a window (substitute the days)"),
E("Which day had the most detections overall",
f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
FROM detections d
GROUP BY 1
ORDER BY detections DESC
LIMIT 1""",
"The busiest single day"),
E("Which day had the fewest detections among days with activity",
f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
FROM detections d
GROUP BY 1
ORDER BY detections ASC, detection_day
LIMIT 1""",
"The quietest active day"),
E("At what hour of the day do most detections happen",
f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
FROM detections d
GROUP BY 1
ORDER BY detections DESC
LIMIT 1""",
"The busiest hour of day with its count"),
E("Show the number of detections for each hour of the day",
f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
FROM detections d
GROUP BY 1
ORDER BY hour_of_day""",
"Hourly profile over all detections"),
E("How many detections per week",
f"""SELECT DATE_TRUNC('week', d.timestamp)::date AS week_starting_monday, COUNT(*) AS detections
FROM detections d
GROUP BY 1
ORDER BY week_starting_monday""",
"Weekly detection counts in order"),
E("How many detections per month",
f"""SELECT TO_CHAR(DATE_TRUNC('month', d.timestamp), 'YYYY-MM') AS month, COUNT(*) AS detections
FROM detections d
GROUP BY 1
ORDER BY month""",
"Monthly detection counts"),
E("How many detections were there this week compared with last week",
f"""SELECT
    COUNT(*) FILTER (WHERE d.timestamp >= DATE_TRUNC('week', NOW())) AS this_week,
    COUNT(*) FILTER (WHERE d.timestamp >= DATE_TRUNC('week', NOW()) - INTERVAL '7 days'
                       AND d.timestamp <  DATE_TRUNC('week', NOW())) AS last_week
FROM detections d""",
"This calendar week versus the previous one, one row"),
E("Compare the last 7 days with the 7 days before that",
f"""SELECT
    COUNT(*) FILTER (WHERE d.timestamp > NOW() - INTERVAL '7 days') AS last_7_days,
    COUNT(*) FILTER (WHERE d.timestamp > NOW() - INTERVAL '14 days' AND d.timestamp <= NOW() - INTERVAL '7 days') AS previous_7_days,
    COUNT(*) FILTER (WHERE d.timestamp > NOW() - INTERVAL '7 days')
      - COUNT(*) FILTER (WHERE d.timestamp > NOW() - INTERVAL '14 days' AND d.timestamp <= NOW() - INTERVAL '7 days') AS change
FROM detections d""",
"Rolling 7-day comparison with the difference"),
E("How many detections happened today versus yesterday",
f"""SELECT
    COUNT(*) FILTER (WHERE DATE(d.timestamp) = CURRENT_DATE) AS today,
    COUNT(*) FILTER (WHERE DATE(d.timestamp) = CURRENT_DATE - 1) AS yesterday
FROM detections d""",
"Today versus yesterday, one row"),
E("How many detections happened at night versus during the day",
f"""SELECT
    COUNT(*) FILTER (WHERE EXTRACT(HOUR FROM d.timestamp) >= 22 OR EXTRACT(HOUR FROM d.timestamp) < 6) AS night_22_to_06,
    COUNT(*) FILTER (WHERE EXTRACT(HOUR FROM d.timestamp) >= 6 AND EXTRACT(HOUR FROM d.timestamp) < 22) AS day_06_to_22
FROM detections d""",
"Night (22:00-06:00) versus day split of all detections"),
E("How many detections happened on weekends versus weekdays",
f"""SELECT
    COUNT(*) FILTER (WHERE EXTRACT(DOW FROM d.timestamp) IN (0, 6)) AS weekend,
    COUNT(*) FILTER (WHERE EXTRACT(DOW FROM d.timestamp) BETWEEN 1 AND 5) AS weekdays
FROM detections d""",
"Weekend (Saturday, Sunday) versus weekday split"),
E("How many detections happened between 2026-08-17 08:00 and 2026-08-17 17:00",
f"""SELECT COUNT(*) AS detections, COUNT(DISTINCT d.pipeline_id) AS cameras_involved
FROM detections d
WHERE d.timestamp >= TIMESTAMP '2026-08-17 08:00:00' AND d.timestamp < TIMESTAMP '2026-08-17 17:00:00'""",
"Detections inside an explicit time range (substitute both timestamps)"),
E("When was the very first and the very last detection in the system",
f"""SELECT MIN(d.timestamp) AS first_detection, MAX(d.timestamp) AS last_detection, COUNT(*) AS total_detections
FROM detections d""",
"The overall time span of the data"),
E("Which days in the last 14 days had no detections at all",
f"""SELECT calendar_day::date AS detection_day
FROM generate_series(CURRENT_DATE - 13, CURRENT_DATE, INTERVAL '1 day') AS calendar_day
WHERE NOT EXISTS (SELECT 1 FROM detections d WHERE DATE(d.timestamp) = calendar_day::date)
ORDER BY detection_day""",
"Calendar days with zero detections: a generated day series minus the days that have rows"),
E("Show the running total of detections by day",
f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections,
    SUM(COUNT(*)) OVER (ORDER BY DATE(d.timestamp)) AS running_total
FROM detections d
GROUP BY 1
ORDER BY detection_day""",
"Cumulative detections day by day (window SUM over the daily counts)"),
E("What is the average number of detections per day",
f"""WITH per_day AS (SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections FROM detections d GROUP BY 1)
SELECT ROUND(AVG(detections), 1) AS avg_per_active_day, MAX(detections) AS busiest_day_count, MIN(detections) AS quietest_day_count,
    COUNT(*) AS active_days
FROM per_day""",
"Average, max and min detections per active day"),
E("Show the hourly detection counts for 2026-08-17",
f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
FROM detections d
WHERE DATE(d.timestamp) = DATE '2026-08-17'
GROUP BY 1
ORDER BY hour_of_day""",
"Hour-by-hour profile of one day (substitute the date)"),
E("Which hour of which day was the busiest ever",
f"""SELECT DATE_TRUNC('hour', d.timestamp) AS hour_start, COUNT(*) AS detections
FROM detections d
GROUP BY 1
ORDER BY detections DESC
LIMIT 1""",
"The single busiest clock hour in the whole data"),
E("How did detections change day over day",
f"""WITH per_day AS (SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections FROM detections d GROUP BY 1)
SELECT detection_day, detections,
    detections - LAG(detections) OVER (ORDER BY detection_day) AS change_from_previous_day
FROM per_day
ORDER BY detection_day""",
"Daily counts with the difference from the previous active day"),
E("How many detections in the last hour",
f"""SELECT COUNT(*) AS detections, COUNT(DISTINCT d.pipeline_id) AS cameras_involved
FROM detections d
WHERE d.timestamp > NOW() - INTERVAL '1 hour'""",
"Detections in the last hour"),
E("Which weekday and hour combination is the busiest",
f"""SELECT TRIM(TO_CHAR(d.timestamp, 'Day')) AS weekday, EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
FROM detections d
GROUP BY 1, EXTRACT(DOW FROM d.timestamp), 2
ORDER BY detections DESC
LIMIT 5""",
"Top weekday-hour slots"),
E("How many detections per day of the month",
f"""SELECT EXTRACT(DAY FROM d.timestamp)::int AS day_of_month, COUNT(*) AS detections
FROM detections d
GROUP BY 1
ORDER BY day_of_month""",
"Detections by calendar day number"),
]

# ---------------------------------------------------------------------------
# 7. People overall
# ---------------------------------------------------------------------------
CATALOG += [
E("List all identified people with how many times each was detected",
f"""SELECT f.name, COUNT(*) AS detections, COUNT(DISTINCT d.pipeline_id) AS cameras_seen,
    MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY f.name
ORDER BY detections DESC""",
"Every identified person with counts and first/last sighting (placeholders excluded)"),
E("Who has been detected the most",
f"""SELECT f.name, COUNT(*) AS detections
FROM faces f
WHERE {IDENT}
GROUP BY f.name
ORDER BY detections DESC
LIMIT 1""",
"The most frequently detected identified person"),
E("Who has been detected the least",
f"""SELECT f.name, COUNT(*) AS detections
FROM faces f
WHERE {IDENT}
GROUP BY f.name
ORDER BY detections ASC, f.name
LIMIT 1""",
"The least frequently detected identified person"),
E("How many different identified people are there",
f"""SELECT COUNT(DISTINCT f.name) AS identified_people,
    COUNT(*) FILTER (WHERE {IDENT}) AS identified_faces,
    COUNT(*) FILTER (WHERE {UNID}) AS unidentified_faces
FROM faces f
WHERE {IDENT} OR {UNID}""",
"Distinct identified people plus identified and unidentified face counts"),
E("Which identified people were seen at more than one camera",
f"""SELECT f.name, COUNT(DISTINCT d.pipeline_id) AS cameras_seen, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY f.name
HAVING COUNT(DISTINCT d.pipeline_id) > 1
ORDER BY cameras_seen DESC""",
"People who moved between cameras"),
E("Which identified people were seen at exactly one camera",
f"""SELECT f.name, MIN({CAM}) AS only_camera, COUNT(*) AS detections
{FDP}
WHERE {IDENT}
GROUP BY f.name
HAVING COUNT(DISTINCT d.pipeline_id) = 1
ORDER BY detections DESC""",
"People confined to a single camera, with that camera"),
E("Which identified people have not been seen in the last 7 days",
f"""SELECT f.name, MAX(d.timestamp) AS last_seen,
    ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(d.timestamp))) / 86400, 1) AS days_since_last_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY f.name
HAVING MAX(d.timestamp) < NOW() - INTERVAL '7 days'
ORDER BY last_seen""",
"Identified people absent for a period (substitute the days)"),
E("Which identified people were seen for the first time this week",
f"""SELECT f.name, MIN(d.timestamp) AS first_ever_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY f.name
HAVING MIN(d.timestamp) >= DATE_TRUNC('week', NOW())
ORDER BY first_ever_seen""",
"New faces: identified people whose earliest detection falls in the current week"),
E("Which identified people were seen on every day of the last 7 days",
f"""SELECT f.name, COUNT(DISTINCT DATE(d.timestamp)) AS days_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT} AND d.timestamp > NOW() - INTERVAL '7 days'
GROUP BY f.name
HAVING COUNT(DISTINCT DATE(d.timestamp)) = 7
ORDER BY f.name""",
"People present on all 7 of the last 7 days"),
E("What is the average similarity score per identified person",
f"""SELECT f.name, COUNT(*) AS detections, ROUND(AVG(f.similarity)::numeric, 3) AS avg_similarity,
    MIN(f.similarity) AS min_similarity, MAX(f.similarity) AS max_similarity
FROM faces f
WHERE {IDENT}
GROUP BY f.name
ORDER BY avg_similarity DESC""",
"Match quality per identified person"),
E("Which identified people have an average similarity below 0.7",
f"""SELECT f.name, COUNT(*) AS detections, ROUND(AVG(f.similarity)::numeric, 3) AS avg_similarity
FROM faces f
WHERE {IDENT}
GROUP BY f.name
HAVING AVG(f.similarity) < 0.7
ORDER BY avg_similarity""",
"Weakly matched people (substitute the threshold)"),
E("How many identified people were seen per day",
f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(DISTINCT f.name) AS identified_people, COUNT(*) AS identified_faces
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY 1
ORDER BY detection_day""",
"Distinct identified people per day"),
E("How many unidentified faces were there per day",
f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS faces,
    COUNT(*) FILTER (WHERE {UNID}) AS unidentified_faces,
    ROUND(100.0 * COUNT(*) FILTER (WHERE {UNID}) / COUNT(*), 1) AS unidentified_percent
FROM faces f JOIN detections d ON f.detection_id = d.id
GROUP BY 1
ORDER BY detection_day""",
"Unidentified faces per day with the daily percentage"),
E("Which identified person was seen at the most cameras",
f"""SELECT f.name, COUNT(DISTINCT d.pipeline_id) AS cameras_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY f.name
ORDER BY cameras_seen DESC, f.name
LIMIT 1""",
"The most widely travelled identified person"),
E("Who was the last identified person detected anywhere",
f"""SELECT f.name, {CAM} AS camera_name, d.timestamp AS seen_at, f.similarity
{FDP}
WHERE {IDENT}
ORDER BY d.timestamp DESC
LIMIT 1""",
"The most recent identified detection system-wide"),
E("Show the latest detection of every identified person",
f"""SELECT DISTINCT ON (f.name) f.name, {CAM} AS last_camera, d.timestamp AS last_seen, f.similarity
{FDP}
WHERE {IDENT}
ORDER BY f.name, d.timestamp DESC""",
"Last sighting per identified person (DISTINCT ON the name)"),
E("Which people were seen at CAMERA_NAME and also at another camera on the same day",
f"""SELECT f.name, DATE(d.timestamp) AS detection_day, COUNT(DISTINCT d.pipeline_id) AS cameras_that_day
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
  AND f.name IN (SELECT f2.name FROM faces f2 JOIN detections d2 ON f2.detection_id = d2.id
                 JOIN pipelines p ON p.pipeline_id = d2.pipeline_id WHERE {CAMF})
GROUP BY f.name, DATE(d.timestamp)
HAVING COUNT(DISTINCT d.pipeline_id) > 1
ORDER BY detection_day, f.name""",
"People seen at the named camera who were also seen elsewhere the same day"),
E("How many identified people were seen at each camera in the last 7 days",
f"""SELECT {CAM} AS camera_name, COUNT(DISTINCT CASE WHEN {IDENT} THEN f.name END) AS identified_people, COUNT(*) AS faces
{FDP}
WHERE d.timestamp > NOW() - INTERVAL '7 days'
{GROUP_CAM}
ORDER BY identified_people DESC""",
"Distinct identified people per camera in a window"),
E("Which identified people were detected more than 5 times",
f"""SELECT f.name, COUNT(*) AS detections
FROM faces f
WHERE {IDENT}
GROUP BY f.name
HAVING COUNT(*) > 5
ORDER BY detections DESC""",
"Frequently seen people (substitute the threshold)"),
E("Rank identified people by how many days they were seen",
f"""SELECT f.name, COUNT(DISTINCT DATE(d.timestamp)) AS days_seen, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY f.name
ORDER BY days_seen DESC, detections DESC""",
"People by number of distinct days present"),
]

# ---------------------------------------------------------------------------
# 8. Faces, similarity, unidentified
# ---------------------------------------------------------------------------
CATALOG += [
E("How many faces have been detected in total and how many were identified",
f"""SELECT COUNT(*) AS total_faces,
    COUNT(*) FILTER (WHERE {IDENT}) AS identified_faces,
    COUNT(*) FILTER (WHERE {UNID}) AS unidentified_faces,
    ROUND(100.0 * COUNT(*) FILTER (WHERE {IDENT}) / NULLIF(COUNT(*), 0), 1) AS identified_percent
FROM faces f""",
"Identified versus unidentified totals with the identified percentage"),
E("What is the overall average similarity score",
f"""SELECT ROUND(AVG(f.similarity)::numeric, 3) AS avg_similarity, MIN(f.similarity) AS min_similarity, MAX(f.similarity) AS max_similarity,
    COUNT(*) AS recognized_faces
FROM faces f
WHERE {IDENT}""",
"Similarity statistics over identified faces only"),
E("How are similarity scores distributed",
f"""SELECT CASE
        WHEN f.similarity >= 0.9 THEN '0.9 - 1.0'
        WHEN f.similarity >= 0.8 THEN '0.8 - 0.9'
        WHEN f.similarity >= 0.7 THEN '0.7 - 0.8'
        WHEN f.similarity >= 0.6 THEN '0.6 - 0.7'
        ELSE 'below 0.6' END AS similarity_band,
    COUNT(*) AS faces
FROM faces f
WHERE {IDENT} AND f.similarity IS NOT NULL
GROUP BY 1
ORDER BY similarity_band DESC""",
"Histogram of similarity for identified faces in 0.1 bands"),
E("Show the 10 lowest-confidence recognitions",
f"""SELECT f.name, f.similarity, {CAM} AS camera_name, d.timestamp AS seen_at
{FDP}
WHERE {IDENT}
ORDER BY f.similarity ASC
LIMIT 10""",
"Weakest identified matches"),
E("Show the 10 highest-confidence recognitions",
f"""SELECT f.name, f.similarity, {CAM} AS camera_name, d.timestamp AS seen_at
{FDP}
WHERE {IDENT}
ORDER BY f.similarity DESC
LIMIT 10""",
"Strongest identified matches"),
E("Show unidentified faces from the last 24 hours",
f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, f.id AS face_id, f.face_image_path
{FDP}
WHERE {UNID} AND d.timestamp > NOW() - INTERVAL '24 hours'
ORDER BY d.timestamp DESC""",
"Recent unidentified faces with their image path"),
E("Which camera produced the most unidentified faces",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS unidentified_faces
{FDP}
WHERE {UNID}
{GROUP_CAM}
ORDER BY unidentified_faces DESC
LIMIT 1""",
"The camera with the most unidentified faces by count"),
E("How many detections had more than one face",
f"""SELECT COUNT(*) AS detections_with_multiple_faces
FROM (SELECT f.detection_id FROM faces f GROUP BY f.detection_id HAVING COUNT(*) > 1) AS multi""",
"Detections where several faces were found"),
E("Which detections contained the most faces",
f"""SELECT d.id AS detection_id, d.timestamp AS seen_at, {CAM} AS camera_name, COUNT(f.id) AS faces
FROM detections d
JOIN pipelines p ON p.pipeline_id = d.pipeline_id
JOIN faces f ON f.detection_id = d.id
GROUP BY d.id, d.timestamp, p.pipeline_id, p.location_name
ORDER BY faces DESC
LIMIT 5""",
"Detections ranked by face count"),
E("How many detections have no faces at all",
f"""SELECT COUNT(*) AS detections_without_faces
FROM detections d
WHERE NOT EXISTS (SELECT 1 FROM faces f WHERE f.detection_id = d.id)""",
"Face-less detections via NOT EXISTS"),
E("What is the average face size in pixels per camera",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS faces,
    ROUND(AVG((f.bbox_x2 - f.bbox_x1) * (f.bbox_y2 - f.bbox_y1))::numeric, 0) AS avg_face_area_px
{FDP}
WHERE f.bbox_x1 IS NOT NULL
{GROUP_CAM}
ORDER BY avg_face_area_px DESC""",
"Average bounding-box area per camera"),
E("Does face size affect recognition? Compare average similarity for large and small faces",
f"""SELECT CASE WHEN (f.bbox_x2 - f.bbox_x1) * (f.bbox_y2 - f.bbox_y1) >= 10000 THEN 'large (>= 100x100)' ELSE 'small' END AS face_size,
    COUNT(*) AS faces, ROUND(AVG(f.similarity)::numeric, 3) AS avg_similarity,
    ROUND(100.0 * COUNT(*) FILTER (WHERE {IDENT}) / COUNT(*), 1) AS identified_percent
FROM faces f
WHERE f.bbox_x1 IS NOT NULL
GROUP BY 1
ORDER BY face_size""",
"Recognition quality by face size band"),
E("Show the images of PERSON_NAME's last 5 detections",
f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, f.similarity, f.face_image_path
{FDP}
WHERE {PERSON} AND f.face_image_path IS NOT NULL
ORDER BY d.timestamp DESC
LIMIT 5""",
"Image paths for a person's most recent detections"),
]

# ---------------------------------------------------------------------------
# 9. Processing and system metrics
# ---------------------------------------------------------------------------
CATALOG += [
E("What is the current CPU and memory usage",
"""SELECT timestamp, cpu_percent, memory_percent, disk_usage_gb, queue_size, active_pipelines
FROM system_metrics
ORDER BY timestamp DESC
LIMIT 1""",
"The latest system metrics row"),
E("What was the peak CPU usage in the last 24 hours and when",
"""SELECT timestamp, cpu_percent, memory_percent
FROM system_metrics
WHERE timestamp > NOW() - INTERVAL '24 hours'
ORDER BY cpu_percent DESC
LIMIT 1""",
"Peak CPU sample in a window"),
E("Show average CPU and memory per hour for the last 24 hours",
"""SELECT DATE_TRUNC('hour', timestamp) AS hour_start,
    ROUND(AVG(cpu_percent)::numeric, 1) AS avg_cpu, ROUND(AVG(memory_percent)::numeric, 1) AS avg_memory,
    MAX(queue_size) AS max_queue
FROM system_metrics
WHERE timestamp > NOW() - INTERVAL '24 hours'
GROUP BY 1
ORDER BY hour_start""",
"Hourly resource averages"),
E("Show average CPU and memory per day for the last 7 days",
"""SELECT DATE(timestamp) AS metric_day,
    ROUND(AVG(cpu_percent)::numeric, 1) AS avg_cpu, ROUND(MAX(cpu_percent)::numeric, 1) AS max_cpu,
    ROUND(AVG(memory_percent)::numeric, 1) AS avg_memory
FROM system_metrics
WHERE timestamp > NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY metric_day""",
"Daily resource averages and peaks"),
E("When was the processing queue largest",
"""SELECT timestamp, queue_size, processing_count, cpu_percent
FROM system_metrics
ORDER BY queue_size DESC
LIMIT 1""",
"The sample with the largest queue"),
E("How often was CPU above 80 percent in the last 7 days",
"""SELECT COUNT(*) FILTER (WHERE cpu_percent > 80) AS samples_above_80,
    COUNT(*) AS samples,
    ROUND(100.0 * COUNT(*) FILTER (WHERE cpu_percent > 80) / NULLIF(COUNT(*), 0), 1) AS percent_of_time
FROM system_metrics
WHERE timestamp > NOW() - INTERVAL '7 days'""",
"Share of samples above a CPU threshold (substitute the percent)"),
E("What is the average detection processing time overall",
"""SELECT ROUND(AVG(d.processing_time_ms)::numeric, 1) AS avg_processing_ms,
    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY d.processing_time_ms)::numeric, 1) AS p95_processing_ms,
    MAX(d.processing_time_ms) AS max_processing_ms, COUNT(*) AS detections
FROM detections d
WHERE d.processing_time_ms IS NOT NULL""",
"Average, 95th percentile and max processing time over detections"),
E("Which detections took the longest to process",
f"""SELECT d.id AS detection_id, d.timestamp AS seen_at, {CAM} AS camera_name, d.processing_time_ms
{DP}
WHERE d.processing_time_ms IS NOT NULL
ORDER BY d.processing_time_ms DESC
LIMIT 10""",
"Slowest detections"),
E("How did processing time change day by day",
"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections,
    ROUND(AVG(d.processing_time_ms)::numeric, 1) AS avg_processing_ms
FROM detections d
WHERE d.processing_time_ms IS NOT NULL
GROUP BY 1
ORDER BY detection_day""",
"Daily average processing time"),
E("At which hour of the day is processing slowest",
"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections,
    ROUND(AVG(d.processing_time_ms)::numeric, 1) AS avg_processing_ms
FROM detections d
WHERE d.processing_time_ms IS NOT NULL
GROUP BY 1
ORDER BY avg_processing_ms DESC
LIMIT 1""",
"The hour with the slowest average processing"),
E("What is the average image size of detections per camera",
f"""SELECT {CAM} AS camera_name, COUNT(*) AS detections,
    ROUND(AVG(d.image_size_bytes) / 1024.0, 1) AS avg_image_kb
{DP}
WHERE d.image_size_bytes IS NOT NULL
{GROUP_CAM}
ORDER BY avg_image_kb DESC""",
"Average image size in KB per camera"),
E("How many detections did each worker process",
"""SELECT d.worker_id, COUNT(*) AS detections, ROUND(AVG(d.processing_time_ms)::numeric, 1) AS avg_processing_ms
FROM detections d
GROUP BY d.worker_id
ORDER BY detections DESC""",
"Per-worker throughput"),
E("How many frames were received, processed and skipped according to the latest metrics",
"""SELECT timestamp, total_received, total_processed, total_skipped, total_faces_detected,
    ROUND(100.0 * total_skipped / NULLIF(total_received, 0), 1) AS skipped_percent
FROM system_metrics
ORDER BY timestamp DESC
LIMIT 1""",
"Pipeline throughput counters from the latest sample"),
E("Show the trend of total faces detected over the last 7 days from the metrics",
"""SELECT DATE(timestamp) AS metric_day, MAX(total_faces_detected) AS faces_detected_cumulative,
    MAX(total_processed) AS processed_cumulative
FROM system_metrics
WHERE timestamp > NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY metric_day""",
"Daily high-water marks of the cumulative counters"),
E("Is the system under load right now",
"""SELECT timestamp, cpu_percent, memory_percent, queue_size, processing_count, active_pipelines,
    CASE WHEN cpu_percent > 80 OR memory_percent > 80 OR queue_size > 100 THEN 'high' ELSE 'normal' END AS load_level
FROM system_metrics
ORDER BY timestamp DESC
LIMIT 1""",
"Latest sample with a simple load classification"),
]

# ---------------------------------------------------------------------------
# 10. Templated windows: the same question across every period users say
# ---------------------------------------------------------------------------
WINDOWS = [
    ("today", "DATE(d.timestamp) = CURRENT_DATE"),
    ("yesterday", "DATE(d.timestamp) = CURRENT_DATE - 1"),
    ("in the last hour", "d.timestamp > NOW() - INTERVAL '1 hour'"),
    ("in the last 24 hours", "d.timestamp > NOW() - INTERVAL '24 hours'"),
    ("in the last 7 days", "d.timestamp > NOW() - INTERVAL '7 days'"),
    ("in the last 30 days", "d.timestamp > NOW() - INTERVAL '30 days'"),
    ("this week", "d.timestamp >= DATE_TRUNC('week', NOW())"),
    ("this month", "d.timestamp >= DATE_TRUNC('month', NOW())"),
    ("last month", "DATE_TRUNC('month', d.timestamp) = DATE_TRUNC('month', NOW()) - INTERVAL '1 month'"),
    ("in August 2026", "d.timestamp >= DATE '2026-08-01' AND d.timestamp < DATE '2026-09-01'"),
]

for label, cond in WINDOWS:
    CATALOG += [
    E(f"How many detections were there {label}",
      f"""SELECT COUNT(*) AS detections, COUNT(DISTINCT d.pipeline_id) AS cameras_involved
FROM detections d
WHERE {cond}""",
      f"Total detections {label} (substitute the period if the user gave another)"),
    E(f"How many times was PERSON_NAME detected {label}",
      f"""SELECT f.name, COUNT(*) AS detections, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON} AND {cond}
GROUP BY f.name""",
      f"One person's detection count {label}; no rows means not seen"),
    E(f"Which cameras detected PERSON_NAME {label}",
      f"""SELECT {CAM} AS camera_name, COUNT(*) AS detections, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen
{FDP}
WHERE {PERSON} AND {cond}
{GROUP_CAM}
ORDER BY detections DESC""",
      f"Cameras that saw one person {label}"),
    E(f"Which camera was the busiest {label}",
      f"""SELECT {CAM} AS camera_name, COUNT(*) AS detections
{DP}
WHERE {cond}
{GROUP_CAM}
ORDER BY detections DESC
LIMIT 1""",
      f"The camera with the most detections {label}"),
    E(f"Which identified people were seen {label}",
      f"""SELECT f.name, COUNT(*) AS detections, COUNT(DISTINCT d.pipeline_id) AS cameras_seen, MAX(d.timestamp) AS last_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT} AND {cond}
GROUP BY f.name
ORDER BY detections DESC""",
      f"Identified people detected {label} (placeholders excluded)"),
    E(f"How many detections did CAMERA_NAME record {label}",
      f"""SELECT {CAM} AS camera_name, COUNT(d.id) AS detections
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id AND {cond}
WHERE {CAMF}
{GROUP_CAM}""",
      f"One camera's detections {label} (the window sits in the LEFT JOIN so a quiet camera returns 0)"),
    ]


def catalog_size() -> int:
    return len(CATALOG)
