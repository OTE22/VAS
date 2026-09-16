"""Persistent dashboard inbox projected from detection alert history.

No evidence is coalesced or deleted: grouping is a read projection. Acknowledging
an episode only updates its unacknowledged rows through the displayed boundary.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import text
from config import settings
from backend.utils.time_utils import iso_utc

# Window functions keep grouping in PostgreSQL, before pagination. A camera
# change, a gap, or acknowledgement starts another operator-visible episode.
SESSIONS = """
WITH events AS (
    SELECT 'watchlist' AS source, a.id, a.watchlist_entry_id AS rule_id,
        a.pipeline_id, a.detection_id, a.snapshot_path, a.similarity_score, a.created_at,
        e.identity_id, e.action_instructions, w.name AS rule_name,
        lower(w.alert_level::text) AS alert_level, true AS sound_alert
    FROM watchlist_alerts a
    JOIN watchlist_entries e ON e.id = a.watchlist_entry_id
    JOIN watchlists w ON w.id = e.watchlist_id
    WHERE a.triggered_by = 'detection' AND NOT a.acknowledged AND w.notify_dashboard
    UNION ALL
    SELECT 'live' AS source, a.id, a.alert_id AS rule_id,
        a.pipeline_id, a.detection_id, a.snapshot_path, a.similarity_score, a.created_at,
        l.identity_id, NULL AS action_instructions, l.name AS rule_name,
        l.alert_level, l.sound_alert
    FROM live_alert_triggers a JOIN live_search_alerts l ON l.id = a.alert_id
    WHERE NOT a.acknowledged AND l.notify_dashboard
), previous AS (
    SELECT *, lag(created_at) OVER (
        PARTITION BY source, rule_id, pipeline_id ORDER BY created_at, id
    ) AS previous_at FROM events
), sessions AS (
    SELECT *, sum(CASE WHEN previous_at IS NULL OR
        created_at - previous_at > make_interval(secs => :gap_seconds)
        THEN 1 ELSE 0 END) OVER (
        PARTITION BY source, rule_id, pipeline_id ORDER BY created_at, id
    ) AS session_number FROM previous
), episodes AS (
    SELECT DISTINCT ON (source, rule_id, pipeline_id, session_number)
        source, rule_id, pipeline_id, session_number,
        identity_id, action_instructions, rule_name, alert_level, sound_alert,
        first_value(id) OVER episode AS first_id,
        first_value(id) OVER latest AS latest_id,
        first_value(detection_id) OVER latest AS detection_id,
        first_value(snapshot_path) OVER latest AS snapshot_path,
        first_value(similarity_score) OVER latest AS similarity_score,
        min(created_at) OVER episode AS first_seen_at,
        max(created_at) OVER episode AS last_seen_at,
        count(*) OVER episode AS sightings
    FROM sessions
    WINDOW episode AS (PARTITION BY source, rule_id, pipeline_id, session_number
        ORDER BY created_at, id ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING),
    latest AS (PARTITION BY source, rule_id, pipeline_id, session_number
        ORDER BY created_at DESC, id DESC)
)
"""


def params():
    return {'gap_seconds': int(settings.DASHBOARD_ALERT_GROUP_GAP_SECONDS)}


async def list_inbox(db, *, limit=50, offset=0, sounds_since=None):
    observed_at = datetime.now(timezone.utc)
    rows = (await db.execute(text(SESSIONS + """
        SELECT x.*, i.display_name AS identity_name, p.location_name,
               count(*) OVER () AS total_groups
        FROM episodes x
        LEFT JOIN identities i ON i.id = x.identity_id
        LEFT JOIN pipelines p ON p.pipeline_id = x.pipeline_id
        ORDER BY CASE x.alert_level WHEN 'critical' THEN 0
            WHEN 'warning' THEN 1 ELSE 2 END, x.last_seen_at DESC, x.first_id
        LIMIT :limit OFFSET :offset
    """), {**params(), 'limit': limit, 'offset': offset})).mappings().all()
    items = []
    for row in rows:
        item = dict(row)
        for key in ('first_id', 'latest_id', 'rule_id', 'identity_id'):
            item[key] = str(item[key]) if item[key] is not None else None
        for key in ('first_seen_at', 'last_seen_at'):
            item[key] = iso_utc(item[key])
        item.pop('total_groups', None)
        item.pop('session_number', None)
        # Snapshot files are served by a separately authorized route, never
        # exposed as arbitrary filesystem paths supplied to the browser.
        item['snapshot_url'] = f"/api/detection-alerts/{item['source']}/{item['latest_id']}/snapshot" if item.pop('snapshot_path') else None
        items.append(item)
    # Count separately only for an out-of-range page (e.g. another admin acked).
    total = int(rows[0]['total_groups']) if rows else int((await db.execute(
        text(SESSIONS + 'SELECT count(*) FROM episodes'), params())).scalar_one())
    # Sound candidates are independent of pagination/severity ordering: a new
    # Info alert must still be heard when a backlog of Critical cards fills page 1.
    sounds = []
    if sounds_since is not None:
        if sounds_since.tzinfo is not None:
            sounds_since = sounds_since.astimezone(timezone.utc).replace(tzinfo=None)
        sound_rows = (await db.execute(text(SESSIONS + '''
            SELECT source, first_id, alert_level FROM episodes
            WHERE first_seen_at >= :sounds_since AND sound_alert
        '''), {**params(), 'sounds_since': sounds_since})).mappings().all()
        sounds = [dict(row, first_id=str(row['first_id'])) for row in sound_rows]
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset,
            'observed_at': iso_utc(observed_at), 'sound_candidates': sounds,
            'group_gap_seconds': params()['gap_seconds']}


async def acknowledge_episode(db, source, first_id, latest_id, user_id):
    tables = {'watchlist': 'watchlist_alerts', 'live': 'live_alert_triggers'}
    table = tables[source]  # allowlist only; never interpolate client SQL
    values = {'source': source, **params(), 'first_id': uuid.UUID(str(first_id)),
              'latest_id': uuid.UUID(str(latest_id)), 'user_id': user_id}
    # Both boundaries must belong to the SAME current episode. Newer events
    # stay pending, and retries cannot overwrite the first acknowledger.
    rows = (await db.execute(text(SESSIONS + f"""
        UPDATE {table} a
        SET acknowledged = true, acknowledged_by = :user_id,
            acknowledged_at = timezone('UTC', now())
        FROM sessions s, episodes e, sessions boundary
        WHERE e.source = :source AND e.first_id = :first_id AND boundary.id = :latest_id
          AND boundary.source = e.source AND boundary.rule_id = e.rule_id
          AND boundary.pipeline_id IS NOT DISTINCT FROM e.pipeline_id
          AND boundary.session_number = e.session_number
          AND s.source = e.source AND s.rule_id = e.rule_id
          AND s.pipeline_id IS NOT DISTINCT FROM e.pipeline_id
          AND s.session_number = e.session_number
          AND (s.created_at, s.id) <= (boundary.created_at, boundary.id)
          AND a.id = s.id AND NOT a.acknowledged
        RETURNING a.id
    """), values)).all()
    await db.commit()
    return len(rows)
