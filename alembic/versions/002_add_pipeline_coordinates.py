"""Add latitude longitude to pipelines

This migration adds:
1. latitude column (Float, nullable) - Latitude coordinate for camera location
2. longitude column (Float, nullable) - Longitude coordinate for camera location
3. location_name column (String, nullable) - Human-readable location name
4. Index on coordinates for faster spatial queries

Revision ID: 002_pipeline_coords
Revises: 001_pgvector
Create Date: 2026-01-05
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '002_pipeline_coords'
down_revision = '001_pgvector'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add latitude, longitude, and location_name columns to pipelines table.
    Also creates an index on coordinates for faster spatial queries.
    """
    # Add latitude column
    op.add_column(
        'pipelines',
        sa.Column('latitude', sa.Float(), nullable=True, comment='Latitude coordinate for camera location')
    )
    
    # Add longitude column
    op.add_column(
        'pipelines',
        sa.Column('longitude', sa.Float(), nullable=True, comment='Longitude coordinate for camera location')
    )
    
    # Add location_name column
    op.add_column(
        'pipelines',
        sa.Column('location_name', sa.String(255), nullable=True, comment='Human-readable location name (e.g., \'Main Entrance\', \'Parking Lot\')')
    )
    
    # Create index on coordinates for faster spatial queries
    op.create_index(
        'idx_pipeline_coordinates',
        'pipelines',
        ['latitude', 'longitude'],
        unique=False
    )
    
    print("✅ Added latitude, longitude, and location_name columns to pipelines table")
    print("💡 Update pipeline coordinates via Pipeline Management page or API to enable map visualization")


def downgrade() -> None:
    """Remove latitude, longitude, and location_name columns."""
    # Drop index
    op.drop_index('idx_pipeline_coordinates', table_name='pipelines')
    
    # Remove columns
    op.drop_column('pipelines', 'location_name')
    op.drop_column('pipelines', 'longitude')
    op.drop_column('pipelines', 'latitude')
    
    print("✅ Removed latitude, longitude, and location_name columns from pipelines table")

