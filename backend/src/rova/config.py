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

    # Comma-separated browser origins allowed to call the API, or "*".
    #
    # The Android client loads its page from `file:///android_asset/index.html`,
    # so its `Origin` header is the literal string `null` — which only a
    # wildcard matches. That is safe here **only because** this API never
    # authenticates with cookies: the token travels in an `Authorization`
    # header the browser will not attach on its own. `allow_credentials` is
    # therefore False in `main.py`, and must stay False; turning it on while
    # origins are `*` is refused by the CORS spec anyway.
    #
    # Production is expected to set this to the real web origins.
    cors_origins: str = "*"

    def cors_origin_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
