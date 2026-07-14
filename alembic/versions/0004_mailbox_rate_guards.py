"""Persist mailbox bandwidth and sending cooldown guards.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "mailbox_daily_usage" not in tables:
        op.create_table(
            "mailbox_daily_usage",
            sa.Column("mailbox", sa.String(length=320), nullable=False),
            sa.Column("usage_date", sa.Date(), nullable=False),
            sa.Column("imap_download_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("mailbox", "usage_date"),
        )
    if "mailbox_throttles" not in tables:
        op.create_table(
            "mailbox_throttles",
            sa.Column("mailbox", sa.String(length=320), nullable=False),
            sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("mailbox"),
        )

    outbox_columns = {column["name"] for column in inspector.get_columns("outbox")}
    if "sent_via" not in outbox_columns:
        op.add_column("outbox", sa.Column("sent_via", sa.String(length=32), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    outbox_columns = {column["name"] for column in inspector.get_columns("outbox")}

    if "sent_via" in outbox_columns:
        op.drop_column("outbox", "sent_via")
    if "mailbox_throttles" in tables:
        op.drop_table("mailbox_throttles")
    if "mailbox_daily_usage" in tables:
        op.drop_table("mailbox_daily_usage")
