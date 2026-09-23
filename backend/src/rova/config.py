"""A2.10 — environment variables only, read once, prefix ROVA_."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROVA_")

    env: str = "dev"  # dev | test | production
    database_url: str
    jwt_secret: str
    storage_dir: str = "./var/storage"
    index_client: str = "file"  # file | http
    index_fixture_path: str = "src/rova/seed/fixtures/index_products.csv"
    index_event_secret: str = ""
    log_level: str = "INFO"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
