"""Add historical-customer reactivation campaigns.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "reactivation_campaigns" not in tables:
        op.create_table(
            "reactivation_campaigns",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="DRAFT"),
            sa.Column("subject_template", sa.String(length=998), nullable=False),
            sa.Column("body_template", sa.Text(), nullable=False),
            sa.Column("min_inactive_days", sa.Integer(), nullable=False, server_default="365"),
            sa.Column("reply_filter", sa.String(length=32), nullable=False, server_default="ANY"),
            sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Kolkata"),
            sa.Column("send_window_start_hour", sa.Integer(), nullable=False, server_default="9"),
            sa.Column("send_window_end_hour", sa.Integer(), nullable=False, server_default="17"),
            sa.Column("start_date", sa.Date(), nullable=False, server_default=sa.func.current_date()),
            sa.Column("max_reactivations", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("second_reactivation_days", sa.Integer(), nullable=False, server_default="90"),
            sa.Column("created_by", sa.String(length=128), nullable=False, server_default="admin"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_reactivation_campaigns_status", "reactivation_campaigns", ["status"])

    tables = set(sa.inspect(bind).get_table_names())
    if "reactivation_recipients" not in tables:
        op.create_table(
            "reactivation_recipients",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "campaign_id",
                sa.Integer(),
                sa.ForeignKey("reactivation_campaigns.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("contact_id", sa.Integer(), sa.ForeignKey("contacts.id"), nullable=False),
            sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id", ondelete="SET NULL"), nullable=True),
            sa.Column("outbox_id", sa.Integer(), sa.ForeignKey("outbox.id", ondelete="SET NULL"), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="CANDIDATE"),
            sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("exclusion_reason", sa.String(length=128), nullable=True),
            sa.Column("has_ever_replied", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("latest_direction", sa.String(length=16), nullable=True),
            sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("previous_reactivation_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("snapshot_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("campaign_id", "contact_id", name="uq_reactivation_campaign_contact"),
            sa.UniqueConstraint("outbox_id", name="uq_reactivation_recipients_outbox_id"),
        )
        for column in (
            "campaign_id",
            "customer_id",
            "contact_id",
            "case_id",
            "status",
            "eligible",
            "selected",
            "exclusion_reason",
            "last_contact_at",
            "scheduled_for",
        ):
            op.create_index(
                f"ix_reactivation_recipients_{column}",
                "reactivation_recipients",
                [column],
            )
        op.create_index(
            "ix_reactivation_recipient_schedule",
            "reactivation_recipients",
            ["status", "scheduled_for"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "reactivation_recipients" in tables:
        op.drop_table("reactivation_recipients")
    if "reactivation_campaigns" in tables:
        op.drop_table("reactivation_campaigns")
