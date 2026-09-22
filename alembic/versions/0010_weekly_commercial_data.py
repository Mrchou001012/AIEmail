"""Add weekly price and inventory confirmation gates.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _index_names(inspector, table: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table)}


def _foreign_key_names(inspector, table: str) -> set[str]:
    return {foreign_key["name"] for foreign_key in inspector.get_foreign_keys(table)}


def _has_foreign_key(inspector, table: str, column: str, target: str) -> bool:
    return any(
        foreign_key.get("constrained_columns") == [column]
        and foreign_key.get("referred_table") == target
        for foreign_key in inspector.get_foreign_keys(table)
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "commercial_data_cycles" not in tables:
        op.create_table(
            "commercial_data_cycles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("scope", sa.String(length=64), nullable=False, server_default="default"),
            sa.Column("week_start", sa.Date(), nullable=False),
            sa.Column("week_end", sa.Date(), nullable=False),
            sa.Column("price_status", sa.String(length=32), nullable=False, server_default="PENDING"),
            sa.Column("inventory_status", sa.String(length=32), nullable=False, server_default="PENDING"),
            sa.Column("price_confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("inventory_confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("price_source_system", sa.String(length=64), nullable=True),
            sa.Column("inventory_source_system", sa.String(length=64), nullable=True),
            sa.Column("price_source_ref", sa.String(length=255), nullable=True),
            sa.Column("inventory_source_ref", sa.String(length=255), nullable=True),
            sa.Column("reminder_status", sa.String(length=32), nullable=False, server_default="PENDING"),
            sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("scope", "week_start", name="uq_commercial_cycle_scope_week"),
        )
        op.create_index("ix_commercial_data_cycles_scope", "commercial_data_cycles", ["scope"])
        op.create_index("ix_commercial_data_cycles_week_start", "commercial_data_cycles", ["week_start"])
        op.create_index("ix_commercial_data_cycles_price_status", "commercial_data_cycles", ["price_status"])
        op.create_index(
            "ix_commercial_data_cycles_inventory_status",
            "commercial_data_cycles",
            ["inventory_status"],
        )
        op.create_index(
            "ix_commercial_data_cycles_reminder_status",
            "commercial_data_cycles",
            ["reminder_status"],
        )

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "inventory_snapshots" not in tables:
        op.create_table(
            "inventory_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "cycle_id",
                sa.Integer(),
                sa.ForeignKey("commercial_data_cycles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("availability", sa.String(length=32), nullable=False, server_default="UNKNOWN"),
            sa.Column("quantity", sa.Numeric(18, 4), nullable=True),
            sa.Column("warehouse", sa.String(length=128), nullable=True),
            sa.Column("source_system", sa.String(length=64), nullable=False, server_default="manual"),
            sa.Column("external_id", sa.String(length=255), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("cycle_id", "product_id", name="uq_inventory_cycle_product"),
        )
        op.create_index("ix_inventory_snapshots_cycle_id", "inventory_snapshots", ["cycle_id"])
        op.create_index("ix_inventory_snapshots_product_id", "inventory_snapshots", ["product_id"])
        op.create_index("ix_inventory_snapshots_availability", "inventory_snapshots", ["availability"])

    inspector = sa.inspect(bind)
    policy_columns = {column["name"] for column in inspector.get_columns("price_policies")}
    quote_columns = {column["name"] for column in inspector.get_columns("quotes")}
    outbox_columns = {column["name"] for column in inspector.get_columns("outbox")}
    if "commercial_cycle_id" not in policy_columns:
        op.add_column("price_policies", sa.Column("commercial_cycle_id", sa.Integer(), nullable=True))
    if "commercial_cycle_id" not in quote_columns:
        op.add_column("quotes", sa.Column("commercial_cycle_id", sa.Integer(), nullable=True))
    if "quote_id" not in outbox_columns:
        op.add_column("outbox", sa.Column("quote_id", sa.Integer(), nullable=True))
    if "message_kind" not in outbox_columns:
        op.add_column(
            "outbox",
            sa.Column("message_kind", sa.String(length=32), nullable=False, server_default="GENERAL"),
        )

    inspector = sa.inspect(bind)
    for table, column, target, name, ondelete in (
        (
            "price_policies",
            "commercial_cycle_id",
            "commercial_data_cycles",
            "fk_price_policies_commercial_cycle_id",
            "SET NULL",
        ),
        (
            "quotes",
            "commercial_cycle_id",
            "commercial_data_cycles",
            "fk_quotes_commercial_cycle_id",
            "SET NULL",
        ),
        ("outbox", "quote_id", "quotes", "fk_outbox_quote_id", "SET NULL"),
    ):
        if not _has_foreign_key(inspector, table, column, target):
            op.create_foreign_key(name, table, target, [column], ["id"], ondelete=ondelete)
        inspector = sa.inspect(bind)

    for table, column, name in (
        ("price_policies", "commercial_cycle_id", "ix_price_policies_commercial_cycle_id"),
        ("quotes", "commercial_cycle_id", "ix_quotes_commercial_cycle_id"),
        ("outbox", "quote_id", "ix_outbox_quote_id"),
        ("outbox", "message_kind", "ix_outbox_message_kind"),
    ):
        inspector = sa.inspect(bind)
        if name not in _index_names(inspector, table):
            op.create_index(name, table, [column])

    # Existing active policies and the former ready-stock assumption are not
    # silently certified as this week's commercial data. The worker creates a
    # PENDING cycle and requires an explicit fresh price import plus independent
    # inventory confirmation before any autonomous quotation can proceed.
    op.execute(
        sa.text(
            """
            UPDATE quotes AS quote
            SET commercial_cycle_id = policy.commercial_cycle_id
            FROM price_policies AS policy
            WHERE quote.price_policy_id = policy.id
              AND quote.commercial_cycle_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE outbox AS outbound
            SET quote_id = (
                SELECT quote.id
                FROM quotes AS quote
                WHERE quote.case_id = outbound.case_id
                  AND quote.created_at <= outbound.created_at
                ORDER BY quote.created_at DESC, quote.id DESC
                LIMIT 1
            )
            WHERE outbound.quote_id IS NULL
              AND outbound.approval_handoff_id IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM quotes AS quote
                  WHERE quote.case_id = outbound.case_id
                    AND quote.created_at <= outbound.created_at
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE outbox
            SET message_kind = CASE
                WHEN approval_handoff_id IS NOT NULL THEN 'HUMAN_REPLY'
                WHEN quote_id IS NOT NULL THEN 'AUTO_QUOTE'
                ELSE message_kind
            END
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, column, index_name, fk_name in (
        ("outbox", "quote_id", "ix_outbox_quote_id", "fk_outbox_quote_id"),
        (
            "quotes",
            "commercial_cycle_id",
            "ix_quotes_commercial_cycle_id",
            "fk_quotes_commercial_cycle_id",
        ),
        (
            "price_policies",
            "commercial_cycle_id",
            "ix_price_policies_commercial_cycle_id",
            "fk_price_policies_commercial_cycle_id",
        ),
    ):
        inspector = sa.inspect(bind)
        if index_name in _index_names(inspector, table):
            op.drop_index(index_name, table_name=table)
        inspector = sa.inspect(bind)
        if fk_name in _foreign_key_names(inspector, table):
            op.drop_constraint(fk_name, table, type_="foreignkey")
        columns = {item["name"] for item in sa.inspect(bind).get_columns(table)}
        if column in columns:
            op.drop_column(table, column)

    inspector = sa.inspect(bind)
    outbox_columns = {column["name"] for column in inspector.get_columns("outbox")}
    if "ix_outbox_message_kind" in _index_names(inspector, "outbox"):
        op.drop_index("ix_outbox_message_kind", table_name="outbox")
    if "message_kind" in outbox_columns:
        op.drop_column("outbox", "message_kind")
    tables = set(sa.inspect(bind).get_table_names())
    if "inventory_snapshots" in tables:
        op.drop_table("inventory_snapshots")
    if "commercial_data_cycles" in tables:
        op.drop_table("commercial_data_cycles")
