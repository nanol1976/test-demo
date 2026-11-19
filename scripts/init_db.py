#!/usr/bin/env python3
"""建表脚本（仅结构，不写任何数据）。

为什么没有 --demo：
演示数据必须走 `POST /api/messages` 造——那样才会经过状态机、幂等、归档。
绕过平台直接 INSERT 台账等于越权，而且会污染真实台账（早期版本有这个分支，已删除）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, inspect

from app.core.config import get_settings
from app.models import Base


def init_database() -> bool:
    settings = get_settings()
    database_url = settings.database_url

    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(database_url, echo=settings.log_level == "DEBUG")
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"Tables: {tables}")
    for table in tables:
        print(f"Indexes for {table}: {inspector.get_indexes(table)}")
    print("Database initialization completed successfully!")
    return True


if __name__ == "__main__":
    unknown = [a for a in sys.argv[1:] if a.startswith("-")]
    if unknown:
        print(f"不支持的参数：{unknown}")
        print("本脚本只建表、不写数据。演示数据请走 POST /api/messages（经状态机与归档）。")
        sys.exit(1)
    try:
        init_database()
    except Exception as e:  # noqa: BLE001 - 建表失败要给出完整栈
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
