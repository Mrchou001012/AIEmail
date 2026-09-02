"""Durable AI batches for inbound disposition review.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_disposition_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("provider_batch_id", sa.String(length=128), nullable=True),
        sa.Column("provider_batch_ids_json", sa.JSON(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("ai_requested_count", sa.Integer(), nullable=False),
        sa.Column("rule_count", sa.Integer(), nullable=False),
        sa.Column("pending_count", sa.Integer(), nullable=False),
        sa.Column("succeeded_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inbound_disposition_batches_status",
        "inbound_disposition_batches",
        ["status"],
    )
    op.create_index(
        "ix_inbound_disposition_batches_provider_batch_id",
        "inbound_disposition_batches",
        ["provider_batch_id"],
    )
    op.create_table(
        "inbound_disposition_batch_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("email_id", sa.Integer(), nullable=False),
        sa.Column("custom_id", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("classification_json", sa.JSON(), nullable=False),
        sa.Column("provider_result_type", sa.String(length=32), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_history_json", sa.JSON(), nullable=False),
        sa.Column("needs_attention", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["inbound_disposition_batches.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "custom_id",
            name="uq_inbound_disposition_batch_custom_id",
        ),
        sa.UniqueConstraint(
            "batch_id",
            "email_id",
            name="uq_inbound_disposition_batch_email",
        ),
    )
    op.create_index(
        "ix_inbound_disposition_batch_items_batch_id",
        "inbound_disposition_batch_items",
        ["batch_id"],
    )
    op.create_index(
        "ix_inbound_disposition_batch_items_email_id",
        "inbound_disposition_batch_items",
        ["email_id"],
    )
    op.create_index(
        "ix_inbound_disposition_batch_items_status",
        "inbound_disposition_batch_items",
        ["status"],
    )
    op.create_index(
        "ix_inbound_disposition_batch_items_needs_attention",
        "inbound_disposition_batch_items",
        ["needs_attention"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbound_disposition_batch_items_needs_attention",
        table_name="inbound_disposition_batch_items",
    )
    op.drop_index(
        "ix_inbound_disposition_batch_items_status",
        table_name="inbound_disposition_batch_items",
    )
    op.drop_index(
        "ix_inbound_disposition_batch_items_email_id",
        table_name="inbound_disposition_batch_items",
    )
    op.drop_index(
        "ix_inbound_disposition_batch_items_batch_id",
        table_name="inbound_disposition_batch_items",
    )
    op.drop_table("inbound_disposition_batch_items")
    op.drop_index(
        "ix_inbound_disposition_batches_provider_batch_id",
        table_name="inbound_disposition_batches",
    )
    op.drop_index(
        "ix_inbound_disposition_batches_status",
        table_name="inbound_disposition_batches",
    )
    op.drop_table("inbound_disposition_batches")
