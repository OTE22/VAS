"""Read-only, bounded inspection of the exact dataset artifact used by training.

The saved build report remains the authority for validation. Explorer statistics
describe the inspected prefix, independently of display filters, and never repair
or rewrite a dataset. No artifact deserialization (pickle/joblib) is involved.
"""
import json
import hashlib
import math
from collections import Counter
from pathlib import Path

SCAN_LIMIT = 100_000


def summarize_records(records, *, total_rows, schema, split=None, label=None,
                      query="", page=1, page_size=25):
    classes, present = Counter(), Counter()
    seen, feature_names, matches = set(), set(), []
    duplicates = invalid = scanned = 0
    for raw in records:
        scanned += 1
        row = dict(raw)
        try:
            features = json.loads(row.pop("features_json", "{}") or "{}")
            if not isinstance(features, dict):
                raise ValueError("features must be an object")
        except (ValueError, TypeError):
            features = {}
        bad = not features or not row.get("entity_id") or not row.get("as_of")
        clean = {}
        for name, value in features.items():
            feature_names.add(name)
            if value is not None:
                present[name] += 1
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    bad = True
                    value = None
                elif (name.endswith("_ratio") or name.endswith("_ratio_30d")) and not 0 <= value <= 1:
                    bad = True
            clean[name] = value
        row["features"] = clean
        key = (row.get("entity_id"), row.get("as_of"))
        duplicates += key in seen
        seen.add(key)
        invalid += bool(bad)
        classes[row.get("label") or "unlabelled"] += 1
        if split and row.get("split") != split:
            continue
        if label and (row.get("label") or "unlabelled") != label:
            continue
        if query and query.casefold() not in json.dumps(row, ensure_ascii=False).casefold():
            continue
        matches.append(row)
    start = (page - 1) * page_size
    return {
        "total_rows": total_rows, "scanned_rows": scanned,
        "truncated": scanned < total_rows, "scan_limit": SCAN_LIMIT,
        "schema": schema, "column_count": len(schema),
        "feature_count": len(feature_names),
        "class_distribution": dict(classes),
        "missing_values": {name: scanned - present[name] for name in sorted(feature_names)},
        "duplicates": duplicates, "invalid_rows": invalid,
        "invalid_rows_definition": "Missing identity/time, empty or malformed features, nonnumeric/nonfinite values, or ratios outside [0,1]. Build validation below also checks leakage and timestamps.",
        "items": matches[start:start + page_size], "filtered_rows": len(matches),
        "page": page, "page_size": page_size,
    }


def explore_dataset(row, artifact_root, **filters):
    import pyarrow.parquet as pq

    if not row.storage_path:
        raise FileNotFoundError("Dataset has no artifact")
    root = Path(artifact_root).resolve()
    path = Path(row.storage_path).resolve()
    if not path.is_relative_to(root) or path.suffix != ".parquet":
        raise ValueError("Dataset artifact is outside the configured root")
    with path.open("rb") as handle:
        expected_hash = getattr(row, "parquet_sha256", None)
        if expected_hash:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != expected_hash:
                raise ValueError("Dataset file checksum mismatch")
            handle.seek(0)
        parquet = pq.ParquetFile(handle)
        schema = [{"name": f.name, "type": str(f.type), "nullable": f.nullable}
                  for f in parquet.schema_arrow]

        def records():
            remaining = SCAN_LIMIT
            for batch in parquet.iter_batches(batch_size=1024):
                for item in batch.to_pylist()[:remaining]:
                    yield item
                remaining -= batch.num_rows
                if remaining <= 0:
                    break

        result = summarize_records(records(), total_rows=parquet.metadata.num_rows,
                                   schema=schema, **filters)
    result.update(grain="saved feature snapshot per entity/as-of anchor",
                  integrity_status="verified" if expected_hash else "unverified_legacy_artifact",
                  frame_dimensions_available=False, position_history_available=False,
                  dataset_id=str(row.id), version=row.version,
                  source=row.definition_name or "Feature snapshots",
                  checksum=row.checksum, validation_report=row.quality_report,
                  stored_missing_value_report=row.missing_value_report)
    return result


# Logical projections over EXISTING tables. These are not registered training
# definitions; inspecting them cannot create a dataset, label, snapshot or job.
ANALYTICS_GRAINS = {
    'observations': 'one saved face result (faces.id)',
    'sightings': 'one saved identity appearance (identity_appearances.id)',
    'camera_windows': 'camera / UTC window; independently counted sources',
    'co_occurrences': 'canonical identity pair / source appearance pair',
    'period_edges': 'canonical identity pair aggregated from the returned match population',
    'cached_edges': 'current mutable identity_relationships row; not a historical period edge',
    'events': 'source_type / source_id; score semantics remain source-specific',
    'reviews': 'one existing assessment with separate prediction and existing labels',
    'next_camera': 'observed adjacent camera transition; future target separate from inputs',
}


