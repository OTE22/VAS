"""
Generated verified seeds: subject x period x metric
====================================================

The second catalog. Where `seed_catalog.py` is hand-written shapes, this
module composes verified building blocks - a subject filter, a period, and a
metric - into several hundred question/SQL pairs, each phrased the way a
user asks it. The SQL is correct by construction (every block is one of the
audited fragments below) and every generated pair is still EXECUTED against
the schema before release (scratch `catalog_check.py`).

Combinations that make no sense are skipped: a per-weekday breakdown of the
last hour, a weekly trend of a single day, a daily trend of "today".

Placeholders: PERSON_NAME, OTHER_PERSON, CAMERA_NAME (see seed_catalog.py).
"""
from .seed_catalog import IDENT, UNID, CAM, PERSON, CAMF, GROUP_CAM, E

FDP = ("FROM faces f\nJOIN detections d ON f.detection_id = d.id\n"
       "JOIN pipelines p ON p.pipeline_id = d.pipeline_id")
DP = "FROM detections d\nJOIN pipelines p ON p.pipeline_id = d.pipeline_id"

# (label, SQL condition on d.timestamp, kind)  kind: sub_day | single_day | multi_day | long
PERIODS = [
    ("today", "DATE(d.timestamp) = CURRENT_DATE", "single_day"),
    ("yesterday", "DATE(d.timestamp) = CURRENT_DATE - 1", "single_day"),
    ("on 2026-08-17", "DATE(d.timestamp) = DATE '2026-08-17'", "single_day"),
    ("in the last hour", "d.timestamp > NOW() - INTERVAL '1 hour'", "sub_day"),
    ("in the last 6 hours", "d.timestamp > NOW() - INTERVAL '6 hours'", "sub_day"),
    ("in the last 12 hours", "d.timestamp > NOW() - INTERVAL '12 hours'", "sub_day"),
    ("in the last 24 hours", "d.timestamp > NOW() - INTERVAL '24 hours'", "sub_day"),
    ("in the last 3 days", "d.timestamp > NOW() - INTERVAL '3 days'", "multi_day"),
    ("in the last 7 days", "d.timestamp > NOW() - INTERVAL '7 days'", "multi_day"),
    ("in the last 14 days", "d.timestamp > NOW() - INTERVAL '14 days'", "multi_day"),
    ("in the last 30 days", "d.timestamp > NOW() - INTERVAL '30 days'", "long"),
    ("in the last 90 days", "d.timestamp > NOW() - INTERVAL '90 days'", "long"),
    ("this week", "d.timestamp >= DATE_TRUNC('week', NOW())", "multi_day"),
    ("last week", "d.timestamp >= DATE_TRUNC('week', NOW()) - INTERVAL '7 days' AND d.timestamp < DATE_TRUNC('week', NOW())", "multi_day"),
    ("this month", "d.timestamp >= DATE_TRUNC('month', NOW())", "long"),
    ("last month", "DATE_TRUNC('month', d.timestamp) = DATE_TRUNC('month', NOW()) - INTERVAL '1 month'", "long"),
    ("this year", "d.timestamp >= DATE_TRUNC('year', NOW())", "long"),
    ("between 2026-08-17 and 2026-08-20", "d.timestamp >= DATE '2026-08-17' AND d.timestamp < DATE '2026-08-21'", "multi_day"),
    ("since 2026-08-20", "d.timestamp >= DATE '2026-08-20'", "long"),
    ("before 2026-08-15", "d.timestamp < DATE '2026-08-15'", "long"),
]

SUBST = "(substitute the period, person or camera the user gave)"


def _pick(variants, i):
    return variants[i % len(variants)]


# Each metric: (name, allowed kinds, question phrasings, sql builder, purpose)
# The builder receives the period condition and returns SQL.
def _m(name, kinds, phrasings, build, purpose):
    return {"name": name, "kinds": set(kinds), "phrasings": phrasings, "build": build, "purpose": purpose}


ALL = {"sub_day", "single_day", "multi_day", "long"}
DAYS = {"multi_day", "long"}
WEEKS = {"long"}

