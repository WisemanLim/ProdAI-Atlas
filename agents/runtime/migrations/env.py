"""Alembic env — DATABASE_URL(설정) 사용, 모델 메타데이터 자동 감지."""
from alembic import context

from app.db import engine
from atlas_agent_core.tasks import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=str(engine.url), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    shared = context.config.attributes.get("connection")  # app.main.migrate 가 잠금을 잡은 연결을 넘김
    if shared is not None:
        _run(shared)
        return
    with engine.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
