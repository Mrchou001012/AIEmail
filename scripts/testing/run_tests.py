"""Run migrations and regression tests against an isolated, empty *_test DB."""

import os
import subprocess

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def main() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    if not (url.database or "").endswith("_test"):
        raise RuntimeError("Verification requires a dedicated *_test database")
    engine = create_engine(url.set(drivername="postgresql+psycopg"))
    try:
        if inspect(engine).get_table_names():
            raise RuntimeError("Verification requires an empty database; recreate the isolated test container")
        # Exercise both a fresh installation and a populated pre-catalog upgrade.
        subprocess.run(["alembic", "upgrade", "0020"], check=True)
        assert "catalog_code" not in {column["name"] for column in inspect(engine).get_columns("products")}
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO products (code, name, unit, approved_text_key, active, created_at, updated_at) "
                "VALUES ('MIGRATION-PROBE', 'Migration probe', 'kg', 'probe', true, now(), now())"
            ))
        subprocess.run(["alembic", "upgrade", "head"], check=True)
        subprocess.run(["alembic", "upgrade", "head"], check=True)
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT name, catalog_code, catalog_visible FROM products WHERE code = 'MIGRATION-PROBE'"
            )).one()
            assert tuple(row) == ("Migration probe", None, False)
        print("Migration verification passed: empty -> 0020 -> populated upgrade -> head (repeatable)", flush=True)
    finally:
        engine.dispose()
    subprocess.run(["pytest", "--tb=short", "--junitxml=/app/runtime/test-results.xml"], check=True)


if __name__ == "__main__":
    main()
