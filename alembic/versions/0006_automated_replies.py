"""Record and safely route inbound automated replies.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("emails")}
    indexes = {index["name"] for index in inspector.get_indexes("emails")}

    if "is_automated_reply" not in columns:
        op.add_column(
            "emails",
            sa.Column("is_automated_reply", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "automated_reply_type" not in columns:
        op.add_column("emails", sa.Column("automated_reply_type", sa.String(length=32), nullable=True))
    if "automated_reply_metadata" not in columns:
        op.add_column(
            "emails",
            sa.Column(
                "automated_reply_metadata",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )
    if "automated_reply_handled_at" not in columns:
        op.add_column(
            "emails",
            sa.Column("automated_reply_handled_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "ix_emails_is_automated_reply" not in indexes:
        op.create_index("ix_emails_is_automated_reply", "emails", ["is_automated_reply"])
    if "ix_emails_automated_reply_type" not in indexes:
        op.create_index("ix_emails_automated_reply_type", "emails", ["automated_reply_type"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("emails")}
    indexes = {index["name"] for index in inspector.get_indexes("emails")}

    if "ix_emails_automated_reply_type" in indexes:
        op.drop_index("ix_emails_automated_reply_type", table_name="emails")
    if "ix_emails_is_automated_reply" in indexes:
        op.drop_index("ix_emails_is_automated_reply", table_name="emails")
    for column_name in (
        "automated_reply_handled_at",
        "automated_reply_metadata",
        "automated_reply_type",
        "is_automated_reply",
    ):
        if column_name in columns:
            op.drop_column("emails", column_name)
