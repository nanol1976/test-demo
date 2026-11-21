"""v012 增量：新增 plugin_audit 表（插件授权判定 / 拒单留痕）。

动机（Batch32-2 派单侧）：
派单可以带 `plugin` 字段。平台要回答「这个岗位有没有资格用这个插件」，
并且拒单必须留痕——但被拒的单不建任务行（没接的活不该占任务台账），
所以另开一张 append-only 审计表，只记判定，不进状态机。

只建表，不动 task_ledger、不动任何既有行（台账铁律）。
幂等：表已存在则直接返回（全新库由 create_all 建表）。
"""
VERSION = 12
NOTE = "新增 plugin_audit（插件授权判定 append-only 审计）"

TABLE = "plugin_audit"

DDL = """
CREATE TABLE IF NOT EXISTS plugin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at DATETIME NOT NULL,
    trace_id VARCHAR(64) NOT NULL,
    task_id VARCHAR(64),
    plugin_name VARCHAR(64) NOT NULL,
    plugin_version VARCHAR(32),
    worker_id VARCHAR(64) NOT NULL,
    instance_id VARCHAR(64) NOT NULL,
    decision VARCHAR(16) NOT NULL,
    reason TEXT
)
"""


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if TABLE in set(insp.get_table_names()):
        return
    conn.execute(text(DDL))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_plugin_audit_trace ON plugin_audit (trace_id)"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_plugin_audit_target "
        "ON plugin_audit (plugin_name, instance_id, created_at)"
    ))
