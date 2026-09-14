"""Persist exact sighting provenance without replacing historical appearances."""
from alembic import op
import sqlalchemy as sa

revision = 'fdd4e5f6a7b8'
down_revision = 'fcc3d4e5f6a7'
branch_labels = depends_on = None


def upgrade():
    for name, type_ in [('event_id', sa.String(64)), ('detection_id', sa.Integer()),
                        ('detection_uuid', sa.String(36)), ('location_name', sa.String(255)),
                        ('timestamp_source', sa.String(32))]:
        op.add_column('identity_appearances', sa.Column(name, type_, nullable=True))
    op.create_unique_constraint('uq_appearance_event_id', 'identity_appearances', ['event_id'])
    op.create_foreign_key('fk_appearance_detection', 'identity_appearances', 'detections',
                         ['detection_id'], ['id'], ondelete='SET NULL')
    op.create_index('idx_appearance_camera_latest', 'identity_appearances',
                    ['identity_id', 'pipeline_id', 'start_time', 'id'])
    # Every legacy appearance keeps its own ID/time, even when the original
    # detection has expired. Only link an exact, unambiguous surviving match.
    op.execute("UPDATE identity_appearances SET event_id = 'appearance:' || id, timestamp_source = 'legacy_server'")
    op.execute("""
        WITH matches AS (
            SELECT a.id, min(d.id) AS detection_id
            FROM identity_appearances a
            JOIN faces f ON f.identity_id = a.identity_id
            JOIN detections d ON d.id = f.detection_id
                AND d.pipeline_id = a.pipeline_id AND d.timestamp = a.start_time
            GROUP BY a.id HAVING count(DISTINCT d.id) = 1
        )
        UPDATE identity_appearances a SET detection_id = d.id, detection_uuid = d.uuid
        FROM matches m JOIN detections d ON d.id = m.detection_id WHERE a.id = m.id
    """)


def downgrade():
    op.drop_index('idx_appearance_camera_latest', table_name='identity_appearances')
    op.drop_constraint('fk_appearance_detection', 'identity_appearances', type_='foreignkey')
    op.drop_constraint('uq_appearance_event_id', 'identity_appearances', type_='unique')
    for name in ['timestamp_source', 'location_name', 'detection_uuid', 'detection_id', 'event_id']:
        op.drop_column('identity_appearances', name)
