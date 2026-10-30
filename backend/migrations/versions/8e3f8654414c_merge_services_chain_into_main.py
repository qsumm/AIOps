"""merge services chain into main

Revision ID: 8e3f8654414c
Revises: b5c7e91a4f38, c2d3e4f5a6b7
Create Date: 2026-10-19 02:01:17.874219
"""
from alembic import op
import sqlalchemy as sa


revision = '8e3f8654414c'
down_revision = ('b5c7e91a4f38', 'c2d3e4f5a6b7')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
