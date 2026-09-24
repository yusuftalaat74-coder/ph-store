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

    # Directory holding the pharmacy web client (apps/ui). Empty means the
    # API serves no HTML at all, which is the default and what every test
    # runs with. A deployment that wants the app reachable from a browser
    # points this at the checked-out apps/ui and it appears under /app.
    ui_dir: str = ""

    # PH Office link (PH Office SPEC 5.9). Off by default: with it off, PH
    # Store behaves exactly as before. See rova/integrations/office/config.py.
    office_enabled: bool = False
    office_url: str = ""
    office_outbound_secret: str = ""      # Store -> Office (= PHOFFICE_INBOUND_SECRET)
    office_inbound_secret: str = ""       # Office -> Store (= PHOFFICE_OUTBOUND_SECRET)
    office_emit: bool = False             # shadow phase: emit events while not enabled

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
