"""Preserve source associations for imported email endpoints.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("contacts")}
    if "metadata_json" not in columns:
        op.add_column(
            "contacts",
            sa.Column(
                "metadata_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("contacts")}
    if "metadata_json" in columns:
        op.drop_column("contacts", "metadata_json")
