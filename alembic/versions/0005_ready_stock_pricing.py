"""Add ready-stock quantity tiers and commercial quote terms.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    product_columns = {column["name"] for column in inspector.get_columns("products")}
    policy_columns = {column["name"] for column in inspector.get_columns("price_policies")}

    if "margin_class" not in product_columns:
        op.add_column("products", sa.Column("margin_class", sa.String(length=1), nullable=True))
    if "tier_1_max_multiple" not in policy_columns:
        op.add_column("price_policies", sa.Column("tier_1_max_multiple", sa.Numeric(8, 4), nullable=True))
    if "tier_1_markup_pct" not in policy_columns:
        op.add_column(
            "price_policies",
            sa.Column("tier_1_markup_pct", sa.Numeric(7, 4), nullable=False, server_default="0"),
        )
    if "tier_2_max_multiple" not in policy_columns:
        op.add_column("price_policies", sa.Column("tier_2_max_multiple", sa.Numeric(8, 4), nullable=True))
    if "tier_2_markup_pct" not in policy_columns:
        op.add_column(
            "price_policies",
            sa.Column("tier_2_markup_pct", sa.Numeric(7, 4), nullable=False, server_default="0"),
        )
    if "quote_valid_weekday" not in policy_columns:
        op.add_column("price_policies", sa.Column("quote_valid_weekday", sa.Integer(), nullable=True))
    if "taxes_included" not in policy_columns:
        op.add_column(
            "price_policies",
            sa.Column("taxes_included", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "freight_included" not in policy_columns:
        op.add_column(
            "price_policies",
            sa.Column("freight_included", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    product_columns = {column["name"] for column in inspector.get_columns("products")}
    policy_columns = {column["name"] for column in inspector.get_columns("price_policies")}

    for column_name in (
        "freight_included",
        "taxes_included",
        "quote_valid_weekday",
        "tier_2_markup_pct",
        "tier_2_max_multiple",
        "tier_1_markup_pct",
        "tier_1_max_multiple",
    ):
        if column_name in policy_columns:
            op.drop_column("price_policies", column_name)
    if "margin_class" in product_columns:
        op.drop_column("products", "margin_class")
