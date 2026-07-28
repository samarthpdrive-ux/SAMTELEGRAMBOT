"""
migrate_user_decimal.py

One-off migration: converts the `users` table's money columns from
FLOAT to DECIMAL(20, 8) (matching orders.amount / products.price),
and is_banned from a bare Integer flag to a real BOOLEAN.

Run this ONCE, after deploying the new models/user.py, and BEFORE the
bot process that uses it starts serving traffic:

    python migrate_user_decimal.py

(Sibling script to the existing migrate_add_columns.py, which
already handles products/orders columns — this one just covers
users.)

Why this needs an explicit migration instead of just changing the
model:
- `Base.metadata.create_all()` (create_tables.py) only creates
  tables that don't exist yet — it never ALTERs an existing table's
  column type.
- A live FLOAT column holds imprecise binary-float values (e.g. a
  balance that displays as 10.10 might actually be stored as
  10.099999999...). This migration widens the column via
  `MODIFY COLUMN ... DECIMAL(20, 8)` — MySQL/TiDB does the numeric
  conversion in-place, which is the best available fix short of
  reconstructing exact historical amounts from transaction history
  (out of scope here — this migration does not attempt to "correct"
  existing balances, only to change their column type going forward).
- BOOLEAN is a MySQL/TiDB alias for TINYINT(1), so existing 0/1
  Integer values are already valid — this is a type-label change with
  no data risk.

This script is idempotent: it checks each column's current type
before touching it, so running it twice (or against a DB that already
has these types) is a no-op.
"""

from sqlalchemy import text

from database import engine

COLUMN_CHANGES = [
    ("users", "balance", "DECIMAL(20, 8) NOT NULL DEFAULT 0"),
    ("users", "total_spent", "DECIMAL(20, 8) NOT NULL DEFAULT 0"),
    ("users", "total_deposited", "DECIMAL(20, 8) NOT NULL DEFAULT 0"),
    ("users", "referral_earnings", "DECIMAL(20, 8) NOT NULL DEFAULT 0"),
    ("users", "is_banned", "BOOLEAN NOT NULL DEFAULT FALSE"),
]


def _current_type(conn, table: str, column: str) -> str:
    result = conn.execute(
        text(
            "SELECT DATA_TYPE FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    row = result.first()
    return (row[0] or "").lower() if row else ""


def main():
    with engine.begin() as conn:
        for table, column, new_type in COLUMN_CHANGES:
            current = _current_type(conn, table, column)

            target_data_type = "decimal" if "DECIMAL" in new_type else "tinyint"

            if current == target_data_type:
                print(f"⏭  {table}.{column} is already {current}, skipping.")
                continue

            print(f"🔧 Converting {table}.{column} ({current or 'unknown'} -> {new_type}) ...")
            conn.execute(
                text(f"ALTER TABLE {table} MODIFY COLUMN {column} {new_type}")
            )
            print(f"✅ Converted {table}.{column}")

    print("✅ Migration complete.")


if __name__ == "__main__":
    main()
