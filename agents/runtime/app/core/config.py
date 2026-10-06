"""atlas-agent-runtime — settings (env 주입: .env.local | .env.prod 는 compose env_file 로 전달)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_name: str = "atlas-agent-runtime"
    app_env: str = "local"
    db_engine: str = "sqlite"
    database_url: str = "sqlite:////app/data/app.db"
    # Atlas Agent Spec (PRD §11.13) — ATLAS_* 환경변수
    atlas_unit_module: str = "units.al_plan"
    atlas_public_url: str = "http://127.0.0.1:8000"
    atlas_manager_url: str = ""      # 비우면 자가 등록 안 함
    atlas_project: str = "default"
    atlas_deploy_env: str = "dev"     # 배포 환경 이름 (Org → Project → Env)
    atlas_driver: str = "compose"     # local | compose
    atlas_auto_migrate: bool = True


settings = Settings()
