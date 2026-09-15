"""Add operational integrity guards without replacing API JSON or historical data.

Private membership tables are derived from existing JSON by triggers. Ownership
is checked at transaction end so merge consolidation may reparent in either order.
Upgrade refuses inconsistent existing data; it never silently repairs/deletes it.
"""
from alembic import op

revision = 'fee5f6a7b8c9'
down_revision = 'fdd4e5f6a7b8'
branch_labels = depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM identity_embeddings e JOIN identity_images i ON i.id=e.image_id
                   WHERE e.identity_id <> i.identity_id) THEN
          RAISE EXCEPTION 'Image/embedding owners disagree; review existing rows before upgrading';
        END IF;
      END $$;
      CREATE FUNCTION vas_valid_alert_days(days jsonb) RETURNS boolean
      LANGUAGE sql IMMUTABLE AS $$
        SELECT CASE WHEN days IS NULL OR days = 'null'::jsonb THEN true
          WHEN jsonb_typeof(days) <> 'array' THEN false
          ELSE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(days) d WHERE d::text !~ '^[0-6]$') END
      $$;
      ALTER TABLE live_search_alerts ADD CONSTRAINT ck_live_alert_days
        CHECK (vas_valid_alert_days(active_days));

      CREATE TABLE live_alert_pipeline_links (
        alert_id uuid NOT NULL REFERENCES live_search_alerts(id) ON DELETE CASCADE,
        pipeline_id varchar(255) NOT NULL REFERENCES pipelines(pipeline_id) ON DELETE RESTRICT,
        PRIMARY KEY (alert_id, pipeline_id)
      );
      CREATE INDEX ix_live_alert_pipeline_links_camera ON live_alert_pipeline_links(pipeline_id);
      CREATE FUNCTION vas_sync_alert_pipelines() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.pipeline_ids IS NOT NULL AND NEW.pipeline_ids <> 'null'::jsonb THEN
          IF jsonb_typeof(NEW.pipeline_ids) <> 'array' THEN
            RAISE EXCEPTION 'Camera IDs must be an array' USING ERRCODE='23514';
          END IF;
          IF EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.pipeline_ids) p
                     WHERE jsonb_typeof(p) <> 'string' OR p = '\"\"'::jsonb) THEN
            RAISE EXCEPTION 'Camera IDs must be nonempty strings' USING ERRCODE='23514';
          END IF;
        END IF;
        DELETE FROM live_alert_pipeline_links WHERE alert_id=NEW.id;
        INSERT INTO live_alert_pipeline_links(alert_id,pipeline_id)
          SELECT NEW.id, p FROM jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(NEW.pipeline_ids)='array' THEN NEW.pipeline_ids ELSE '[]'::jsonb END) p
          ON CONFLICT DO NOTHING;
        RETURN NEW;
      END $$;
      CREATE TRIGGER trg_sync_alert_pipelines AFTER INSERT OR UPDATE OF pipeline_ids ON live_search_alerts
        FOR EACH ROW EXECUTE FUNCTION vas_sync_alert_pipelines();
      -- Fires the same validation/sync path; changes no JSON values.
      UPDATE live_search_alerts SET pipeline_ids=pipeline_ids;

      CREATE FUNCTION vas_lock_embedding_image() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.image_id IS NOT NULL THEN
          -- SHARE conflicts with image owner changes, unlike FK KEY SHARE alone.
          PERFORM 1 FROM identity_images WHERE id=NEW.image_id FOR SHARE;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER trg_lock_embedding_image BEFORE INSERT OR UPDATE OF image_id,identity_id ON identity_embeddings
        FOR EACH ROW EXECUTE FUNCTION vas_lock_embedding_image();
      CREATE FUNCTION vas_check_embedding_owner() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF EXISTS (SELECT 1 FROM identity_embeddings e JOIN identity_images i ON i.id=e.image_id
                   WHERE e.id=NEW.id AND e.identity_id<>i.identity_id) THEN
          RAISE EXCEPTION 'Embedding and image must belong to the same identity' USING ERRCODE='23514';
        END IF;
        RETURN NULL;
      END $$;
      CREATE CONSTRAINT TRIGGER ck_embedding_image_owner
        AFTER INSERT OR UPDATE OF image_id,identity_id ON identity_embeddings
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION vas_check_embedding_owner();
      CREATE FUNCTION vas_check_image_owner() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF EXISTS (SELECT 1 FROM identity_embeddings e JOIN identity_images i ON i.id=e.image_id
                   WHERE i.id=NEW.id AND e.identity_id<>i.identity_id) THEN
          RAISE EXCEPTION 'Image and embeddings must belong to the same identity' USING ERRCODE='23514';
        END IF;
        RETURN NULL;
      END $$;
      CREATE CONSTRAINT TRIGGER ck_image_embedding_owner AFTER UPDATE OF identity_id ON identity_images
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION vas_check_image_owner();

      CREATE TABLE pending_merge_members (
        suggestion_id integer NOT NULL REFERENCES merge_suggestions(id) ON DELETE CASCADE,
        identity_id uuid NOT NULL REFERENCES identities(id) ON DELETE RESTRICT,
        PRIMARY KEY (suggestion_id,identity_id)
      );
      CREATE INDEX ix_pending_merge_members_identity ON pending_merge_members(identity_id);
      CREATE FUNCTION vas_sync_pending_merge_members() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE member_id uuid;
      BEGIN
        DELETE FROM pending_merge_members WHERE suggestion_id=NEW.id;
        -- Completed/rejected/invalidated suggestions keep their historical JSON.
        IF NEW.status::text <> 'PENDING' THEN RETURN NEW; END IF;
        IF jsonb_typeof(NEW.identity_ids) IS DISTINCT FROM 'array' THEN
          RAISE EXCEPTION 'Pending suggestion members must be an array' USING ERRCODE='23514';
        END IF;
        IF jsonb_array_length(NEW.identity_ids)<2 THEN
          RAISE EXCEPTION 'Pending suggestions require at least two identities' USING ERRCODE='23514';
        END IF;
        FOR member_id IN SELECT p::uuid FROM jsonb_array_elements_text(NEW.identity_ids) p ORDER BY p LOOP
          PERFORM 1 FROM identities WHERE id=member_id AND status::text IN ('ACTIVE','PROMOTED') FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Pending suggestion contains a missing or inactive identity' USING ERRCODE='23503';
          END IF;
          INSERT INTO pending_merge_members(suggestion_id,identity_id) VALUES(NEW.id,member_id);
        END LOOP;
        RETURN NEW;
      END $$;
      CREATE TRIGGER trg_sync_pending_merge_members AFTER INSERT OR UPDATE OF identity_ids,status ON merge_suggestions
        FOR EACH ROW EXECUTE FUNCTION vas_sync_pending_merge_members();
      UPDATE merge_suggestions SET identity_ids=identity_ids WHERE status::text='PENDING';
      CREATE FUNCTION vas_invalidate_pending_merge_members() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='UPDATE' THEN
          IF NEW.status::text IN ('ACTIVE','PROMOTED') THEN RETURN NEW; END IF;
        END IF;
        UPDATE merge_suggestions SET status='INVALIDATED',
          invalidated_reason='Member deleted or no longer actionable', invalidated_at=now()
          WHERE id IN (SELECT suggestion_id FROM pending_merge_members WHERE identity_id=OLD.id)
            AND status::text='PENDING';
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER trg_invalidate_merge_on_delete BEFORE DELETE ON identities
        FOR EACH ROW EXECUTE FUNCTION vas_invalidate_pending_merge_members();
      CREATE TRIGGER trg_invalidate_merge_on_status AFTER UPDATE OF status ON identities
        FOR EACH ROW EXECUTE FUNCTION vas_invalidate_pending_merge_members();
    """)


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
      DROP TRIGGER trg_invalidate_merge_on_status ON identities;
      DROP TRIGGER trg_invalidate_merge_on_delete ON identities;
      DROP TRIGGER trg_sync_pending_merge_members ON merge_suggestions;
      DROP FUNCTION vas_invalidate_pending_merge_members();
      DROP FUNCTION vas_sync_pending_merge_members();
      DROP TABLE pending_merge_members;
      DROP TRIGGER ck_image_embedding_owner ON identity_images;
      DROP TRIGGER ck_embedding_image_owner ON identity_embeddings;
      DROP TRIGGER trg_lock_embedding_image ON identity_embeddings;
      DROP FUNCTION vas_check_image_owner();
      DROP FUNCTION vas_check_embedding_owner();
      DROP FUNCTION vas_lock_embedding_image();
      DROP TRIGGER trg_sync_alert_pipelines ON live_search_alerts;
      DROP FUNCTION vas_sync_alert_pipelines();
      DROP TABLE live_alert_pipeline_links;
      ALTER TABLE live_search_alerts DROP CONSTRAINT ck_live_alert_days;
      DROP FUNCTION vas_valid_alert_days(jsonb);
    """)