def analytics_scope(kind, start, end, pipeline_ids, limit=500, window_seconds=10):
    from datetime import timedelta
    from backend.core.trajectory_predictor import utc_naive
    start, end = utc_naive(start), utc_naive(end)
    pipelines = sorted(set(pipeline_ids))
    if kind not in ANALYTICS_GRAINS:
        raise ValueError('Unsupported analytical projection')
    if not pipelines or len(pipelines) > 30 or any(not p or len(p) > 255 for p in pipelines):
        raise ValueError('Select 1 to 30 cameras')
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise ValueError('Select a nonempty period of at most seven days')
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError('Row limit must be 1 to 2000')
    if type(window_seconds) is not int or not 1 <= window_seconds <= 3600:
        raise ValueError('Window must be 1 to 3600 seconds')
    return start, end, pipelines


async def explore_existing_data(db, *, kind, start, end, pipeline_ids, limit=500,
                                window_seconds=10, identity_id=None):
    """Bounded SELECT-only inspection in a caller-owned READ ONLY transaction.

    Only the existing admin ML_MANAGE surface may expose this estate-wide data.
    Every camera-based join uses the explicit camera scope; no raw embeddings,
    file paths, image bytes or credentials are returned. Source status/ownership
    is current, not a reconstruction of values before edits or identity merges.
    """
    from collections import defaultdict
    from datetime import datetime, timedelta
    import uuid
    from sqlalchemy import select, func, text, literal
    from db_models import (Detection, Face, IdentityAppearance as A, Pipeline,
        IdentityRelationship as R, ThreatAssessmentRecord as T, LiveAlertTrigger as L,
        MLLabel, MLPrediction)
    from backend.core.trajectory_predictor import observed_camera_examples

    start, end, pipelines = analytics_scope(kind, start, end, pipeline_ids, limit, window_seconds)
    identity = uuid.UUID(str(identity_id)) if identity_id else None
    if kind in ('co_occurrences', 'period_edges', 'next_camera') and identity is None:
        raise ValueError('Select an identity for pair or next-camera inspection')
    if kind == 'camera_windows' and identity is not None:
        raise ValueError('Camera windows count all saved observations; clear the identity filter')
    if db.new or db.dirty or db.deleted:
        raise ValueError('Analytics requires a clean read-only session')
    with db.no_autoflush:
        if (await db.execute(text('SHOW transaction_read_only'))).scalar_one() != 'on':
            raise ValueError('Analytics requires a READ ONLY transaction')
        known = set((await db.execute(select(Pipeline.pipeline_id).where(
            Pipeline.pipeline_id.in_(pipelines)))).scalars())
        if known != set(pipelines):
            raise ValueError('One or more selected cameras do not exist')
        metadata = dict(kind=kind, contract_version='existing-data-v1', grain=ANALYTICS_GRAINS[kind], read_only=True,
            period_start=start, period_end=end, interval='[start,end)', pipeline_ids=pipelines,
            limit=limit, truncated=False, captured_at=datetime.utcnow(),
            scene_coverage='unknown; selected saved observations, not continuous capture',
            unavailable={'occupancy': 'No continuous object history',
                         'speed': 'No scoped position sequence',
                         'normalized_coordinates': 'Original frame dimensions not retained',
                         'next_zone': 'No observed zone geometry/history'},
            mutable_history='Identity ownership, labels, review status and cache reflect current stored values.')
        def scope(model, timestamp):
            return (model.pipeline_id.in_(pipelines), timestamp >= start, timestamp < end)
        async def rows(query):
            result = [dict(r) for r in (await db.execute(query.limit(limit + 1))).mappings()]
            metadata['truncated'] |= len(result) > limit
            return result[:limit]
        appearance_filters = list(scope(A, A.start_time))
        if identity:
            appearance_filters.append(A.identity_id == identity)
        if kind == 'observations':
            q = select(Face.id.label('source_id'), Detection.id.label('detection_id'),
                Detection.uuid.label('detection_uuid'), Detection.pipeline_id, Detection.timestamp,
                Face.identity_id, Face.similarity, Face.label_state, Face.bbox_x1, Face.bbox_y1,
                Face.bbox_x2, Face.bbox_y2, Face.face_image_path.is_not(None).label('has_image_reference')
            ).join(Detection, Face.detection_id == Detection.id).where(*scope(Detection, Detection.timestamp))
            if identity:
                q = q.where(Face.identity_id == identity)
            items = await rows(q.order_by(Detection.timestamp, Face.id))
            for item in items:
                box = [item[k] for k in ('bbox_x1', 'bbox_y1', 'bbox_x2', 'bbox_y2')]
                valid = all(v is not None and math.isfinite(v) for v in box)
                valid = valid and box[2] > box[0] >= 0 and box[3] > box[1] >= 0
                item.update(coordinate_space='pixels', frame_dimensions_available=False,
                    roi_valid=bool(valid), roi_width=box[2]-box[0] if valid else None,
                    roi_height=box[3]-box[1] if valid else None,
                    center_x=(box[0]+box[2])/2 if valid else None,
                    center_y=(box[1]+box[3])/2 if valid else None)
        elif kind in ('sightings', 'next_camera'):
            q = select(A).where(*appearance_filters).order_by(A.start_time, A.id)
            if kind == 'next_camera':
                q = q.where(A.created_at < end)
            raw = list((await db.execute(q.limit(limit + 1))).scalars())
            metadata['truncated'] = len(raw) > limit
            raw = raw[:limit]
            if kind == 'sightings':
                items = [dict(source_id=a.id, identity_id=a.identity_id, pipeline_id=a.pipeline_id,
                    start_time=a.start_time, end_time=a.end_time, created_at=a.created_at,
                    event_id=a.event_id, detection_id=a.detection_id, detection_uuid=a.detection_uuid,
                    timestamp_source=a.timestamp_source, track_id=a.track_id,
                    has_image_reference=bool(a.best_snapshot_path)) for a in raw]
            else:
                if metadata['truncated'] and raw:
                    boundary = raw[-1].start_time
                    raw = [a for a in raw if a.start_time < boundary]
                items = observed_camera_examples(raw)
                metadata.update(session_gap_hours=2.0, source_appearance_count=len(raw),
                    input_policy='Only inputs available at anchor_time; target_time must be later',
                    target_policy='Observed next camera within selected camera/time scope; no next-zone label')
        elif kind == 'camera_windows':
            # Group each fact table independently. Joining faces to sightings or
            # labels before aggregation would multiply counts and invent volume.
            sources = [('detection_events', Detection, Detection.timestamp, None),
                       ('recognition_results', Detection, Detection.timestamp, Face),
                       ('saved_sightings', A, A.start_time, None),
                       ('distinct_observed_identities', A, A.start_time, None),
                       ('alert_triggers', L, L.created_at, None),
                       ('assessments', T, T.source_timestamp, None)]
            windows = {}
            names = [source[0] for source in sources]
            for name, model, timestamp, joined in sources:
                bucket = func.date_bin(literal(timedelta(seconds=window_seconds)), timestamp,
                                       literal(datetime(1970, 1, 1)))
                count = func.count(func.distinct(A.identity_id)) if name == 'distinct_observed_identities' else func.count()
                q = select(model.pipeline_id, bucket.label('window_start'), count.label('count')).select_from(model)
                if joined is not None:
                    q = q.join(Face, Face.detection_id == Detection.id)
                q = q.where(*scope(model, timestamp)).group_by(model.pipeline_id, bucket).order_by(bucket, model.pipeline_id)
                for item in await rows(q):
                    key = (item['window_start'], item['pipeline_id'])
                    windows.setdefault(key, dict.fromkeys(names, 0))[name] = item['count']
            items = [dict(pipeline_id=pid, window_start=stamp, window_seconds=window_seconds,
                          counts=windows[(stamp,pid)], coverage='saved_observations_only')
                     for stamp, pid in sorted(windows)[:limit]]
            metadata['truncated'] |= len(windows) > limit
            metadata.update(window_seconds=window_seconds, sparse=True,
                boundary_windows='Counts clipped to requested period; missing windows mean no saved observations, not an empty scene')
        elif kind in ('co_occurrences', 'period_edges'):
            from backend.core.intelligence_service import intelligence_service
            from config import settings
            matches = await intelligence_service._calculate_co_appearances(db, identity,
                settings.RELATED_IDENTITY_TIME_WINDOW_MINUTES, 1, 500, cutoff_date=start,
                period_end=end, pipeline_ids=pipelines, include_evidence=True)
            metadata.update(matches.metadata)
            metadata['truncated'] |= metadata.get('evidence_truncated', False)
            evidence = matches.evidence
            if kind == 'co_occurrences':
                items = evidence[:limit]
                metadata['truncated'] |= len(evidence) > limit
            else:
                grouped = defaultdict(list)
                for item in evidence:
                    grouped[(item['identity_id_1'], item['identity_id_2'])].append(item)
                items = []
                for (id1,id2), group in sorted(grouped.items()):
                    anchor_ids = {x['appearance_id_1' if id1 == str(identity) else 'appearance_id_2'] for x in group}
                    items.append(dict(identity_id_1=id1, identity_id_2=id2,
                        source_pair_count=len(group), matched_anchor_appearances=len(anchor_ids),
                        anchor_identity_id=str(identity), anchor_appearance_count=metadata['target_appearance_count'],
                        same_camera_count=sum(x['classification']=='same_camera' for x in group),
                        cross_camera_count=sum(x['classification']=='cross_camera' for x in group),
                        source_appearance_pairs=[x['evidence_key'] for x in group]))
                metadata['truncated'] |= len(items) > limit
                items = items[:limit]
            metadata['scope'] = 'Pairs involving selected anchor identity; not the entire estate graph'
            metadata['availability_cutoff'] = 'Both appearances were recorded before period_end; current identity ownership'
        elif kind == 'cached_edges':
            population = select(A.identity_id).where(*scope(A, A.start_time))
            q = select(R.id.label('source_id'), R.identity_id_1, R.identity_id_2,
                R.co_appearance_count, R.co_appearance_percentage, R.calculated_at,
                R.first_co_appearance, R.last_co_appearance).where(
                    R.identity_id_1.in_(population), R.identity_id_2.in_(population))
            if identity:
                q = q.where((R.identity_id_1 == identity) | (R.identity_id_2 == identity))
            items = await rows(q.order_by(R.identity_id_1, R.identity_id_2))
            metadata['scope'] = 'Current cache for identities seen in scope; cached counts may include other cameras/times'
        else:
            assessment_query = select(T.id.label('source_id'), T.person_id, T.pipeline_id,
                T.source_timestamp.label('timestamp'), T.total_risk_score, T.confidence,
                T.status, T.resolution_status, T.ml_prediction_id, T.model_version,
                T.created_at).where(*scope(T,T.source_timestamp))
            if identity:
                assessment_query = assessment_query.where(T.person_id == identity)
            assessments = await rows(assessment_query.order_by(T.source_timestamp,T.id))
            for a in assessments:
                a.update(source_type='threat_assessment', score_type='risk_score_0_100',
                         confidence_semantics='evidence coverage, not threat probability')
            if kind == 'events':
                q = select(L.id.label('source_id'), L.pipeline_id, L.created_at.label('timestamp'),
                    L.detection_id, L.similarity_score, L.acknowledged).where(*scope(L,L.created_at))
                if identity:
                    q = q.where(L.detection_id.in_(select(Face.detection_id).where(Face.identity_id == identity)))
                alerts = await rows(q.order_by(L.created_at,L.id))
                for a in alerts:
                    a.update(source_type='live_alert_trigger', score_type='similarity_score', timestamp_basis='created_at')
                combined = sorted(assessments+alerts, key=lambda a:(a['timestamp'], a['source_type'],str(a['source_id'])))
                metadata['truncated'] |= len(combined) > limit
                items = combined[:limit]
            else:
                items = assessments
                ids = [a['source_id'] for a in items]
                # Separate result sets preserve all labels without multiplying
                # assessment rows or replacing model output with a human outcome.
                labels = await rows(select(MLLabel.id, MLLabel.assessment_id, MLLabel.label,
                    MLLabel.label_kind, MLLabel.review_status, MLLabel.status, MLLabel.reviewed_at,
                    MLLabel.reviewed_by, MLLabel.event_time, MLLabel.label_definition_version,
                    MLLabel.supersedes_id).where(MLLabel.assessment_id.in_(ids)).order_by(MLLabel.id)) if ids else []
                prediction_ids = [a['ml_prediction_id'] for a in items if a['ml_prediction_id']]
                predictions = await rows(select(MLPrediction.id, MLPrediction.behavioral_anomaly_score,
                    MLPrediction.score_type, MLPrediction.is_probability, MLPrediction.calibration_status,
                    MLPrediction.model_version_label, MLPrediction.as_of_timestamp).where(
                        MLPrediction.id.in_(prediction_ids)).order_by(MLPrediction.id)) if prediction_ids else []
                for a in items:
                    a['labels'] = [label for label in labels if label['assessment_id'] == a['source_id']]
                    a['prediction'] = next((p for p in predictions if p['id'] == a['ml_prediction_id']), None)
                metadata['review_scope'] = 'Existing assessments with a selected camera; unlinked/global labels are not attributed to a camera'
        metadata['returned_rows'] = len(items)
        metadata['status'] = 'partial' if metadata['truncated'] else 'available' if items else 'no_saved_observations'
        return {'metadata': metadata, 'items': items}
