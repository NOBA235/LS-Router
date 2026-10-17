from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Reads FALKORDB_* variables from the environment or the repo-root .env."""

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_password: str | None = None  # unused in Phase 0 (no password configured)
    falkordb_graph: str = "landslide"
    falkordb_timeout_s: float = 2.0

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-sonnet-latest"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    landslide_dataset_path: str | None = None
    landslide_use_fixture: bool = False
    landslide_history_file: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


