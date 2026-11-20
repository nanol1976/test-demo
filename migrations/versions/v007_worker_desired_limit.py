"""v007 增量：worker_registry 增加 desired_limit（平台下发的期望并发上限）。

动机（Batch15 并发配额）：
  concurrency 是 Worker 自报的"我现在同时跑几个"，只有一个数时看不出
  "平台要求 4、它还按 1 跑"这种下发没生效的故障。单独一列 desired_limit：
  - 0 = 没下发过，Worker 按自己的 --concurrency 跑；
  - >0 = 平台要求的实例级上限，Worker 下次上报时读到并生效。

只加列、不重建表（SQLite 支持 ADD COLUMN，代价小且一行不丢）。
幂等：列已存在则直接返回。
"""
VERSION = 7
NOTE = "worker_registry 增加 desired_limit（平台下发期望并发上限）"

TABLE = "worker_registry"


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if TABLE not in set(insp.get_table_names()):
        return  # 全新库由模型建表时已带该列
    cols = {c["name"] for c in insp.get_columns(TABLE)}
    if "desired_limit" in cols:
        return
    conn.execute(text(
        f"ALTER TABLE {TABLE} ADD COLUMN desired_limit INTEGER NOT NULL DEFAULT 0"
    ))
