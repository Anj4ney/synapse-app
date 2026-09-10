import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# Local dev defaults to a SQLite file. In production (e.g. on Vercel), Vercel's
# serverless filesystem is ephemeral, so set DATABASE_URL to a real Postgres
# connection string (Neon, Supabase, Vercel Postgres, etc.).
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./synapse.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Best-effort runtime column migration.
#
# Base.metadata.create_all() only creates TABLES that are missing entirely —
# it never adds new columns to tables that already exist. So a synapse.db
# created by an older version of the app would make every query that touches
# a new column fail with "no such column". This helper inspects the live
# schema on startup and issues a plain ALTER TABLE ... ADD COLUMN for any
# model column that is missing. It is additive-only (nothing is renamed,
# re-typed, or dropped), idempotent (skips columns that already exist), and
# best-effort (a failure logs and moves on rather than crashing the app), so
# it is safe to run on every startup against both SQLite and Postgres.
# New engagement features append their columns to this list as they are
# introduced, keeping each feature's schema change self-contained.
# ---------------------------------------------------------------------------
_COLUMN_MIGRATIONS = [
    # (table, column_name, ADD COLUMN type/defaults clause)
    ("users", "xp", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "streak_count", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_active_date", "TIMESTAMP"),
    ("users", "badges", "JSON DEFAULT '[]'"),
    ("courses", "share_id", "VARCHAR(64)"),
]

# Indexes that only make sense once their column exists (created on fresh
# databases automatically by create_all; backfilled here for older ones).
_INDEX_MIGRATIONS = [
    ("courses", "share_id",
     "CREATE UNIQUE INDEX IF NOT EXISTS ix_courses_share_id ON courses (share_id)"),
]


def ensure_schema_upgrades() -> None:
    try:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        for table, column, ddl in _COLUMN_MIGRATIONS:
            if table not in existing_tables:
                continue  # table will be created fresh by create_all()
            existing_cols = {c["name"] for c in inspector.get_columns(table)}
            if column in existing_cols:
                continue
            stmt = f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                print(f"[schema] added missing column {table}.{column}")
            except Exception as e:
                # Best effort: if this fails (e.g. permissions on a managed
                # DB), the operator should run the SQL by hand — see
                # CHANGES.md for the manual migration statements.
                print(f"[schema] could not add {table}.{column} ({e}); "
                      f"run manually: {stmt}")
        for table, column, stmt in _INDEX_MIGRATIONS:
            try:
                if table not in existing_tables:
                    continue
                cols = {c["name"] for c in inspector.get_columns(table)}
                if column not in cols:
                    continue  # column itself failed to be added
                with engine.begin() as conn:
                    conn.execute(text(stmt))
            except Exception as e:
                # Non-fatal: lookups still work without the index; uniqueness
                # is effectively guaranteed by UUIDv4 anyway.
                print(f"[schema] could not create {table}.{column} index ({e})")
    except Exception as e:
        print(f"[schema] runtime column migration skipped: {e}")