METRICS = [
# ---------------------------------------------------------------- everything
_m("all_per_day", DAYS,
   ["How many detections per day {p}", "Show the daily detection counts {p}", "Give me a day-by-day breakdown of detections {p}"],
   lambda c: f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
FROM detections d
WHERE {c}
GROUP BY 1
ORDER BY detection_day""",
   "Daily detection counts inside the period"),
_m("all_per_hour", ALL,
   ["How many detections per hour of the day {p}", "Show the hourly distribution of detections {p}", "At which hours were detections recorded {p}"],
   lambda c: f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
FROM detections d
WHERE {c}
GROUP BY 1
ORDER BY hour_of_day""",
   "Detections per hour of day inside the period"),
_m("all_per_weekday", DAYS,
   ["How many detections per weekday {p}", "Which weekdays were busiest {p}"],
   lambda c: f"""SELECT TRIM(TO_CHAR(d.timestamp, 'Day')) AS weekday, COUNT(*) AS detections
FROM detections d
WHERE {c}
GROUP BY 1, EXTRACT(DOW FROM d.timestamp)
ORDER BY detections DESC""",
   "Detections per weekday inside the period, busiest first"),
_m("all_per_week", WEEKS,
   ["How many detections per week {p}", "Show the weekly detection trend {p}"],
   lambda c: f"""SELECT DATE_TRUNC('week', d.timestamp)::date AS week_starting_monday, COUNT(*) AS detections
FROM detections d
WHERE {c}
GROUP BY 1
ORDER BY week_starting_monday""",
   "Weekly detection counts inside the period"),
_m("all_per_camera", ALL,
   ["Rank the cameras by detections {p}", "How many detections did each camera record {p}", "Which cameras were active {p} and how busy were they"],
   lambda c: f"""SELECT {CAM} AS camera_name, COUNT(d.id) AS detections
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id AND {c}
{GROUP_CAM}
ORDER BY detections DESC""",
   "Per-camera detection counts inside the period; silent cameras show 0 (the period sits in the LEFT JOIN)"),
_m("all_first_last", ALL,
   ["When were the first and last detections {p}", "What is the time span of detections {p}"],
   lambda c: f"""SELECT MIN(d.timestamp) AS first_detection, MAX(d.timestamp) AS last_detection, COUNT(*) AS detections
FROM detections d
WHERE {c}""",
   "First and last detection inside the period"),
_m("all_busiest_day", DAYS,
   ["Which day had the most detections {p}", "What was the busiest day {p}"],
   lambda c: f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
FROM detections d
WHERE {c}
GROUP BY 1
ORDER BY detections DESC
LIMIT 1""",
   "The busiest day inside the period"),
_m("all_busiest_hour", ALL,
   ["Which hour was the busiest {p}", "At what hour did most detections happen {p}"],
   lambda c: f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
FROM detections d
WHERE {c}
GROUP BY 1
ORDER BY detections DESC
LIMIT 1""",
   "The busiest hour of day inside the period"),
_m("all_people_and_unidentified", ALL,
   ["How many identified people and how many unidentified faces were there {p}", "What share of faces was unidentified {p}"],
   lambda c: f"""SELECT COUNT(DISTINCT CASE WHEN {IDENT} THEN f.name END) AS identified_people,
    COUNT(*) FILTER (WHERE {IDENT}) AS identified_faces,
    COUNT(*) FILTER (WHERE {UNID}) AS unidentified_faces,
    ROUND(100.0 * COUNT(*) FILTER (WHERE {UNID}) / NULLIF(COUNT(*), 0), 1) AS unidentified_percent
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {c}""",
   "Identified people, identified faces, unidentified faces and the unidentified share inside the period"),
_m("all_avg_per_day", DAYS,
   ["What was the average number of detections per day {p}", "How many detections a day on average {p}"],
   lambda c: f"""WITH per_day AS (SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections FROM detections d WHERE {c} GROUP BY 1)
SELECT ROUND(AVG(detections), 1) AS avg_per_active_day, MAX(detections) AS busiest_day_count, COUNT(*) AS active_days
FROM per_day""",
   "Average detections per active day inside the period"),
_m("all_avg_processing", ALL,
   ["What was the average processing time {p}", "How fast were detections processed {p}"],
   lambda c: f"""SELECT COUNT(*) AS detections, ROUND(AVG(d.processing_time_ms)::numeric, 1) AS avg_processing_ms,
    MAX(d.processing_time_ms) AS max_processing_ms
FROM detections d
WHERE {c} AND d.processing_time_ms IS NOT NULL""",
   "Processing-time statistics inside the period"),
_m("all_top_person", ALL,
   ["Who was detected the most {p}", "Which identified person was seen most often {p}"],
   lambda c: f"""SELECT f.name, COUNT(*) AS detections, COUNT(DISTINCT d.pipeline_id) AS cameras_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT} AND {c}
GROUP BY f.name
ORDER BY detections DESC
LIMIT 1""",
   "The most detected identified person inside the period (placeholders excluded)"),
_m("all_people_ranked", ALL,
   ["Rank identified people by detections {p}", "How many times was each identified person seen {p}"],
   lambda c: f"""SELECT f.name, COUNT(*) AS detections, COUNT(DISTINCT d.pipeline_id) AS cameras_seen, MAX(d.timestamp) AS last_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT} AND {c}
GROUP BY f.name
ORDER BY detections DESC""",
   "Identified people ranked by detections inside the period"),
_m("all_avg_similarity", ALL,
   ["What was the average similarity of recognized faces {p}", "How good were the matches {p}"],
   lambda c: f"""SELECT COUNT(*) AS recognized_faces, ROUND(AVG(f.similarity)::numeric, 3) AS avg_similarity,
    MIN(f.similarity) AS min_similarity, MAX(f.similarity) AS max_similarity
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT} AND {c}""",
   "Similarity statistics of identified faces inside the period"),
_m("all_silent_cameras", ALL,
   ["Which cameras recorded nothing {p}", "Which cameras had zero detections {p}"],
   lambda c: f"""SELECT {CAM} AS camera_name, p.is_active
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id AND {c}
{GROUP_CAM}, p.is_active
HAVING COUNT(d.id) = 0
ORDER BY camera_name""",
   "Cameras with no detections inside the period (LEFT JOIN with the period in the join, HAVING zero)"),
_m("all_absent_people", ALL,
   ["Which identified people were not seen {p}", "Who was absent {p}"],
   lambda c: f"""SELECT f.name, MAX(d.timestamp) AS last_seen_ever
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT}
GROUP BY f.name
HAVING COUNT(*) FILTER (WHERE {c}) = 0
ORDER BY f.name""",
   "Identified people with zero detections inside the period, with their last sighting ever"),
_m("all_multi_camera_people", ALL,
   ["Which people moved between cameras {p}", "Who was seen at more than one camera {p}"],
   lambda c: f"""SELECT f.name, COUNT(DISTINCT d.pipeline_id) AS cameras_seen, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {IDENT} AND {c}
GROUP BY f.name
HAVING COUNT(DISTINCT d.pipeline_id) > 1
ORDER BY cameras_seen DESC""",
   "People seen at several cameras inside the period"),
_m("all_camera_share", ALL,
   ["What share of detections did each camera have {p}", "How were detections split across cameras {p}"],
   lambda c: f"""WITH per_camera AS (SELECT {CAM} AS camera_name, COUNT(*) AS detections {DP} WHERE {c} {GROUP_CAM})
SELECT camera_name, detections, ROUND(100.0 * detections / (SELECT SUM(detections) FROM per_camera), 1) AS percent_of_period
FROM per_camera
ORDER BY detections DESC""",
   "Each camera's percentage of the period's detections"),
_m("all_unidentified_per_camera", ALL,
   ["How many unidentified faces did each camera produce {p}", "Where did the unknown faces come from {p}"],
   lambda c: f"""SELECT {CAM} AS camera_name, COUNT(*) FILTER (WHERE {UNID}) AS unidentified_faces, COUNT(*) AS faces
{FDP}
WHERE {c}
{GROUP_CAM}
ORDER BY unidentified_faces DESC""",
   "Unidentified faces per camera inside the period"),
_m("all_latest", ALL,
   ["Show the latest detections {p}", "What were the most recent events {p}"],
   lambda c: f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, COALESCE(f.name, 'Unknown') AS name, f.similarity
FROM detections d
JOIN pipelines p ON p.pipeline_id = d.pipeline_id
LEFT JOIN faces f ON f.detection_id = d.id
WHERE {c}
ORDER BY d.timestamp DESC
LIMIT 50""",
   "The most recent detections inside the period with recognised names"),
# -------------------------------------------------------------- one person
_m("person_per_day", DAYS,
   ["How many times was PERSON_NAME detected each day {p}", "Show PERSON_NAME's daily detection counts {p}"],
   lambda c: f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON} AND {c}
GROUP BY 1
ORDER BY detection_day""",
   "One person's daily counts inside the period"),
_m("person_per_hour", ALL,
   ["At what hours was PERSON_NAME detected {p}", "Show PERSON_NAME's detections by hour {p}"],
   lambda c: f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON} AND {c}
GROUP BY 1
ORDER BY hour_of_day""",
   "One person's detections per hour of day inside the period"),
_m("person_first_last", ALL,
   ["When was PERSON_NAME first and last seen {p}", "Between what times was PERSON_NAME present {p}"],
   lambda c: f"""SELECT f.name, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen, COUNT(*) AS detections,
    COUNT(DISTINCT d.pipeline_id) AS cameras_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON} AND {c}
