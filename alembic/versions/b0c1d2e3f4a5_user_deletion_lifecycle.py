"""User deletion lifecycle: preserve history, cascade account-bound state

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-08-14

Deleting a user was impossible. `UserService.delete_user()` handled 2 of the 26
FK columns referencing users.id; PostgreSQL refused everything else, observed
live as:

    update or delete on table "users" violates foreign key constraint
    "chatbot_audit_log_user_id_fkey" on table "chatbot_audit_log"

Worse, three CASCADE rules pointed at data that must OUTLIVE the account —
conversations, user_query_history, user_query_embeddings — so the one deletion
path that did not error would have destroyed the user's chat history.

This revision makes the schema express the actual ownership semantics:

  CASCADE (account-bound, dies with the account)
      workspace_members, user_conversation_sessions, user_conversation_memory,
      message_feedback, pending_enrollments            (already CASCADE)
      user_pipeline_access                             (NO ACTION -> CASCADE)

  SET NULL (history, outlives the account)
      conversations, user_query_history, user_query_embeddings,
      chatbot_audit_log, identity_audit_log, search_history,
      identity_merges.merged_by, live_search_alerts.created_by
                                                       (CASCADE/NO ACTION -> SET NULL,
                                                        NOT NULL dropped where needed)
      merge_suggestions.reviewed_by, settings_audit_log.changed_by_user_id,
      similarity_training_data.created_by_user_id,
      similarity_model_registry.created_by, watchlists.created_by,
      watchlist_entries.added_by, watchlist_alerts.acknowledged_by,
      live_alert_triggers.acknowledged_by              (NO ACTION -> SET NULL,
                                                        already nullable)

Attribution survives three ways, because each alone is insufficient:
  * denormalized usernames (already present on the audit tables);
  * historical_* integer columns, backfilled here for existing rows and
    populated at deletion time thereafter — usernames cannot recover a numeric
    id, since users.username is unique only among LIVE rows and can be
    re-registered;
  * a deleted_users tombstone (id -> username, deliberately nothing more:
    no email, no hash, no role — traceability, not a recoverable account).

identity_audit_log.user_id becoming nullable does NOT retire the `system`
principal (a3b4c5d6e7f8). `system` remains the actor for MACHINE actions;
NULL means "a deleted human". Collapsing the two would make an automated 3am
rebuild indistinguishable from a departed employee's action.

Constraint names are resolved from pg_constraint at runtime rather than assumed:
several of these tables were first created by Base.metadata.create_all(), not
Alembic, so the default `<table>_<column>_fkey` naming is likely but not
guaranteed in every environment.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b0c1d2e3f4a5'
down_revision = 'a9b0c1d2e3f4'
branch_labels = None
depends_on = None


# (table, column, new_rule) — the delete-rule rewrites.
FK_RULES = [
    # account-bound: dies with the account
    ('user_pipeline_access', 'user_id', 'CASCADE'),
    # history: outlives the account
    ('conversations', 'user_id', 'SET NULL'),
    ('user_query_history', 'user_id', 'SET NULL'),
    ('user_query_embeddings', 'user_id', 'SET NULL'),
    ('chatbot_audit_log', 'user_id', 'SET NULL'),
    ('identity_audit_log', 'user_id', 'SET NULL'),
    ('search_history', 'user_id', 'SET NULL'),
    ('identity_merges', 'merged_by', 'SET NULL'),
    ('live_search_alerts', 'created_by', 'SET NULL'),
    ('merge_suggestions', 'reviewed_by', 'SET NULL'),
    ('settings_audit_log', 'changed_by_user_id', 'SET NULL'),
    ('similarity_training_data', 'created_by_user_id', 'SET NULL'),
    ('similarity_model_registry', 'created_by', 'SET NULL'),
    ('watchlists', 'created_by', 'SET NULL'),
    ('watchlist_entries', 'added_by', 'SET NULL'),
    ('watchlist_alerts', 'acknowledged_by', 'SET NULL'),
    ('live_alert_triggers', 'acknowledged_by', 'SET NULL'),
]

# Columns that must become nullable for SET NULL to be possible.
DROP_NOT_NULL = [
    ('conversations', 'user_id'),
    ('user_query_history', 'user_id'),
    ('user_query_embeddings', 'user_id'),
    ('chatbot_audit_log', 'user_id'),
    ('identity_audit_log', 'user_id'),
    ('search_history', 'user_id'),
    ('identity_merges', 'merged_by'),
    ('live_search_alerts', 'created_by'),
]

# (table, historical column, source column) — numeric attribution that survives
# deletion. Backfilled from the live FK so EXISTING rows are attributable before
# any deletion ever happens.
HISTORICAL = [
    ('conversations', 'historical_user_id', 'user_id'),
    ('user_query_history', 'historical_user_id', 'user_id'),
    ('search_history', 'historical_user_id', 'user_id'),
    ('chatbot_audit_log', 'historical_user_id', 'user_id'),
    ('identity_audit_log', 'historical_user_id', 'user_id'),
    ('identity_merges', 'historical_merged_by', 'merged_by'),
    ('live_search_alerts', 'historical_created_by', 'created_by'),
]


def _fk_name(bind, table: str, column: str):
    """The actual FK constraint name on (table.column) -> users.id, or None."""
    return bind.execute(sa.text("""
        SELECT conname FROM pg_constraint
        WHERE contype = 'f'
          AND confrelid = 'users'::regclass
          AND conrelid = CAST(:t AS regclass)
          AND (SELECT array_agg(attname) FROM pg_attribute
               WHERE attrelid = conrelid AND attnum = ANY(conkey)) = ARRAY[CAST(:c AS name)]
    """), {"t": table, "c": column}).scalar()


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Nullability first: SET NULL against a NOT NULL column is a constraint
    #    violation at delete time, not at DDL time — it must go before the rules.
    for table, column in DROP_NOT_NULL:
        op.execute(f'ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL')

    # 2. Delete-rule rewrites, resolving each real constraint name.
    for table, column, rule in FK_RULES:
        name = _fk_name(bind, table, column)
        if name:
            op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}')
        else:
            # No FK at all (should not happen for these columns) — create under
            # the default naming so downgrade can find it.
            name = f'{table}_{column}_fkey'
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT {name} '
            f'FOREIGN KEY ({column}) REFERENCES users (id) ON DELETE {rule}'
        )

    # 3. Attribution columns, backfilled for existing rows.
    op.execute("""
        ALTER TABLE conversations
        ADD COLUMN IF NOT EXISTS author_username VARCHAR(100) NULL
    """)
    for table, hist_col, src_col in HISTORICAL:
        op.execute(
            f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {hist_col} INTEGER NULL')
        op.execute(
            f'UPDATE {table} SET {hist_col} = {src_col} WHERE {hist_col} IS NULL')
    op.execute("""
        UPDATE conversations c SET author_username = u.username
        FROM users u WHERE u.id = c.user_id AND c.author_username IS NULL
    """)

    # 4. The tombstone.
    op.execute("""
        CREATE TABLE IF NOT EXISTS deleted_users (
            user_id             INTEGER PRIMARY KEY,
            username            VARCHAR(100) NOT NULL,
            deleted_at          TIMESTAMP NOT NULL DEFAULT NOW(),
            deleted_by_user_id  INTEGER NULL,
            deleted_by_username VARCHAR(100) NULL
        )
    """)
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_deleted_users_username ON deleted_users (username)')
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_deleted_users_deleted_at ON deleted_users (deleted_at)')

    # Same convention as every other table holding user data: the read-only
    # reporting role must not see it (d3e4f5a6b7c8, c1d2e3f4a5b6).
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fr_readonly') THEN
                REVOKE ALL ON deleted_users FROM fr_readonly;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    """Reverse the schema shape — but never at the cost of history.

    NOT NULL is restored only where no NULLs exist. If any preserved row
    already references a deleted user, restoring NOT NULL would require
    destroying or falsifying that row; the downgrade refuses instead. Same rule
    as a3b4c5d6e7f8: a downgrade may undo a schema change, never an audit trail.
    """
    bind = op.get_bind()

    for table, column in DROP_NOT_NULL:
        nulls = bind.execute(sa.text(
            f'SELECT COUNT(*) FROM {table} WHERE {column} IS NULL')).scalar()
        if nulls:
            raise RuntimeError(
                f"cannot restore NOT NULL on {table}.{column}: {nulls} row(s) "
                f"reference deleted users. History is not destroyed by a "
                f"downgrade; resolve those rows deliberately first.")

    # Restore the pre-revision delete rules.
    previous = {
        ('user_pipeline_access', 'user_id'): 'NO ACTION',
        ('conversations', 'user_id'): 'CASCADE',
        ('user_query_history', 'user_id'): 'CASCADE',
        ('user_query_embeddings', 'user_id'): 'CASCADE',
        ('chatbot_audit_log', 'user_id'): 'NO ACTION',
        ('identity_audit_log', 'user_id'): 'NO ACTION',
        ('search_history', 'user_id'): 'NO ACTION',
        ('identity_merges', 'merged_by'): 'NO ACTION',
        ('live_search_alerts', 'created_by'): 'NO ACTION',
        ('merge_suggestions', 'reviewed_by'): 'NO ACTION',
        ('settings_audit_log', 'changed_by_user_id'): 'NO ACTION',
        ('similarity_training_data', 'created_by_user_id'): 'NO ACTION',
        ('similarity_model_registry', 'created_by'): 'NO ACTION',
        ('watchlists', 'created_by'): 'NO ACTION',
        ('watchlist_entries', 'added_by'): 'NO ACTION',
        ('watchlist_alerts', 'acknowledged_by'): 'NO ACTION',
        ('live_alert_triggers', 'acknowledged_by'): 'NO ACTION',
    }
    for table, column, _rule in FK_RULES:
        name = _fk_name(bind, table, column) or f'{table}_{column}_fkey'
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}')
        rule = previous[(table, column)]
        suffix = '' if rule == 'NO ACTION' else f' ON DELETE {rule}'
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT {name} '
            f'FOREIGN KEY ({column}) REFERENCES users (id){suffix}'
        )

    for table, column in DROP_NOT_NULL:
        op.execute(f'ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL')

    # The attribution columns and the tombstone carry historical record and are
    # deliberately kept — same reasoning as the guard above. Downgrading the
    # schema must not delete the only remaining attribution for past deletions.
