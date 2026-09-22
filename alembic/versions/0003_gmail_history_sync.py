"""Track Gmail history synchronization state.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

NEW_MAILBOX_UID_COLUMNS = ["mailbox", "mailbox_folder", "uid_validity", "imap_uid"]
OLD_MAILBOX_UID_COLUMNS = ["mailbox", "uid_validity", "imap_uid"]


def _unique_constraints(inspector) -> dict[str, list[str]]:
    return {
        constraint["name"]: list(constraint.get("column_names") or [])
        for constraint in inspector.get_unique_constraints("emails")
        if constraint.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    email_columns = {column["name"] for column in inspector.get_columns("emails")}
    cursor_columns = {column["name"] for column in inspector.get_columns("mailbox_cursors")}
    unique_constraints = _unique_constraints(inspector)
    indexes = {index["name"] for index in inspector.get_indexes("emails")}

    if "mailbox_folder" not in email_columns:
        op.add_column("emails", sa.Column("mailbox_folder", sa.String(length=255), nullable=True))
    if unique_constraints.get("uq_mailbox_uid") != NEW_MAILBOX_UID_COLUMNS:
        if "uq_mailbox_uid" in unique_constraints:
            op.drop_constraint("uq_mailbox_uid", "emails", type_="unique")
        op.create_unique_constraint("uq_mailbox_uid", "emails", NEW_MAILBOX_UID_COLUMNS)
    if "is_history" not in email_columns:
        op.add_column(
            "emails",
            sa.Column("is_history", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "ix_emails_is_history" not in indexes:
        op.create_index("ix_emails_is_history", "emails", ["is_history"])
    if "history_cutoff_uid" not in cursor_columns:
        op.add_column("mailbox_cursors", sa.Column("history_cutoff_uid", sa.Integer(), nullable=True))
    if "history_complete" not in cursor_columns:
        op.add_column(
            "mailbox_cursors",
            sa.Column("history_complete", sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    email_columns = {column["name"] for column in inspector.get_columns("emails")}
    cursor_columns = {column["name"] for column in inspector.get_columns("mailbox_cursors")}
    unique_constraints = _unique_constraints(inspector)
    indexes = {index["name"] for index in inspector.get_indexes("emails")}

    if "history_complete" in cursor_columns:
        op.drop_column("mailbox_cursors", "history_complete")
    if "history_cutoff_uid" in cursor_columns:
        op.drop_column("mailbox_cursors", "history_cutoff_uid")
    if "ix_emails_is_history" in indexes:
        op.drop_index("ix_emails_is_history", table_name="emails")
    if "is_history" in email_columns:
        op.drop_column("emails", "is_history")
    if unique_constraints.get("uq_mailbox_uid") != OLD_MAILBOX_UID_COLUMNS:
        if "uq_mailbox_uid" in unique_constraints:
            op.drop_constraint("uq_mailbox_uid", "emails", type_="unique")
        op.create_unique_constraint("uq_mailbox_uid", "emails", OLD_MAILBOX_UID_COLUMNS)
    if "mailbox_folder" in email_columns:
        op.drop_column("emails", "mailbox_folder")
