"""Customer-facing catalog provenance fields.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("catalog_code", sa.String(length=128), nullable=True))
    op.add_column(
        "products",
        sa.Column(
            "catalog_visible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "catalog_visible")
    op.drop_column("products", "catalog_code")
