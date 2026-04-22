from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool, text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.tenant.models import Base  # noqa: E402

target_metadata = Base.metadata

config = context.config


def _get_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "sqlalchemy.url is not set in the Alembic config. "
            "For per-tenant migrations, set it programmatically via "
            "alembic_cfg.set_main_option('sqlalchemy.url', <url>) before "
            "calling command.upgrade()."
        )
    return url


def _ensure_extensions(connection: Any) -> None:
    """
    Create pgvector and pg_trgm extensions before any DDL migrations.
    Must run in its own committed transaction — CREATE EXTENSION cannot run
    inside the same transaction as CREATE TABLE in some PostgreSQL versions.
    """
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    connection.commit()


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """
    Custom renderer for pgvector Vector columns.

    Without this hook, autogenerate writes `Vector(1536)` in the migration
    file but never adds the import, causing NameError at migration runtime.
    This hook adds `from pgvector.sqlalchemy import Vector` to autogen imports
    and returns the correct string representation.
    """
    if type_ == "type" and hasattr(obj, "dim"):
        autogen_context.imports.add("from pgvector.sqlalchemy import Vector")
        return f"Vector({obj.dim})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _get_url()

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _ensure_extensions(connection)

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            transaction_per_migration=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
