"""显式迁移入口（替代"启动时自动建表"的兜底做法）。

启动自检 ensure_schema() 只是兜底，生产应显式迁移。本脚本提供：
- migrate：建表/补表，并把版本写进 schema_version
- --check：只检查，不改库（上线前预检用）

用法：
    python deploy/migrate.py            # 执行迁移
    python deploy/migrate.py --check    # 只检查
"""
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, inspect, text  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.models import Base  # noqa: E402

SCHEMA_VERSION = 1

meta = MetaData()
schema_version_tbl = Table(
    "schema_version",
    meta,
    Column("version", Integer, primary_key=True),
    Column("applied_at", String(64), nullable=False),
    Column("note", String(255)),
)


def connect():
    settings = get_settings()
    url = settings.database_url
    return create_engine(url, future=True), url


def current_version(engine) -> int:
    insp = inspect(engine)
    if "schema_version" not in insp.get_table_names():
        return 0
    with engine.connect() as conn:
        row = conn.execute(text("SELECT MAX(version) FROM schema_version")).first()
    return int(row[0]) if row and row[0] is not None else 0


def main() -> int:
    check_only = "--check" in sys.argv
    engine, url = connect()
    print(f"数据库：{url}")

    tables_needed = [t.name for t in Base.metadata.sorted_tables]
    existing = set(inspect(engine).get_table_names())
    missing = [t for t in tables_needed if t not in existing]
    ver = current_version(engine)

    print(f"期望表：{tables_needed}")
    print(f"现有表：{sorted(existing)}")
    print(f"schema_version：{ver}（目标 {SCHEMA_VERSION}）")

    if check_only:
        ok = not missing and ver >= SCHEMA_VERSION
        print(f"预检结果：{'通过' if ok else '不通过'}")
        if not ok:
            print(f"  缺表：{missing}；版本落后：{ver} < {SCHEMA_VERSION}")
            return 1
        return 0

    if missing:
        print(f"→ 补建缺失表：{missing}")
        Base.metadata.create_all(engine)
    meta.create_all(engine)

    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT COUNT(1) FROM schema_version WHERE version = :v"), {"v": SCHEMA_VERSION}
        ).scalar()
        if not exists:
            conn.execute(
                text("INSERT INTO schema_version (version, applied_at, note) VALUES (:v, :a, :n)"),
                {"v": SCHEMA_VERSION, "a": now, "n": "初始建表（Batch6 固化）"},
            )
            print(f"→ 记录 schema_version = {SCHEMA_VERSION}")

    after = set(inspect(engine).get_table_names())
    still = [t for t in tables_needed if t not in after]
    print(f"迁移完成，缺表：{still or '无'}")
    return 0 if not still else 1


if __name__ == "__main__":
    raise SystemExit(main())
