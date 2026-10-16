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


@lru_cache
def get_settings() -> Settings:
    return Settings()
