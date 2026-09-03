"""Relational ML feature contracts.

Revision ID: fbb2c3d4e5f6
Revises: faa1b2c3d4e5
Create Date: 2026-09-03
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "fbb2c3d4e5f6"
down_revision = "faa1b2c3d4e5"
branch_labels = None
depends_on = None


FEATURES = (
    ("pair_co_appearance_count_90d", "pair", "90d", "pair_relationship_count", "Cached coappearances observed for the canonical pair"),
    ("pair_co_appearance_rate_per_day", "pair", "90d", "pair_relationship_rate", "Coappearances divided by observed relationship days"),
    ("pair_common_pipeline_count", "pair", "90d", "pair_common_pipelines", "Distinct cameras shared by the pair"),
    ("pair_co_appearance_percentage", "pair", "90d", "pair_percentage", "Bounded cached coappearance percentage divided by 100"),
    ("pair_relationship_span_days", "pair", "90d", "pair_span_days", "Days between first and last coappearance"),
    ("pair_days_since_last_coappearance", "pair", "90d", "pair_recency_days", "Days from the most recent coappearance to snapshot time"),
    ("graph_degree_centrality_90d", "person", "90d", "graph_degree_centrality_v1", "Degree divided by the number of other observed graph nodes"),
    ("graph_weighted_degree_log_90d", "person", "90d", "graph_weighted_degree_log_v1", "log1p of total coappearance edge weight"),
    ("graph_pagerank_90d", "person", "90d", "graph_pagerank_v1", "Weighted PageRank on the readiness-qualified graph"),
    ("graph_clustering_coefficient_90d", "person", "90d", "graph_clustering_v1", "Fraction of possible links present among immediate neighbours"),
    ("graph_bridge_ratio_90d", "person", "90d", "graph_bridge_ratio_v1", "Share of neighbour pairs not directly linked; high values indicate bridging"),
    ("graph_mean_edge_weight_90d", "person", "90d", "graph_mean_edge_weight_v1", "Mean coappearance count across incident edges"),
)


def upgrade():
    table = sa.table(
        "ml_feature_definitions",
        sa.column("id", postgresql.UUID(as_uuid=True)), sa.column("name", sa.String),
        sa.column("version", sa.Integer), sa.column("entity_type", sa.String),
        sa.column("value_type", sa.String), sa.column("window", sa.String),
        sa.column("source", sa.String), sa.column("computation", sa.String),
        sa.column("params", postgresql.JSONB), sa.column("leakage_class", sa.String),
        sa.column("readiness_requirements", postgresql.JSONB),
        sa.column("description", sa.Text), sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )
    rows = []
    for name, entity_type, window, computation, description in FEATURES:
        requirements = None
        if name == "graph_degree_centrality_90d":
            requirements = {"min_nodes": "ML_GRAPH_MIN_NODES", "min_edges": "ML_GRAPH_MIN_EDGES",
                            "min_observation_days": "ML_GRAPH_MIN_OBSERVATION_DAYS"}
        rows.append({"id": uuid.uuid4(), "name": name, "version": 1,
                     "entity_type": entity_type, "value_type": "float", "window": window,
                     "source": "identity_relationships", "computation": computation,
                     "params": {}, "leakage_class": "safe",
                     "readiness_requirements": requirements,
                     "description": description, "is_active": True,
                     "created_at": datetime.utcnow()})
    op.bulk_insert(table, rows)


def downgrade():
    names = ", ".join("'%s'" % name for name, *_ in FEATURES)
    op.execute(f"DELETE FROM ml_feature_definitions WHERE version = 1 AND name IN ({names})")
