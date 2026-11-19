from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/mw_platform.db"
    archive_root: str = "./archive"
    log_level: str = "INFO"
    bus_capacity: int = 1000
    bus_workers: int = 2

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
