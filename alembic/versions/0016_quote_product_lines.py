"""Multi-product quotations: one case can carry one quote row per product.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    quote_columns = {column["name"] for column in sa.inspect(bind).get_columns("quotes")}
    if "product_id" not in quote_columns:
        op.add_column("quotes", sa.Column("product_id", sa.Integer(), nullable=True))
        # Backfill from the historical single-product case association so the
        # new uniqueness scope does not reject existing rows.
        op.execute(
            "UPDATE quotes SET product_id = (SELECT product_id FROM cases "
            "WHERE cases.id = quotes.case_id) WHERE product_id IS NULL"
        )
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("quotes")}
    if "fk_quotes_product_id_products" not in foreign_keys:
        op.create_foreign_key(
            "fk_quotes_product_id_products",
            "quotes",
            "products",
            ["product_id"],
            ["id"],
            ondelete="SET NULL",
        )
    unique_names = {item["name"] for item in inspector.get_unique_constraints("quotes")}
    if "uq_quote_case_round" in unique_names:
        op.drop_constraint("uq_quote_case_round", "quotes", type_="unique")
    if "uq_quote_case_round_product" not in unique_names:
        op.create_unique_constraint(
            "uq_quote_case_round_product",
            "quotes",
            ["case_id", "round_number", "product_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    unique_names = {item["name"] for item in inspector.get_unique_constraints("quotes")}
    if "uq_quote_case_round_product" in unique_names:
        op.drop_constraint("uq_quote_case_round_product", "quotes", type_="unique")
    if "uq_quote_case_round" not in unique_names:
        op.create_unique_constraint("uq_quote_case_round", "quotes", ["case_id", "round_number"])
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("quotes")}
    if "fk_quotes_product_id_products" in foreign_keys:
        op.drop_constraint("fk_quotes_product_id_products", "quotes", type_="foreignkey")
    quote_columns = {column["name"] for column in sa.inspect(bind).get_columns("quotes")}
    if "product_id" in quote_columns:
        op.drop_column("quotes", "product_id")
