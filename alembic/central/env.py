from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Resolve project root so `app.*` imports work regardless of where alembic is invoked from
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config.settings import settings  # noqa: E402
from app.db.central.models import Base  # noqa: E402

target_metadata = Base.metadata

config = context.config


def run_migrations_offline() -> None:
    """
    Offline mode: emit SQL to stdout without a live DB connection.
    Used by scripts/migrate_all_tenants.py --sql flag for dry-run review.
    """
    context.configure(
        url=settings.CENTRAL_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Online mode: connects to the DB and runs migrations transactionally.
    NullPool — migration is a one-shot CLI operation, not a long-running server.
    transaction_per_migration=True — each migration runs in its own transaction,
    so a failure mid-run doesn't leave the DB in a partial state.
    """
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = settings.CENTRAL_DB_URL

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
