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


@lru_cache
def get_settings() -> Settings:
    return Settings()

