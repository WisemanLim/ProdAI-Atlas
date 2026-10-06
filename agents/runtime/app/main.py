"""atlas-agent-runtime — 단위 에이전트 호스트. ATLAS_UNIT_MODULE 의 `agent` 를 A2A 서버로 띄운다."""
import importlib
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app import db
from app.core.config import settings
from atlas_agent_core import create_app

MIGRATION_LOCK = 0x41544C53  # 'ATLS' — 같은 DB 를 쓰는 워커·컨테이너의 동시 마이그레이션 직렬화


def migrate() -> None:
    if not settings.atlas_auto_migrate:
        return
    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    if ini.exists():                       # 저장소·컨테이너
        cfg = Config(str(ini))
    else:                                  # wheel 설치본 — 패키지에 내장된 atlas_migrations 사용
        import importlib.util
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(importlib.util.find_spec("atlas_migrations").origin or "").parent))
    with db.engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK})
        try:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            conn.commit()
        finally:
            if conn.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK})
                conn.commit()


unit = importlib.import_module(settings.atlas_unit_module)
app = create_app(unit.agent, engine=db.engine, public_url=settings.atlas_public_url, env=settings.app_env,
                 db_engine=settings.db_engine, manager_url=settings.atlas_manager_url or None,
                 project=settings.atlas_project, deploy_env=settings.atlas_deploy_env, driver=settings.atlas_driver,
                 on_startup=[migrate])