GROUP BY f.name""",
   "One person's first and last sighting inside the period"),
_m("person_top_camera", ALL,
   ["Which camera saw PERSON_NAME most {p}", "Where was PERSON_NAME mostly {p}"],
   lambda c: f"""SELECT {CAM} AS camera_name, COUNT(*) AS detections
{FDP}
WHERE {PERSON} AND {c}
{GROUP_CAM}
ORDER BY detections DESC
LIMIT 1""",
   "The camera with the most detections of one person inside the period"),
_m("person_timeline", ALL,
   ["Show every detection of PERSON_NAME {p}", "Track PERSON_NAME {p}", "Give me PERSON_NAME's timeline {p}"],
   lambda c: f"""SELECT d.timestamp AS seen_at, {CAM} AS camera_name, f.similarity, f.face_image_path
{FDP}
WHERE {PERSON} AND {c}
ORDER BY d.timestamp ASC""",
   "One person's full timeline inside the period"),
_m("person_last_location", ALL,
   ["Where was PERSON_NAME last seen {p}", "What is PERSON_NAME's most recent location {p}"],
   lambda c: f"""SELECT d.timestamp AS last_seen, {CAM} AS camera_name, f.similarity
{FDP}
WHERE {PERSON} AND {c}
ORDER BY d.timestamp DESC
LIMIT 1""",
   "One person's most recent sighting inside the period"),
_m("person_avg_gap", ALL,
   ["What was the average gap between PERSON_NAME's detections {p}", "How often was PERSON_NAME detected, in minutes between sightings, {p}"],
   lambda c: f"""WITH ordered AS (
    SELECT d.timestamp AS seen_at, LAG(d.timestamp) OVER (ORDER BY d.timestamp) AS previous_seen_at
    FROM faces f JOIN detections d ON f.detection_id = d.id
    WHERE {PERSON} AND {c})
