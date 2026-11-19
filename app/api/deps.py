"""FastAPI dependencies for Master-Worker Platform.

engine 与 sessionmaker 必须是模块级单例：每个请求新建 engine 会造成连接池泄漏。
"""
from functools import lru_cache
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

_engine = create_engine(
    _settings.database_url,
    echo=_settings.log_level == "DEBUG",
    future=True,
)
_session_factory = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


@lru_cache(maxsize=1)
def get_engine():
    """返回全局唯一 engine（模块加载时创建，不按请求创建）。"""
    return _engine


@lru_cache(maxsize=1)
def get_session_factory():
    """返回全局唯一 sessionmaker。"""
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency：只产出 session 并保证关闭，不做连接池管理。"""
    db = _session_factory()
    try:
        yield db
    finally:
        db.close()
