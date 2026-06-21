from logging.config import fileConfig
from sqlalchemy import create_engine, pool
from alembic import context

from app.config import cfg
from app.models.db import Base

alembic_config = context.config

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# Never pass URL through configparser (it chokes on % from URL-encoding).
# Use cfg.db_url directly in both offline and online modes.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = cfg.db_url
    if not url:
        raise RuntimeError("DB_HOST is not set — cannot run migrations offline")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = cfg.db_url
    if not url:
        raise RuntimeError("DB_HOST is not set — cannot run migrations")
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
