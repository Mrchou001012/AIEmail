from sqlalchemy import CheckConstraint, UniqueConstraint

from app.db import ForwardRecipient, Outbox


def test_forward_recipient_email_uses_named_unique_constraint_without_extra_index() -> None:
    table = ForwardRecipient.metadata.tables["forward_recipients"]
    unique_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert "uq_forward_recipients_email" in unique_names
    assert "ix_forward_recipients_email" not in {index.name for index in table.indexes}


def test_outbox_metadata_requires_complete_human_approval() -> None:
    table = Outbox.metadata.tables["outbox"]
    check_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "ck_outbox_human_approval_complete" in check_names
