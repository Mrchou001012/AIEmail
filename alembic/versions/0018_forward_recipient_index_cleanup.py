"""Remove the redundant non-unique forward-recipient email index.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "forward_recipients" not in inspector.get_table_names():
        return
    indexes = {
        item["name"]: item
        for item in inspector.get_indexes("forward_recipients")
    }
    redundant = indexes.get("ix_forward_recipients_email")
    if redundant is not None and not redundant.get("unique", False):
        op.drop_index(
            "ix_forward_recipients_email",
            table_name="forward_recipients",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "forward_recipients" not in inspector.get_table_names():
        return
    index_names = {
        item["name"]
        for item in inspector.get_indexes("forward_recipients")
    }
    if "ix_forward_recipients_email" not in index_names:
        op.create_index(
            "ix_forward_recipients_email",
            "forward_recipients",
            ["email"],
        )