SELECT ROUND(AVG(EXTRACT(EPOCH FROM (seen_at - previous_seen_at)) / 60)::numeric, 1) AS avg_gap_minutes, COUNT(*) AS gaps
FROM ordered WHERE previous_seen_at IS NOT NULL""",
   "Average minutes between one person's consecutive detections inside the period"),
_m("person_camera_changes", ALL,
   ["How did PERSON_NAME move between cameras {p}", "List PERSON_NAME's camera changes {p}"],
   lambda c: f"""WITH steps AS (
    SELECT d.timestamp AS seen_at, {CAM} AS camera_name, LAG({CAM}) OVER (ORDER BY d.timestamp) AS previous_camera
    {FDP}
    WHERE {PERSON} AND {c})
SELECT seen_at, previous_camera, camera_name FROM steps
WHERE previous_camera IS DISTINCT FROM camera_name
ORDER BY seen_at""",
   "One person's camera-to-camera moves inside the period (only rows where the camera changed)"),
_m("person_similarity", ALL,
   ["How well was PERSON_NAME matched {p}", "What was PERSON_NAME's average similarity {p}"],
   lambda c: f"""SELECT f.name, COUNT(*) AS detections, ROUND(AVG(f.similarity)::numeric, 3) AS avg_similarity,
    MIN(f.similarity) AS worst, MAX(f.similarity) AS best
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE {PERSON} AND {c}
GROUP BY f.name""",
   "One person's similarity statistics inside the period"),
_m("person_companions", ALL,
   ["Who was seen near PERSON_NAME {p}", "Who else appeared at the same camera within 5 minutes of PERSON_NAME {p}"],
   lambda c: f"""SELECT DISTINCT f2.name AS other_person, {CAM} AS camera_name
