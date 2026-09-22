"""Product categories and catalog fields for automatic product-list replies.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Migration 0001 creates every table registered on Base.metadata via
    # create_all(), which already includes product_categories when
    # ProductCategory is imported. Guard the create so a fresh database
    # does not fail on "relation already exists".
    existing_tables = set(sa.inspect(bind).get_table_names())
    if "product_categories" not in existing_tables:
        op.create_table(
            "product_categories",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("name_zh", sa.String(length=255), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        )
        op.create_index("ix_product_categories_key", "product_categories", ["key"], unique=True)

    inspector = sa.inspect(bind)
    product_columns = {column["name"] for column in sa.inspect(bind).get_columns("products")}
    for name, column in (
        ("category_id", sa.Column("category_id", sa.Integer(), nullable=True)),
        ("brand", sa.Column("brand", sa.String(length=64), nullable=True)),
        ("cas_no", sa.Column("cas_no", sa.String(length=64), nullable=True)),
        ("content", sa.Column("content", sa.String(length=64), nullable=True)),
        ("series", sa.Column("series", sa.String(length=128), nullable=True)),
        ("sort_order", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")),
    ):
        if name not in product_columns:
            op.add_column("products", column)
    product_fks = {fk["name"] for fk in inspector.get_foreign_keys("products")}
    if "fk_products_category_id_product_categories" not in product_fks:
        op.create_foreign_key(
            "fk_products_category_id_product_categories",
            "products",
            "product_categories",
            ["category_id"],
            ["id"],
            ondelete="SET NULL",
        )
    product_indexes = {ix["name"] for ix in inspector.get_indexes("products")}
    if "ix_products_category_id" not in product_indexes:
        op.create_index("ix_products_category_id", "products", ["category_id"])

    case_columns = {column["name"] for column in sa.inspect(bind).get_columns("cases")}
    if "category_id" not in case_columns:
        op.add_column("cases", sa.Column("category_id", sa.Integer(), nullable=True))
    case_fks = {fk["name"] for fk in inspector.get_foreign_keys("cases")}
    if "fk_cases_category_id_product_categories" not in case_fks:
        op.create_foreign_key(
            "fk_cases_category_id_product_categories",
            "cases",
            "product_categories",
            ["category_id"],
            ["id"],
            ondelete="SET NULL",
        )
    case_indexes = {ix["name"] for ix in inspector.get_indexes("cases")}
    if "ix_cases_category_id" not in case_indexes:
        op.create_index("ix_cases_category_id", "cases", ["category_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    case_columns = {column["name"] for column in sa.inspect(bind).get_columns("cases")}
    if "category_id" in case_columns:
        case_fks = {fk["name"] for fk in inspector.get_foreign_keys("cases")}
        if "fk_cases_category_id_product_categories" in case_fks:
            op.drop_constraint("fk_cases_category_id_product_categories", "cases", type_="foreignkey")
        case_indexes = {ix["name"] for ix in inspector.get_indexes("cases")}
        if "ix_cases_category_id" in case_indexes:
            op.drop_index("ix_cases_category_id", table_name="cases")
        op.drop_column("cases", "category_id")

    product_columns = {column["name"] for column in sa.inspect(bind).get_columns("products")}
    if "category_id" in product_columns:
        product_fks = {fk["name"] for fk in inspector.get_foreign_keys("products")}
        if "fk_products_category_id_product_categories" in product_fks:
            op.drop_constraint("fk_products_category_id_product_categories", "products", type_="foreignkey")
        product_indexes = {ix["name"] for ix in inspector.get_indexes("products")}
        if "ix_products_category_id" in product_indexes:
            op.drop_index("ix_products_category_id", table_name="products")
        op.drop_column("products", "category_id")
    for name in ("brand", "cas_no", "content", "series", "sort_order"):
        if name in product_columns:
            op.drop_column("products", name)

    if "product_categories" in existing_tables:
        op.drop_index("ix_product_categories_key", table_name="product_categories")
        op.drop_table("product_categories")
