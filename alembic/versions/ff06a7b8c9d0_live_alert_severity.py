"""Add configurable severity to live alerts; preserve every rule and trigger."""
from alembic import op
import sqlalchemy as sa

revision = 'ff06a7b8c9d0'
down_revision = 'fee5f6a7b8c9'
branch_labels = depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column('live_search_alerts', sa.Column('alert_level', sa.String(16), nullable=False, server_default='warning'))
    op.create_check_constraint('ck_live_alert_severity', 'live_search_alerts', "alert_level IN ('info', 'warning', 'critical')")


def downgrade():
    op.drop_constraint('ck_live_alert_severity', 'live_search_alerts', type_='check')
    op.drop_column('live_search_alerts', 'alert_level')
