"""Persist contact activity dates imported from CRM spreadsheets.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("contacts")}
    if "first_contact_at" not in columns:
        op.add_column("contacts", sa.Column("first_contact_at", sa.DateTime(timezone=True)))
    if "last_contact_at" not in columns:
        op.add_column("contacts", sa.Column("last_contact_at", sa.DateTime(timezone=True)))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("contacts")}
    if "ix_contacts_first_contact_at" not in indexes:
        op.create_index("ix_contacts_first_contact_at", "contacts", ["first_contact_at"])
    if "ix_contacts_last_contact_at" not in indexes:
        op.create_index("ix_contacts_last_contact_at", "contacts", ["last_contact_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("contacts")}
    indexes = {index["name"] for index in inspector.get_indexes("contacts")}
    if "last_contact_at" in columns:
        if "ix_contacts_last_contact_at" in indexes:
            op.drop_index("ix_contacts_last_contact_at", table_name="contacts")
        op.drop_column("contacts", "last_contact_at")
    if "first_contact_at" in columns:
        if "ix_contacts_first_contact_at" in indexes:
            op.drop_index("ix_contacts_first_contact_at", table_name="contacts")
        op.drop_column("contacts", "first_contact_at")
