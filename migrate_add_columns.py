"""
One-off migration: adds the new columns needed for delivery type,
preorder, low-stock threshold, and quantity to already-existing tables.

`create_tables.py` (Base.metadata.create_all) only creates tables that
don't exist yet — it will NOT add columns to a table you already have
rows in. Run this once after pulling the new code:

    python migrate_add_columns.py
"""

from sqlalchemy import text
from database import engine

# (table, column, DDL type)
COLUMNS = [
    ("products", "delivery_type", "VARCHAR(20) DEFAULT 'automatic'"),
    ("products", "preorder", "BOOLEAN DEFAULT FALSE"),
    ("products", "low_stock_threshold", "INT DEFAULT 3"),
    ("orders", "quantity", "INT DEFAULT 1"),
    ("orders", "delivery_type", "VARCHAR(20) DEFAULT 'automatic'"),
    ("orders", "is_preorder", "BOOLEAN DEFAULT FALSE"),
]


def column_exists(conn, table, column) -> bool:
    result = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() > 0


def main():
    with engine.begin() as conn:
        for table, column, ddl_type in COLUMNS:
            if column_exists(conn, table, column):
                print(f"⏭  {table}.{column} already exists, skipping.")
                continue

            print(f"➕ Adding {table}.{column} ...")
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
            )
            print(f"✅ Added {table}.{column}")

        # Backfill existing rows so old data doesn't have NULLs where
        # the app expects a real default.
        conn.execute(
            text(
                "UPDATE products SET delivery_type = 'automatic' "
                "WHERE delivery_type IS NULL"
            )
        )
        conn.execute(
            text(
                "UPDATE products SET low_stock_threshold = 3 "
                "WHERE low_stock_threshold IS NULL"
            )
        )
        conn.execute(
            text(
                "UPDATE orders SET quantity = 1 WHERE quantity IS NULL"
            )
        )
        conn.execute(
            text(
                "UPDATE orders SET delivery_type = 'automatic' "
                "WHERE delivery_type IS NULL"
            )
        )

    print("✅ Migration complete.")


if __name__ == "__main__":
    main()
