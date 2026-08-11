"""Require complete human-approval metadata on outbox rows.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "ck_outbox_human_approval_complete"
CONSTRAINT_SQL = """
(
    approval_handoff_id IS NULL
    AND human_approved_by IS NULL
    AND human_approved_at IS NULL
)
OR
(
    approval_handoff_id IS NOT NULL
    AND human_approved_by IS NOT NULL
    AND human_approved_at IS NOT NULL
    AND length(trim(human_approved_by)) > 0
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "outbox" not in inspector.get_table_names():
        return
    invalid_rows = bind.execute(
        sa.text(
            """
            SELECT id
            FROM outbox
            WHERE NOT (
                (
                    approval_handoff_id IS NULL
                    AND human_approved_by IS NULL
                    AND human_approved_at IS NULL
                )
                OR
                (
                    approval_handoff_id IS NOT NULL
                    AND human_approved_by IS NOT NULL
                    AND human_approved_at IS NOT NULL
                    AND length(trim(human_approved_by)) > 0
                )
            )
            ORDER BY id
            LIMIT 20
            """
        )
    ).scalars().all()
    if invalid_rows:
        ids = ", ".join(str(row_id) for row_id in invalid_rows)
        raise RuntimeError(
            "Cannot add complete human-approval constraint: partial or blank "
            f"approval metadata exists on outbox row(s): {ids}"
        )
    constraints = {
        item["name"] for item in inspector.get_check_constraints("outbox")
    }
    if CONSTRAINT_NAME not in constraints:
        op.create_check_constraint(CONSTRAINT_NAME, "outbox", CONSTRAINT_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "outbox" not in inspector.get_table_names():
        return
    constraints = {
        item["name"] for item in inspector.get_check_constraints("outbox")
    }
    if CONSTRAINT_NAME in constraints:
        op.drop_constraint(CONSTRAINT_NAME, "outbox", type_="check")
