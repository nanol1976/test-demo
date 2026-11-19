"""v004 增量：task_ledger 增加 last_heartbeat_time 列。

动机（Batch12 Worker 在线视图）：
平台原来把心跳只写进归档 JSONL，台账上没有"这个任务最近一次心跳是什么时候"。
于是"哪些执行方在线、哪些静默"只能靠逐条翻归档——看板做不出来，运维也看不见。

本列只在收到 `task_heartbeat` 时更新，可空（老库历史行为 NULL）。
有了它，"在线视图"就不需要新增 `/api/workers` 接口：按 target_worker 聚合
任务行 + 最近心跳时间即可推断，接口面继续守住 9 个。

只加列与更新，不提供删除。
"""
VERSION = 4
NOTE = "task_ledger.last_heartbeat_time 记录最近心跳（可空）"

TABLE = "task_ledger"
COLUMN = "last_heartbeat_time"


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    if COLUMN in existing:
        return
    conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} DATETIME"))


def down(conn):
    """仅作书面参考：本框架不提供自动回滚。"""
    from sqlalchemy import text

    conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"))