FROM faces f JOIN detections d ON f.detection_id = d.id
JOIN detections d2 ON d2.pipeline_id = d.pipeline_id AND d2.id <> d.id
    AND d2.timestamp BETWEEN d.timestamp - INTERVAL '5 minutes' AND d.timestamp + INTERVAL '5 minutes'
JOIN faces f2 ON f2.detection_id = d2.id
JOIN pipelines p ON p.pipeline_id = d.pipeline_id
WHERE {PERSON} AND {c}
  AND f2.name IS NOT NULL AND f2.name <> '' AND LOWER(f2.name) NOT LIKE 'unknown%' AND LOWER(f2.name) NOT LIKE 'person_%'
  AND LOWER(f2.name) NOT LIKE LOWER('%PERSON_NAME%')
ORDER BY other_person""",
   "Identified people at the same camera within 5 minutes of one person inside the period"),
# ---------------------------------------------------------------- one camera
_m("camera_per_day", DAYS,
   ["How many detections per day at CAMERA_NAME {p}", "Show CAMERA_NAME's daily activity {p}"],
   lambda c: f"""SELECT DATE(d.timestamp) AS detection_day, COUNT(*) AS detections
{DP}
WHERE {CAMF} AND {c}
GROUP BY 1
ORDER BY detection_day""",
   "One camera's daily counts inside the period"),
_m("camera_per_hour", ALL,
   ["Show the hourly activity of CAMERA_NAME {p}", "At what hours was CAMERA_NAME busy {p}"],
   lambda c: f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
{DP}
WHERE {CAMF} AND {c}
GROUP BY 1
ORDER BY hour_of_day""",
   "One camera's detections per hour of day inside the period"),
_m("camera_people", ALL,
   ["Which identified people did CAMERA_NAME see {p}", "Who passed CAMERA_NAME {p}"],
   lambda c: f"""SELECT f.name, COUNT(*) AS detections, MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen
{FDP}
WHERE {CAMF} AND {IDENT} AND {c}
GROUP BY f.name
ORDER BY detections DESC""",
   "Identified people at one camera inside the period"),
_m("camera_unidentified_share", ALL,
   ["What share of faces at CAMERA_NAME was unidentified {p}", "How many unknown faces did CAMERA_NAME see {p}"],
   lambda c: f"""SELECT {CAM} AS camera_name, COUNT(*) AS faces, COUNT(*) FILTER (WHERE {UNID}) AS unidentified_faces,
    ROUND(100.0 * COUNT(*) FILTER (WHERE {UNID}) / NULLIF(COUNT(*), 0), 1) AS unidentified_percent
{FDP}
WHERE {CAMF} AND {c}
{GROUP_CAM}""",
   "Unidentified share at one camera inside the period"),
_m("camera_busiest_hour", ALL,
   ["What was the busiest hour at CAMERA_NAME {p}", "When was CAMERA_NAME most active {p}"],
   lambda c: f"""SELECT EXTRACT(HOUR FROM d.timestamp)::int AS hour_of_day, COUNT(*) AS detections
{DP}
WHERE {CAMF} AND {c}
GROUP BY 1
ORDER BY detections DESC
LIMIT 1""",
   "The busiest hour at one camera inside the period"),
