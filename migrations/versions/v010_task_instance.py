"""v010 增量：task_ledger 增加 target_instance / done_instance 两列。

动机（Batch31-D 实例级路由）：
Batch31 首单暴露的真问题——派单只认 `worker_id`（身份族），三个 `workbuddy`
实例先到先得，台账里也只记 `worker_id`，事后分不清是谁干的。
- `target_instance`：这张活指定给哪个岗位（空 = 身份族内先到先得，保持老行为）；
- `done_instance`：实际由哪个岗位完成（Worker 回执带 metadata.instance_id 时落库）。

只加可空列，不重建表、不动任何既有行（台账铁律）。
幂等：列已存在则直接返回。
"""
VERSION = 10
NOTE = "task_ledger 增加 target_instance / done_instance（实例级路由与执行归属）"

TABLE = "task_ledger"
COLUMNS = [
    ("target_instance", "VARCHAR(64)"),
    ("done_instance", "VARCHAR(64)"),
]


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if TABLE not in set(insp.get_table_names()):
        # 全新库由 create_all 建表（含这两列），这里什么都不做
        return

    cols = {c["name"] for c in insp.get_columns(TABLE)}
    for name, ddl in COLUMNS:
        if name in cols:
            continue
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}"))
