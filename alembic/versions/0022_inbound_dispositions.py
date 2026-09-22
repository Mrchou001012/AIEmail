"""Inbound disposition and customer/contact lifecycle state.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "qualification_status",
            sa.String(length=32),
            nullable=False,
            server_default="UNKNOWN",
        ),
    )
    op.add_column(
        "customers",
        sa.Column("qualification_reason", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "customers",
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_customers_qualification_status",
        "customers",
        ["qualification_status"],
    )
    op.create_index(
        "ix_customers_qualification_reason",
        "customers",
        ["qualification_reason"],
    )

    op.add_column(
        "contacts",
        sa.Column(
            "lifecycle_status",
            sa.String(length=32),
            nullable=False,
            server_default="ACTIVE",
        ),
    )
    op.add_column(
        "contacts",
        sa.Column("unavailable_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_contacts_lifecycle_status", "contacts", ["lifecycle_status"]
    )
    op.create_index(
        "ix_contacts_unavailable_until", "contacts", ["unavailable_until"]
    )

    op.add_column(
        "emails",
        sa.Column("disposition_type", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "emails",
        sa.Column("disposition_confidence", sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        "emails",
        sa.Column(
            "disposition_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "emails",
        sa.Column("disposition_handled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_emails_disposition_type", "emails", ["disposition_type"])
    op.create_index(
        "ix_emails_disposition_handled_at", "emails", ["disposition_handled_at"]
    )

    op.create_table(
        "contact_referrals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_email_id",
            sa.Integer(),
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "original_contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "new_contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("referred_email", sa.String(length=320), nullable=False),
        sa.Column("referred_name", sa.String(length=255), nullable=True),
        sa.Column(
            "relationship_type",
            sa.String(length=32),
            nullable=False,
            server_default="REPLACEMENT",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="CANDIDATE",
        ),
        sa.Column(
            "forwarded_already",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column(
            "metadata_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "source_email_id",
            "referred_email",
            name="uq_contact_referrals_source_email",
        ),
    )
    op.create_index(
        "ix_contact_referrals_source_email_id",
        "contact_referrals",
        ["source_email_id"],
    )
    op.create_index(
        "ix_contact_referrals_customer_id", "contact_referrals", ["customer_id"]
    )
    op.create_index(
        "ix_contact_referrals_original_contact_id",
        "contact_referrals",
        ["original_contact_id"],
    )
    op.create_index(
        "ix_contact_referrals_new_contact_id",
        "contact_referrals",
        ["new_contact_id"],
    )
    op.create_index(
        "ix_contact_referrals_referred_email",
        "contact_referrals",
        ["referred_email"],
    )
    op.create_index(
        "ix_contact_referrals_status", "contact_referrals", ["status"]
    )

    op.create_table(
        "inbound_disposition_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_email_id",
            sa.Integer(),
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("disposition_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="APPLIED",
        ),
        sa.Column("applied_by", sa.String(length=128), nullable=False),
        sa.Column(
            "before_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "after_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("rolled_back_by", sa.String(length=128), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_inbound_disposition_actions_source_email_id",
        "inbound_disposition_actions",
        ["source_email_id"],
    )
    op.create_index(
        "ix_inbound_disposition_actions_disposition_type",
        "inbound_disposition_actions",
        ["disposition_type"],
    )
    op.create_index(
        "ix_inbound_disposition_actions_status",
        "inbound_disposition_actions",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("inbound_disposition_actions")
    op.drop_table("contact_referrals")
    op.drop_index("ix_emails_disposition_handled_at", table_name="emails")
    op.drop_index("ix_emails_disposition_type", table_name="emails")
    op.drop_column("emails", "disposition_handled_at")
    op.drop_column("emails", "disposition_metadata")
    op.drop_column("emails", "disposition_confidence")
    op.drop_column("emails", "disposition_type")
    op.drop_index("ix_contacts_unavailable_until", table_name="contacts")
    op.drop_index("ix_contacts_lifecycle_status", table_name="contacts")
    op.drop_column("contacts", "unavailable_until")
    op.drop_column("contacts", "lifecycle_status")
    op.drop_index("ix_customers_qualification_reason", table_name="customers")
    op.drop_index("ix_customers_qualification_status", table_name="customers")
    op.drop_column("customers", "qualified_at")
    op.drop_column("customers", "qualification_reason")
    op.drop_column("customers", "qualification_status")