_m("camera_first_last", ALL,
   ["When did CAMERA_NAME record its first and last detection {p}", "What was CAMERA_NAME's active span {p}"],
   lambda c: f"""SELECT {CAM} AS camera_name, COUNT(d.id) AS detections, MIN(d.timestamp) AS first_detection, MAX(d.timestamp) AS last_detection
FROM pipelines p LEFT JOIN detections d ON d.pipeline_id = p.pipeline_id AND {c}
WHERE {CAMF}
{GROUP_CAM}""",
   "One camera's first and last detection inside the period (0 and NULLs when silent)"),
_m("camera_avg_processing", ALL,
   ["How fast did CAMERA_NAME process detections {p}", "What was CAMERA_NAME's average processing time {p}"],
   lambda c: f"""SELECT {CAM} AS camera_name, COUNT(*) AS detections, ROUND(AVG(d.processing_time_ms)::numeric, 1) AS avg_processing_ms
{DP}
WHERE {CAMF} AND {c} AND d.processing_time_ms IS NOT NULL
{GROUP_CAM}""",
   "One camera's processing time inside the period"),
_m("camera_latest", ALL,
   ["Show the latest detections at CAMERA_NAME {p}", "What happened at CAMERA_NAME {p}"],
   lambda c: f"""SELECT d.timestamp AS seen_at, COALESCE(f.name, 'Unknown') AS name, f.similarity
FROM detections d
JOIN pipelines p ON p.pipeline_id = d.pipeline_id
LEFT JOIN faces f ON f.detection_id = d.id
WHERE {CAMF} AND {c}
ORDER BY d.timestamp DESC
LIMIT 50""",
   "Recent events at one camera inside the period"),
_m("camera_people_count", ALL,
   ["How many different people did CAMERA_NAME identify {p}", "How many distinct identified people passed CAMERA_NAME {p}"],
   lambda c: f"""SELECT {CAM} AS camera_name, COUNT(DISTINCT CASE WHEN {IDENT} THEN f.name END) AS identified_people, COUNT(*) AS faces
{FDP}
WHERE {CAMF} AND {c}
{GROUP_CAM}""",
   "Distinct identified people at one camera inside the period"),
# ---------------------------------------------------------------- two people
_m("two_people_compare", ALL,
   ["Compare PERSON_NAME and OTHER_PERSON {p}", "Who was seen more, PERSON_NAME or OTHER_PERSON, {p}"],
   lambda c: f"""SELECT f.name, COUNT(*) AS detections, COUNT(DISTINCT d.pipeline_id) AS cameras_seen,
    MIN(d.timestamp) AS first_seen, MAX(d.timestamp) AS last_seen
FROM faces f JOIN detections d ON f.detection_id = d.id
WHERE ({PERSON} OR LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')) AND {c}
GROUP BY f.name
ORDER BY detections DESC""",
   "Two people side by side inside the period"),
_m("two_people_shared_cameras", ALL,
   ["Which cameras saw both PERSON_NAME and OTHER_PERSON {p}", "Where were PERSON_NAME and OTHER_PERSON both seen {p}"],
   lambda c: f"""SELECT {CAM} AS camera_name,
    COUNT(*) FILTER (WHERE {PERSON}) AS person_name_detections,
    COUNT(*) FILTER (WHERE LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')) AS other_person_detections
{FDP}
WHERE ({PERSON} OR LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')) AND {c}
{GROUP_CAM}
HAVING COUNT(*) FILTER (WHERE {PERSON}) > 0 AND COUNT(*) FILTER (WHERE LOWER(f.name) LIKE LOWER('%OTHER_PERSON%')) > 0
ORDER BY camera_name""",
   "Cameras that saw both people inside the period (HAVING both counts > 0)"),
]


def _generate():
    out = []
    i = 0
    for metric in METRICS:
        for label, cond, kind in PERIODS:
            if kind not in metric["kinds"]:
                continue
            question = _pick(metric["phrasings"], i).format(p=label)
            question = question.replace(", ,", ",").strip()
            purpose = f"{metric['purpose']} - period: {label} {SUBST}"
            out.append(E(question, metric["build"](cond), purpose))
            i += 1
    return out


GENERATED = _generate()


def generated_size() -> int:
    return len(GENERATED)
