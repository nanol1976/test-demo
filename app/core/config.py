from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Batch24：默认库地址改回 mvp.db。原先默认指向一个"结构建好、一行数据都没有"的历史空库，
    # 手敲 uvicorn（不带 DATABASE_URL）就会连过去：看板不报错、数据全空，第一反应永远是"数据被删了"。
    database_url: str = "sqlite:///./data/mvp.db"
    archive_root: str = "./archive"
    log_level: str = "INFO"
    bus_capacity: int = 1000
    bus_workers: int = 2
    api_token: str = ""
    # Batch28：只读口令。给手机／外发查看用，只能读台账，不能下发指令。
    # 留空 = 不启用只读档（配了这一枚就只认「全权」那一枚）。
    api_token_readonly: str = ""
    page_size_default: int = 20
    page_size_max: int = 100
    # Batch4：调度判定参数
    task_timeout_seconds: int = 3600
    heartbeat_timeout_seconds: int = 120
    failure_threshold: int = 3
    scan_interval_seconds: float = 5.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
