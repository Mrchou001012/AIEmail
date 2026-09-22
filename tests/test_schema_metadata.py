from sqlalchemy import CheckConstraint, UniqueConstraint

from app.db import AgentRun, AgentStep, AssistanceRequest, ForwardRecipient, Outbox


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


def test_agent_runtime_has_durable_idempotency_constraints() -> None:
    run_table = AgentRun.metadata.tables["agent_runs"]
    step_table = AgentStep.metadata.tables["agent_steps"]
    assistance_table = AssistanceRequest.metadata.tables["assistance_requests"]

    run_unique = {
        constraint.name
        for constraint in run_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    step_unique = {
        constraint.name
        for constraint in step_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assistance_unique = {
        constraint.name
        for constraint in assistance_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert {"uq_agent_runs_source_email", "uq_agent_runs_handoff"} <= run_unique
    assert {"uq_agent_steps_run_sequence", "uq_agent_steps_run_key"} <= step_unique
    assert "uq_assistance_run_key" in assistance_unique
