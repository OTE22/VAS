"""Prediction lineage is history: a model / threshold set with prediction
history is archived or retired, never hard-deleted.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0

`ml_predictions.model_id`, `ml_predictions.threshold_id` and
`ml_shadow_comparisons.model_id` were ON DELETE SET NULL — deleting a model
silently turned every successful shadow prediction it produced into a
lineage-less success row, exactly the class d4e5f6a7b8c9 forbids at write
time. The rule becomes ON DELETE RESTRICT: the lifecycle path for a model is
stage transition (archived / rejected / failed) and for a threshold set is
`retired`; hard deletion is only possible once the prediction history that
names it is gone (demo cleanup deletes the predictions first, on purpose).

Rule change only — no rows are read or written; no precondition can fail.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None

_FKS = (
    ("ml_predictions", "model_id", "ml_models", "id", "ml_predictions_model_id_fkey"),
    ("ml_predictions", "threshold_id", "ml_model_thresholds", "id", "ml_predictions_threshold_id_fkey"),
    ("ml_shadow_comparisons", "model_id", "ml_models", "id", "ml_shadow_comparisons_model_id_fkey"),
)


def _set_rule(rule: str) -> None:
    for table, column, ref_table, ref_column, name in _FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({column}) "
                   f"REFERENCES {ref_table} ({ref_column}) ON DELETE {rule}")


def upgrade() -> None:
    _set_rule("RESTRICT")


def downgrade() -> None:
    _set_rule("SET NULL")
