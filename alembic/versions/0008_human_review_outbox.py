"""Record explicit human approval on outbound messages.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("outbox")}
    indexes = {index["name"] for index in inspector.get_indexes("outbox")}
    constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("outbox")
    }
    foreign_keys = {foreign_key["name"] for foreign_key in inspector.get_foreign_keys("outbox")}

    if "approval_handoff_id" not in columns:
        op.add_column("outbox", sa.Column("approval_handoff_id", sa.Integer(), nullable=True))
    if "human_approved_by" not in columns:
        op.add_column("outbox", sa.Column("human_approved_by", sa.String(length=128), nullable=True))
    if "human_approved_at" not in columns:
        op.add_column(
            "outbox",
            sa.Column("human_approved_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "fk_outbox_approval_handoff_id_handoffs" not in foreign_keys:
        op.create_foreign_key(
            "fk_outbox_approval_handoff_id_handoffs",
            "outbox",
            "handoffs",
            ["approval_handoff_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "uq_outbox_approval_handoff_id" not in constraints:
        op.create_unique_constraint(
            "uq_outbox_approval_handoff_id",
            "outbox",
            ["approval_handoff_id"],
        )
    if "ix_outbox_approval_handoff_id" not in indexes:
        op.create_index(
            "ix_outbox_approval_handoff_id",
            "outbox",
            ["approval_handoff_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("outbox")}
    indexes = {index["name"] for index in inspector.get_indexes("outbox")}
    constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("outbox")
    }
    foreign_keys = {foreign_key["name"] for foreign_key in inspector.get_foreign_keys("outbox")}

    if "ix_outbox_approval_handoff_id" in indexes:
        op.drop_index("ix_outbox_approval_handoff_id", table_name="outbox")
    if "uq_outbox_approval_handoff_id" in constraints:
        op.drop_constraint("uq_outbox_approval_handoff_id", "outbox", type_="unique")
    if "fk_outbox_approval_handoff_id_handoffs" in foreign_keys:
        op.drop_constraint(
            "fk_outbox_approval_handoff_id_handoffs",
            "outbox",
            type_="foreignkey",
        )
    for column_name in ("human_approved_at", "human_approved_by", "approval_handoff_id"):
        if column_name in columns:
            op.drop_column("outbox", column_name)
