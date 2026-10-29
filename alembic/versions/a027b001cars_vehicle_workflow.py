"""Vehicle references, immutable sightings, durable processing and reviewed links."""
from alembic import op

revision = "a027b001cars"
down_revision = "ff17b8c9d0e1"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("\nCREATE TABLE known_vehicles (\n\tid UUID NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\tplate VARCHAR(64) NOT NULL, \n\tplate_key VARCHAR(64) NOT NULL, \n\tcountry VARCHAR(8) NOT NULL, \n\tmake VARCHAR(80), \n\tmodel VARCHAR(80), \n\tcolor VARCHAR(60), \n\tyear INTEGER, \n\tnotes TEXT, \n\tphoto_path VARCHAR(512), \n\tarchived BOOLEAN DEFAULT 'false' NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n")
    op.execute('CREATE UNIQUE INDEX uq_known_vehicle_plate ON known_vehicles (country, plate_key) WHERE NOT archived')
    op.execute("\nCREATE TABLE vehicle_pipelines (\n\tpipeline_id VARCHAR(255) NOT NULL, \n\tenabled BOOLEAN DEFAULT 'false' NOT NULL, \n\tocr_profile VARCHAR(12) DEFAULT 'en' NOT NULL, \n\tcountry VARCHAR(8) DEFAULT 'LB' NOT NULL, \n\tupdated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (pipeline_id), \n\tCONSTRAINT ck_vehicle_ocr_profile CHECK (ocr_profile IN ('ar','en','latin')), \n\tFOREIGN KEY(pipeline_id) REFERENCES pipelines (pipeline_id) ON DELETE RESTRICT\n)\n\n")
    op.execute("\nCREATE TABLE vehicle_sightings (\n\tid UUID NOT NULL, \n\tpipeline_id VARCHAR(255) NOT NULL, \n\tevent_id VARCHAR(128) NOT NULL, \n\tdetection_index INTEGER NOT NULL, \n\tcaptured_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\treceived_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tvehicle_type VARCHAR(32) NOT NULL, \n\ttrack_id VARCHAR(128), \n\tbbox JSONB NOT NULL, \n\tdetection_score FLOAT NOT NULL, \n\tscene_path VARCHAR(512) NOT NULL, \n\tvehicle_path VARCHAR(512) NOT NULL, \n\tplate_path VARCHAR(512), \n\tcountry VARCHAR(8) NOT NULL, \n\tocr_profile VARCHAR(12) NOT NULL, \n\tocr_result JSONB, \n\tplate VARCHAR(64), \n\tplate_key VARCHAR(64), \n\tplate_score FLOAT, \n\treview_state VARCHAR(24) DEFAULT 'pending' NOT NULL, \n\tknown_vehicle_id UUID, \n\trejected_vehicle_ids JSONB DEFAULT '[]' NOT NULL, \n\tembedding VECTOR(256), \n\tembedding_version VARCHAR(64), \n\tprocessing_state VARCHAR(24) DEFAULT 'pending' NOT NULL, \n\tattempts INTEGER DEFAULT '0' NOT NULL, \n\tlease_until TIMESTAMP WITHOUT TIME ZONE, \n\tnext_attempt_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\terror_code VARCHAR(80), \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_vehicle_event_detection UNIQUE (pipeline_id, event_id, detection_index), \n\tCONSTRAINT ck_vehicle_processing CHECK (processing_state IN ('pending','processing','done','failed')), \n\tCONSTRAINT ck_vehicle_review CHECK (review_state IN ('pending','confirmed','rejected','unreadable')), \n\tFOREIGN KEY(pipeline_id) REFERENCES pipelines (pipeline_id) ON DELETE RESTRICT, \n\tFOREIGN KEY(known_vehicle_id) REFERENCES known_vehicles (id) ON DELETE RESTRICT\n)\n\n")
    op.execute('CREATE INDEX ix_vehicle_capture_time ON vehicle_sightings (captured_at)')
    op.execute('CREATE INDEX ix_vehicle_pipeline_time ON vehicle_sightings (pipeline_id, captured_at)')
    op.execute('CREATE INDEX ix_vehicle_processing ON vehicle_sightings (processing_state, next_attempt_at)')
    op.execute('CREATE INDEX ix_vehicle_sightings_plate_key ON vehicle_sightings (plate_key)')
    op.execute("\nCREATE TABLE vehicle_person_links (\n\tid UUID NOT NULL, \n\tvehicle_id UUID NOT NULL, \n\tidentity_id UUID, \n\tidentity_name VARCHAR(255) NOT NULL, \n\thistorical_identity_id UUID NOT NULL, \n\trelationship_type VARCHAR(24) NOT NULL, \n\tevidence TEXT NOT NULL, \n\tactive BOOLEAN DEFAULT 'true' NOT NULL, \n\treviewed_by INTEGER, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_vehicle_person_relation CHECK (relationship_type IN ('registered_owner','driver','associated')), \n\tFOREIGN KEY(vehicle_id) REFERENCES known_vehicles (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(identity_id) REFERENCES identities (id) ON DELETE SET NULL, \n\tFOREIGN KEY(reviewed_by) REFERENCES users (id) ON DELETE SET NULL\n)\n\n")
    op.execute('CREATE INDEX ix_vehicle_person_vehicle ON vehicle_person_links (vehicle_id)')
    op.execute('\nCREATE TABLE vehicle_audit (\n\tid SERIAL NOT NULL, \n\tentity_type VARCHAR(24) NOT NULL, \n\tentity_id VARCHAR(255) NOT NULL, \n\taction VARCHAR(40) NOT NULL, \n\tactor_id INTEGER, \n\tdetails JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(actor_id) REFERENCES users (id) ON DELETE SET NULL\n)\n\n')
    op.execute('CREATE INDEX ix_vehicle_audit_entity ON vehicle_audit (entity_type, entity_id)')

def downgrade():
    op.drop_table('vehicle_audit')
    op.drop_table('vehicle_person_links')
    op.drop_table('vehicle_sightings')
    op.drop_table('vehicle_pipelines')
    op.drop_table('known_vehicles')
