"""Read-only prerequisites shared by guided controls and registry commands."""
from datetime import datetime, timedelta
from sqlalchemy import text
from backend.ml.model_specs import get_model_spec


def connection_blockers(row, model_type):
    spec = get_model_spec(model_type)
    if spec.serving_mode == 'offline_regression':
        return [{'code': 'OFFLINE_ONLY', 'message': 'Numeric regression remains an offline experiment.', 'action': 'advanced'}]
    if row is None:
        return [{'code': 'NO_MODEL', 'message': 'Prepare data and train a model for this service.', 'action': 'prepare_train'}]
    out = []
    def block(code, message):
        out.append({'code': code, 'message': message, 'action': 'review'})
    report, config = row.evaluation_report or {}, row.training_config or {}
    engineering = (report.get('engineering_gate') or {}).get('status') or config.get('engineering_gate')
    if row.feature_set_version != spec.feature_set_version:
        block('FEATURE_SCHEMA_MISMATCH', 'Train with the current feature schema for this service.')
    if (row.quality_gates or {}).get('passed') is not True or engineering != 'PASS':
        block('ENGINEERING_REVIEW_REQUIRED', 'Engineering checks must pass before connection. Review model readiness.')
    if not getattr(row, 'dataset_id', None) or not getattr(row, 'code_version', None):
        block('MODEL_LINEAGE_REQUIRED', 'Dataset and training revision must be recorded. Train a new candidate.')
    splits = report.get('splits') or {}
    def positive(value):
        return type(value) is int and value > 0
    missing = [name for name in ('train', 'val', 'test') if not positive((splits.get(name) or {}).get('rows'))
               or (splits.get(name) or {}).get('insufficient_data')]
    if missing:
        block('EVALUATION_COVERAGE_REQUIRED', 'Evaluation is incomplete: ' + ', '.join(missing) +
              ' has no usable rows. Collect independent evaluation history, then prepare and train a new candidate.')
    if model_type == 'threat_ranking_model' and any(
            not positive((splits.get(name) or {}).get(label)) for name in ('val', 'test') for label in ('positive', 'negative')):
        block('EVALUATION_CLASSES_REQUIRED', 'Validation and test sets each need reviewed positive and negative outcomes.')
    return out


async def training_readiness(db, model_type, *, labels=None):
    """Cheap source eligibility, not a replacement for immutable dataset validation.

    A saved compatible dataset can be reused when fresh source history is absent.
    Label governance remains required for supervised training in either case.
    """
    from config import settings
    spec = get_model_spec(model_type)
    blockers, counts = [], {}
    if spec.serving_mode == 'offline_regression':
        blockers.append({'code': 'ADVANCED_CONFIGURATION_REQUIRED', 'message': 'Choose an explicit numeric target and saved dataset in Advanced training options.', 'action': 'advanced'})
    elif model_type == 'threat_ranking_model':
        if labels is None:
            from backend.ml.labeling_service import labeling_service
            labels = await labeling_service.label_stats(db)
        counts = labels.get('counted_reviewed_manual') or {}
        if not labels['supervised_gate_open']:
            blockers.append({'code': 'INSUFFICIENT_REVIEWED_LABELS', 'message':
                f"Review outcomes: {counts.get('total', 0)}/{labels.get('required_total', 100)} total; "
                f"{counts.get('positive', 0)} positive and {counts.get('negative', 0)} negative "
                f"(at least {labels.get('required_per_class', 25)} each).", 'action': 'labels'})
    elif model_type in ('coappearance_anomaly_model', 'social_graph_anomaly_model'):
        now = datetime.utcnow()
        values = (await db.execute(text('''WITH edges AS (
            SELECT identity_id_1, identity_id_2, first_co_appearance, last_co_appearance
            FROM identity_relationships WHERE calculated_at <= :now
            AND identity_id_1 <> identity_id_2
            AND (last_co_appearance IS NULL OR last_co_appearance >= :floor)
        ), nodes AS (SELECT identity_id_1 AS id FROM edges UNION SELECT identity_id_2 FROM edges)
        SELECT count(*) AS edges, (SELECT count(*) FROM nodes) AS nodes,
        extract(epoch FROM (max(last_co_appearance)-min(first_co_appearance)))/86400 AS span_days FROM edges'''),
            {'now': now, 'floor': now - timedelta(days=90)})).mappings().one()
        counts = {'edges': values['edges'], 'nodes': values['nodes'], 'span_days': max(0.0, float(values['span_days'] or 0))}
        requirements = {'edges': 1} if model_type == 'coappearance_anomaly_model' else {
            'edges': int(settings.ML_GRAPH_MIN_EDGES), 'nodes': int(settings.ML_GRAPH_MIN_NODES),
            'span_days': int(settings.ML_GRAPH_MIN_OBSERVATION_DAYS)}
        for measure, required in requirements.items():
            if counts[measure] < required:
                blockers.append({'code': 'SOURCE_HISTORY_REQUIRED', 'message':
                    f"Collect more relationship history: {counts[measure]:g}/{required} {measure.replace('_', ' ')}.", 'action': 'collect'})
    else:
        count = (await db.execute(text('SELECT count(*) FROM identity_appearances WHERE start_time <= :now'),
                                 {'now': datetime.utcnow()})).scalar_one()
        counts = {'appearances': count}
        if not count:
            blockers.append({'code': 'SOURCE_HISTORY_REQUIRED', 'message': 'Add camera observations before preparing a new dataset.', 'action': 'collect'})
    return {'ready': not blockers, 'counts': counts, 'blockers': blockers,
            'scope': 'new_dataset', 'note': 'The worker checks dataset quality and evaluation splits after preparation.'}
