"""Natural-key uniqueness for four tables that had none.

Every table below has a natural key that the schema never enforced, so a
retry, a race or a re-run could store the same fact twice. A surrogate primary
key does not prevent this — it guarantees the ROW is distinct, not the fact.

Measured before writing this (read-only count against a live database):

    risk_signal_results        93 rows   0 duplicate groups
    ml_shadow_comparisons     261 rows   1 duplicate group  (1 row to remove)
    identity_relationships    155 rows   0 duplicates, 0 reversed pairs
    similarity_training_data    0 rows   empty there

So on that database this is almost entirely "add index". The dedupe steps are
still written, and written first, because another deployment will have
different data and a CREATE UNIQUE INDEX that meets a duplicate fails the
whole migration.

Dedupe policy: keep the EARLIEST row by `created_at`, tie-broken by `id`,
and drop the later copies.

Deliberately not `min(id)`, which is the precedent in d4e5f6a7b8c9: three of
these four tables have UUID v4 primary keys, and a random UUID has no
chronological order, so `min(id)` there keeps an arbitrary row rather than the
first one. That is invisible when the duplicates are identical — and these are
not. The one duplicate measured had two rows 3.04 s apart with DIFFERENT
contents, so the choice decides which evaluation survives. Keeping the first
matches how the rest of this codebase resolves the same question: every
`ON CONFLICT DO NOTHING` upsert here lets the first write win.

WHY EACH ONE

ml_shadow_comparisons: one comparison per prediction. `ml_predictions` is
    protected by `idempotency_key`; its child was not, so two shadow
    evaluations of the same prediction could both persist. The one duplicate
    found was exactly that — two rows 3.04 s apart with different contents,
    i.e. a genuine race, not a replayed identical write.

risk_signal_results: the aggregation table for per-signal analytics. Its
    parent guards retries (children are written only when the parent's
    ON CONFLICT actually inserted), so the exposure is narrower than it looks:
    two signals colliding INSIDE one assessment. `signal_name` is truncated to
    64 characters by the writer, which can manufacture that collision from two
    distinct long names. Duplicates here silently double every aggregate.

identity_relationships: already unique on the ORDERED pair, but nothing
    enforced the ordering, so (A,B) and (B,A) could both exist and the unique
    index would not see them as the same relationship. The model comment
    claimed the ordering was "ensured" when only application convention did
    it. A CHECK makes the claim true.

similarity_training_data: an association table with neither a unique pair nor
    FK delete rules, feeding an ML training set — duplicate pairs double-weight
    a training example. Ordering must be normalised BEFORE deduping or the
    swap silently pairs each identity with the other one's quality score.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b4d5e6f7a8c9"
down_revision: Union[str, None] = "a3c8e5f1b7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------------------------------------------------------------------
    # 1. ml_shadow_comparisons — one comparison per prediction
    # ---------------------------------------------------------------------
    op.execute("""
        DELETE FROM ml_shadow_comparisons t
        USING (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY prediction_id ORDER BY created_at, id
                ) AS rn
                FROM ml_shadow_comparisons
            ) ranked WHERE rn > 1
        ) later
        WHERE t.id = later.id
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ml_shadow_comparison_prediction
        ON ml_shadow_comparisons (prediction_id)
    """)

    # ---------------------------------------------------------------------
    # 2. risk_signal_results — one result per signal per assessment
    # ---------------------------------------------------------------------
    op.execute("""
        DELETE FROM risk_signal_results t
        USING (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY assessment_id, signal_name ORDER BY created_at, id
                ) AS rn
                FROM risk_signal_results
            ) ranked WHERE rn > 1
        ) later
        WHERE t.id = later.id
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_risk_signal_assessment_name
        ON risk_signal_results (assessment_id, signal_name)
    """)

    # ---------------------------------------------------------------------
    # 3. identity_relationships — make the ordering convention real
    #
    # The unique index on the ordered pair already exists, so a reversed row
    # cannot simply be flipped in place: flipping (B,A) when (A,B) is present
    # would violate it mid-statement. Drop the colliding reversed row first,
    # keeping the correctly-ordered one, and only then normalise what is left.
    # ---------------------------------------------------------------------
    op.execute("""
        DELETE FROM identity_relationships r
        WHERE r.identity_id_1 > r.identity_id_2
          AND EXISTS (
              SELECT 1 FROM identity_relationships k
              WHERE k.identity_id_1 = r.identity_id_2
                AND k.identity_id_2 = r.identity_id_1
          )
    """)
    op.execute("""
        UPDATE identity_relationships
        SET identity_id_1 = identity_id_2,
            identity_id_2 = identity_id_1
        WHERE identity_id_1 > identity_id_2
    """)
    op.execute("""
        ALTER TABLE identity_relationships
        ADD CONSTRAINT ck_identity_relationship_ordered
        CHECK (identity_id_1 < identity_id_2)
    """)

    # ---------------------------------------------------------------------
    # 4. similarity_training_data — normalise, then dedupe, then constrain
    #
    # ORDER MATTERS. quality_score_1/quality_score_2 are positional: they
    # belong to identity_id_1/identity_id_2 respectively. Swapping the ids
    # without swapping the scores would attach each identity to the other
    # one's score — a silent corruption of the training set, which is worse
    # than the duplicate this migration exists to remove.
    # ---------------------------------------------------------------------
    op.execute("""
        UPDATE similarity_training_data
        SET identity_id_1 = identity_id_2,
            identity_id_2 = identity_id_1,
            quality_score_1 = quality_score_2,
            quality_score_2 = quality_score_1
        WHERE identity_id_1 IS NOT NULL
          AND identity_id_2 IS NOT NULL
          AND identity_id_1 > identity_id_2
    """)
    op.execute("""
        DELETE FROM similarity_training_data t
        USING (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY identity_id_1, identity_id_2 ORDER BY created_at, id
                ) AS rn
                FROM similarity_training_data
                WHERE identity_id_1 IS NOT NULL AND identity_id_2 IS NOT NULL
            ) ranked WHERE rn > 1
        ) later
        WHERE t.id = later.id
    """)
    # Partial: rows with a NULL member are not a pair and must not collide
    # with each other (many NULLs would otherwise be "equal" under the index).
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_similarity_training_pair
        ON similarity_training_data (identity_id_1, identity_id_2)
        WHERE identity_id_1 IS NOT NULL AND identity_id_2 IS NOT NULL
    """)
    op.execute("""
        ALTER TABLE similarity_training_data
        ADD CONSTRAINT ck_similarity_training_ordered
        CHECK (identity_id_1 IS NULL OR identity_id_2 IS NULL
               OR identity_id_1 < identity_id_2)
    """)


def downgrade() -> None:
    # Only the constraints are reversible. The rows removed above were
    # duplicates of rows that remain; they are not restored, and nothing reads
    # them. The ordering normalisation is likewise not un-swapped: the
    # normalised form is correct under either schema.
    op.execute("ALTER TABLE similarity_training_data "
               "DROP CONSTRAINT IF EXISTS ck_similarity_training_ordered")
    op.execute("DROP INDEX IF EXISTS uq_similarity_training_pair")
    op.execute("ALTER TABLE identity_relationships "
               "DROP CONSTRAINT IF EXISTS ck_identity_relationship_ordered")
    op.execute("DROP INDEX IF EXISTS uq_risk_signal_assessment_name")
    op.execute("DROP INDEX IF EXISTS uq_ml_shadow_comparison_prediction")
