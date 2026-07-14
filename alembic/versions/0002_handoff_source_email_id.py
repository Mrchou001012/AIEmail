"""Add inbound email idempotency to handoffs.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("handoffs")}
    foreign_keys = {foreign_key["name"] for foreign_key in inspector.get_foreign_keys("handoffs")}
    unique_constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("handoffs")}

    if "source_email_id" not in columns:
        op.add_column("handoffs", sa.Column("source_email_id", sa.Integer(), nullable=True))
    if "fk_handoffs_source_email_id_emails" not in foreign_keys:
        op.create_foreign_key(
            "fk_handoffs_source_email_id_emails",
            "handoffs",
            "emails",
            ["source_email_id"],
            ["id"],
        )
    if "uq_handoff_source_email" not in unique_constraints:
        op.create_unique_constraint("uq_handoff_source_email", "handoffs", ["source_email_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("handoffs")}
    foreign_keys = {foreign_key["name"] for foreign_key in inspector.get_foreign_keys("handoffs")}
    unique_constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("handoffs")}

    if "uq_handoff_source_email" in unique_constraints:
        op.drop_constraint("uq_handoff_source_email", "handoffs", type_="unique")
    if "fk_handoffs_source_email_id_emails" in foreign_keys:
        op.drop_constraint("fk_handoffs_source_email_id_emails", "handoffs", type_="foreignkey")
    if "source_email_id" in columns:
        op.drop_column("handoffs", "source_email_id")
