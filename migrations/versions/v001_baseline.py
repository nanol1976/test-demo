"""v001 基线：三张台账表。

这是把 Batch6 时代"直接 create_all"的做法收编成迁移的第一步。
新库会走这一条；已有库（schema_version 已记 1）会跳过。
"""
VERSION = 1
NOTE = "基线三表：pipeline_ledger / task_ledger / message_archive"


def up(conn):
    from app.models import Base

    Base.metadata.create_all(conn)
