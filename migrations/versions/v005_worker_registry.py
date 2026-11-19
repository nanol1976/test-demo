"""v005 增量：新增 worker_registry 表（Batch13 执行方注册表）。

Batch12 的"在线视图"是拿任务心跳反推的，代价是**空转但活着的 Worker 看不见**
（没任务就没有台账行）。本批补一张注册表：Worker 无论有没有活，都周期性上报
自己在不在、并发开多大、手上几个任务，平台据此判"在线/静默/停机"。

只建表，不删除；老库升级幂等（表已存在则跳过）。
"""
VERSION = 5
NOTE = "worker_registry（执行方注册表）建表"

TABLE = "worker_registry"


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if TABLE in set(insp.get_table_names()):
        return
    conn.execute(text(f"""
        CREATE TABLE {TABLE} (
            worker_id      VARCHAR(64)  NOT NULL PRIMARY KEY,
            status         VARCHAR(32)  NOT NULL DEFAULT 'running',
            last_seen      DATETIME     NOT NULL,
            first_seen     DATETIME     NOT NULL,
            concurrency    INTEGER      NOT NULL DEFAULT 1,
            concurrency_limit INTEGER   NOT NULL DEFAULT 1,
            in_flight      INTEGER      NOT NULL DEFAULT 0,
            handled        INTEGER      NOT NULL DEFAULT 0,
            note           TEXT,
            meta           TEXT
        )
    """))
    conn.execute(text(f"CREATE INDEX ix_{TABLE}_last_seen ON {TABLE} (last_seen)"))


def down(conn):
    """仅作书面参考：本框架不提供自动回滚。"""
    from sqlalchemy import text

    conn.execute(text(f"DROP TABLE {TABLE}"))
