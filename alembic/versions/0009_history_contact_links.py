"""Link email history to customers and contacts independently of cases.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("emails")}
    if "customer_id" not in columns:
        op.add_column("emails", sa.Column("customer_id", sa.Integer(), nullable=True))
    if "contact_id" not in columns:
        op.add_column("emails", sa.Column("contact_id", sa.Integer(), nullable=True))

    # Base.metadata.create_all() is used by the initial migration, so a fresh
    # database may already contain these constraints and indexes by the time
    # this incremental migration is reached.
    inspector = sa.inspect(bind)
    foreign_keys = {
        foreign_key["name"] for foreign_key in inspector.get_foreign_keys("emails")
    }
    indexes = {index["name"] for index in inspector.get_indexes("emails")}
    if "fk_emails_customer_id_customers" not in foreign_keys:
        op.create_foreign_key(
            "fk_emails_customer_id_customers",
            "emails",
            "customers",
            ["customer_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "fk_emails_contact_id_contacts" not in foreign_keys:
        op.create_foreign_key(
            "fk_emails_contact_id_contacts",
            "emails",
            "contacts",
            ["contact_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_emails_customer_id" not in indexes:
        op.create_index("ix_emails_customer_id", "emails", ["customer_id"])
    if "ix_emails_contact_id" not in indexes:
        op.create_index("ix_emails_contact_id", "emails", ["contact_id"])

    op.execute(
        sa.text(
            """
            UPDATE emails AS email
            SET customer_id = sales_case.customer_id,
                contact_id = sales_case.contact_id
            FROM cases AS sales_case
            WHERE email.case_id = sales_case.id
              AND (
                email.customer_id IS DISTINCT FROM sales_case.customer_id
                OR email.contact_id IS DISTINCT FROM sales_case.contact_id
              )
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("emails")}
    foreign_keys = {
        foreign_key["name"] for foreign_key in inspector.get_foreign_keys("emails")
    }
    columns = {column["name"] for column in inspector.get_columns("emails")}

    if "ix_emails_contact_id" in indexes:
        op.drop_index("ix_emails_contact_id", table_name="emails")
    if "ix_emails_customer_id" in indexes:
        op.drop_index("ix_emails_customer_id", table_name="emails")
    if "fk_emails_contact_id_contacts" in foreign_keys:
        op.drop_constraint(
            "fk_emails_contact_id_contacts",
            "emails",
            type_="foreignkey",
        )
    if "fk_emails_customer_id_customers" in foreign_keys:
        op.drop_constraint(
            "fk_emails_customer_id_customers",
            "emails",
            type_="foreignkey",
        )
    if "contact_id" in columns:
        op.drop_column("emails", "contact_id")
    if "customer_id" in columns:
        op.drop_column("emails", "customer_id")
