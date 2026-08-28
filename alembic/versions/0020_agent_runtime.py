"""Durable Agent runs, execution steps, and typed human assistance.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "agent_runs" not in existing:
        op.create_table(
            "agent_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "case_id",
                sa.Integer(),
                sa.ForeignKey("cases.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "source_email_id",
                sa.Integer(),
                sa.ForeignKey("emails.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "handoff_id",
                sa.Integer(),
                sa.ForeignKey("handoffs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "run_kind",
                sa.String(length=64),
                nullable=False,
                server_default="INBOUND_EMAIL",
            ),
            sa.Column("goal", sa.Text(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="WAITING_HUMAN",
            ),
            sa.Column(
                "context_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
            sa.Column("current_step", sa.String(length=128), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.UniqueConstraint("source_email_id", name="uq_agent_runs_source_email"),
            sa.UniqueConstraint("handoff_id", name="uq_agent_runs_handoff"),
            sa.CheckConstraint("version >= 1", name="ck_agent_runs_version_positive"),
        )
        op.create_index("ix_agent_runs_case_id", "agent_runs", ["case_id"])
        op.create_index("ix_agent_runs_status", "agent_runs", ["status"])

    existing = set(sa.inspect(bind).get_table_names())
    if "agent_steps" not in existing:
        op.create_table(
            "agent_steps",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "run_id",
                sa.Integer(),
                sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("idempotency_key", sa.String(length=255), nullable=False),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="WAITING",
            ),
            sa.Column(
                "input_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
            sa.Column(
                "output_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "run_id", "sequence", name="uq_agent_steps_run_sequence"
            ),
            sa.UniqueConstraint(
                "run_id", "idempotency_key", name="uq_agent_steps_run_key"
            ),
            sa.CheckConstraint(
                "sequence >= 1", name="ck_agent_steps_sequence_positive"
            ),
        )
        op.create_index("ix_agent_steps_run_id", "agent_steps", ["run_id"])
        op.create_index("ix_agent_steps_kind", "agent_steps", ["kind"])
        op.create_index("ix_agent_steps_status", "agent_steps", ["status"])

    existing = set(sa.inspect(bind).get_table_names())
    if "assistance_requests" not in existing:
        op.create_table(
            "assistance_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "run_id",
                sa.Integer(),
                sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "handoff_id",
                sa.Integer(),
                sa.ForeignKey("handoffs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("request_key", sa.String(length=128), nullable=False),
            sa.Column("request_type", sa.String(length=64), nullable=False),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column(
                "response_schema",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
            sa.Column(
                "options_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="OPEN",
            ),
            sa.Column("answer_json", sa.JSON(), nullable=True),
            sa.Column("answered_by", sa.String(length=128), nullable=True),
            sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.UniqueConstraint("run_id", "request_key", name="uq_assistance_run_key"),
        )
        op.create_index(
            "ix_assistance_requests_run_id", "assistance_requests", ["run_id"]
        )
        op.create_index(
            "ix_assistance_requests_handoff_id",
            "assistance_requests",
            ["handoff_id"],
        )
        op.create_index(
            "ix_assistance_requests_request_type",
            "assistance_requests",
            ["request_type"],
        )
        op.create_index(
            "ix_assistance_requests_status", "assistance_requests", ["status"]
        )

    # Every existing open inbound handoff receives durable task state. Product
    # category reviews additionally receive the typed resumable contract
    # below. The inserts are idempotent so this also works on fresh databases
    # where 0001's create_all already materialized the current metadata.
    op.execute(
        sa.text(
            """
            INSERT INTO agent_runs (
                case_id, source_email_id, handoff_id, run_kind, goal, status,
                context_json, current_step, version, created_at, updated_at
            )
            SELECT
                h.case_id,
                h.source_email_id,
                h.id,
                'INBOUND_EMAIL',
                'Resolve the inbound customer request and prepare a policy-compliant reply',
                'WAITING_HUMAN',
                json_build_object(
                    'handoff_reason', h.reason_code,
                    'source_email_id', h.source_email_id
                ),
                CASE
                    WHEN h.reason_code = 'PRODUCT_CATEGORY_REVIEW'
                        THEN 'select-product-category'
                    ELSE 'human_review'
                END,
                1,
                now(),
                now()
            FROM handoffs h
            WHERE h.status = 'OPEN'
              AND h.source_email_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO agent_steps (
                run_id, sequence, kind, idempotency_key, status, input_json,
                output_json, created_at
            )
            SELECT
                r.id,
                1,
                'HUMAN_HANDOFF',
                'initial-handoff',
                'WAITING',
                json_build_object(
                    'handoff_id', h.id,
                    'reason', h.reason_code,
                    'summary', h.summary
                ),
                '{}'::json,
                now()
            FROM agent_runs r
            JOIN handoffs h ON h.id = r.handoff_id
            WHERE h.status = 'OPEN'
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO assistance_requests (
                run_id, handoff_id, request_key, request_type, question,
                response_schema, options_json, status, created_at, updated_at
            )
            SELECT
                r.id,
                h.id,
                'select-product-category',
                'PRODUCT_CATEGORY_SELECTION',
                '请选择应发送给该客户的产品系列。确认后 Agent 会从当前断点继续，重新执行安全校验并生成产品目录回复。',
                json_build_object(
                    'type', 'object',
                    'required', json_build_array('category_id'),
                    'properties', json_build_object(
                        'category_id', json_build_object(
                            'type', 'integer', 'minimum', 1
                        ),
                        'note', json_build_object(
                            'type', 'string', 'maxLength', 2000
                        )
                    ),
                    'additionalProperties', false
                ),
                COALESCE(
                    (
                        SELECT json_agg(
                            json_build_object(
                                'category_id', c.id,
                                'key', c.key,
                                'name', c.name,
                                'name_zh', c.name_zh,
                                'recommended', false
                            ) ORDER BY c.sort_order, c.id
                        )
                        FROM product_categories c
                        WHERE c.active = true
                    ),
                    '[]'::json
                ),
                'OPEN',
                now(),
                now()
            FROM agent_runs r
            JOIN handoffs h ON h.id = r.handoff_id
            WHERE h.status = 'OPEN'
              AND h.reason_code = 'PRODUCT_CATEGORY_REVIEW'
            ON CONFLICT DO NOTHING
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "assistance_requests" in existing:
        op.drop_table("assistance_requests")
    if "agent_steps" in existing:
        op.drop_table("agent_steps")
    if "agent_runs" in existing:
        op.drop_table("agent_runs")
