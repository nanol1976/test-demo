"""v002 增量：给 message_archive.msg_type 建索引。

动机：报文类型统计（按 8 种 msg_type 计数）与排查"某类报文没收到"时，
全表扫会随归档量增长变慢。msg_type 基数只有 8，索引很小。

这是第一个"真正的增量"迁移，用来验证迁移框架能在已有库上增量演进。
"""
VERSION = 2
NOTE = "message_archive.msg_type 加索引 ix_message_archive_msg_type"

INDEX_NAME = "ix_message_archive_msg_type"
TABLE = "message_archive"
COLUMN = "msg_type"


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    existing = {idx["name"] for idx in insp.get_indexes(TABLE)}
    if INDEX_NAME in existing:
        return
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON {TABLE} ({COLUMN})"))


def down(conn):
    """仅作书面参考：本框架不提供自动回滚。"""
    from sqlalchemy import text

    conn.execute(text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
