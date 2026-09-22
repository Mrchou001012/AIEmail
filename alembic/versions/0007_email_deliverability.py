"""Add recipient preflight and bounce suppression records.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    email_columns = {column["name"] for column in inspector.get_columns("emails")}
    email_indexes = {index["name"] for index in inspector.get_indexes("emails")}

    for name, column in (
        ("is_bounce", sa.Column("is_bounce", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("bounce_type", sa.Column("bounce_type", sa.String(length=32), nullable=True)),
        (
            "bounce_metadata",
            sa.Column("bounce_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        ),
        ("bounce_handled_at", sa.Column("bounce_handled_at", sa.DateTime(timezone=True), nullable=True)),
    ):
        if name not in email_columns:
            op.add_column("emails", column)
    if "ix_emails_is_bounce" not in email_indexes:
        op.create_index("ix_emails_is_bounce", "emails", ["is_bounce"])
    if "ix_emails_bounce_type" not in email_indexes:
        op.create_index("ix_emails_bounce_type", "emails", ["bounce_type"])

    tables = _table_names()
    if "email_domain_statuses" not in tables:
        op.create_table(
            "email_domain_statuses",
            sa.Column("domain", sa.String(length=255), primary_key=True),
            sa.Column("mx_status", sa.String(length=32), nullable=False),
            sa.Column("mx_records", sa.JSON(), nullable=False),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_email_domain_statuses_mx_status", "email_domain_statuses", ["mx_status"])

    tables = _table_names()
    if "email_address_statuses" not in tables:
        op.create_table(
            "email_address_statuses",
            sa.Column("email", sa.String(length=320), primary_key=True),
            sa.Column("domain", sa.String(length=255), nullable=True),
            sa.Column("format_valid", sa.Boolean(), nullable=True),
            sa.Column("preflight_status", sa.String(length=32), nullable=True),
            sa.Column("last_preflight_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_preflight_detail", sa.Text(), nullable=True),
            sa.Column("suppressed", sa.Boolean(), nullable=False),
            sa.Column("suppression_reason", sa.String(length=64), nullable=True),
            sa.Column("suppression_source_email_id", sa.Integer(), nullable=True),
            sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_bounce_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_bounce_type", sa.String(length=32), nullable=True),
            sa.Column("last_bounce_diagnostic", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["suppression_source_email_id"],
                ["emails.id"],
                ondelete="SET NULL",
            ),
        )
        for name, columns in (
            ("ix_email_address_statuses_domain", ["domain"]),
            ("ix_email_address_statuses_preflight_status", ["preflight_status"]),
            ("ix_email_address_statuses_suppressed", ["suppressed"]),
            ("ix_email_address_statuses_suppression_reason", ["suppression_reason"]),
            ("ix_email_address_statuses_last_bounce_type", ["last_bounce_type"]),
        ):
            op.create_index(name, "email_address_statuses", columns)


def downgrade() -> None:
    tables = _table_names()
    if "email_address_statuses" in tables:
        op.drop_table("email_address_statuses")
    if "email_domain_statuses" in tables:
        op.drop_table("email_domain_statuses")

    inspector = sa.inspect(op.get_bind())
    email_columns = {column["name"] for column in inspector.get_columns("emails")}
    email_indexes = {index["name"] for index in inspector.get_indexes("emails")}
    if "ix_emails_bounce_type" in email_indexes:
        op.drop_index("ix_emails_bounce_type", table_name="emails")
    if "ix_emails_is_bounce" in email_indexes:
        op.drop_index("ix_emails_is_bounce", table_name="emails")
    for name in ("bounce_handled_at", "bounce_metadata", "bounce_type", "is_bounce"):
        if name in email_columns:
            op.drop_column("emails", name)
