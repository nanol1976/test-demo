from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/mw_platform.db"
    archive_root: str = "./archive"
    log_level: str = "INFO"
    bus_capacity: int = 1000
    bus_workers: int = 2
    api_token: str = ""
    page_size_default: int = 20
    page_size_max: int = 100
    # Batch4：调度判定参数
    task_timeout_seconds: int = 3600
    heartbeat_timeout_seconds: int = 120
    failure_threshold: int = 3
    scan_interval_seconds: float = 5.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
