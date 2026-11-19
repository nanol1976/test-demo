"""v006 增量：worker_registry 增加 instance_id，主键改为 (worker_id, instance_id)。

动机（Batch14 多实例）：
Batch13 的注册表用 worker_id 做唯一键，同一台 worker_id 的两台机器会互相覆盖
last_seen —— 谁后报谁算"在线"，另一台被顶成静默，看板上分辨不出来。

本迁移把主键换成 (worker_id, instance_id)：
- 老数据（Batch13 时期没有 instance_id）统一补 'default'，不丢任何一行；
- 表结构重建在 SQLite 上必须走"建新表 → 拷贝 → 换名"，所以这里显式做一次；
- worker_registry 是运行态注册表，不是任务台账，不受"台账只增不删"约束；
  但迁移仍然**一行不丢**，只是给每行补了 instance_id。

幂等：列已存在则直接返回。
"""
VERSION = 6
NOTE = "worker_registry 增加 instance_id，主键改为 (worker_id, instance_id)"

TABLE = "worker_registry"
NEW = "worker_registry_v6"


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if TABLE not in set(insp.get_table_names()):
        # 全新库：直接按新结构建表（与模型一致）
        conn.execute(text(f"""
            CREATE TABLE {TABLE} (
                worker_id      VARCHAR(64)  NOT NULL,
                instance_id    VARCHAR(64)  NOT NULL DEFAULT 'default',
                status         VARCHAR(32)  NOT NULL DEFAULT 'running',
                last_seen      DATETIME     NOT NULL,
                first_seen     DATETIME     NOT NULL,
                concurrency    INTEGER      NOT NULL DEFAULT 1,
                concurrency_limit INTEGER  NOT NULL DEFAULT 1,
                in_flight      INTEGER      NOT NULL DEFAULT 0,
                handled        INTEGER      NOT NULL DEFAULT 0,
                note           TEXT,
                meta           TEXT,
                PRIMARY KEY (worker_id, instance_id)
            )
        """))
        conn.execute(text(f"CREATE INDEX ix_{TABLE}_last_seen ON {TABLE} (last_seen)"))
        return

    cols = {c["name"] for c in insp.get_columns(TABLE)}
    if "instance_id" in cols:
        return

    conn.execute(text(f"""
        CREATE TABLE {NEW} (
            worker_id      VARCHAR(64)  NOT NULL,
            instance_id    VARCHAR(64)  NOT NULL DEFAULT 'default',
            status         VARCHAR(32)  NOT NULL DEFAULT 'running',
            last_seen      DATETIME     NOT NULL,
            first_seen     DATETIME     NOT NULL,
            concurrency    INTEGER      NOT NULL DEFAULT 1,
            concurrency_limit INTEGER  NOT NULL DEFAULT 1,
            in_flight      INTEGER      NOT NULL DEFAULT 0,
            handled        INTEGER      NOT NULL DEFAULT 0,
            note           TEXT,
            meta           TEXT,
            PRIMARY KEY (worker_id, instance_id)
        )
    """))
    # 老库里这一列可能叫 limit（SQL 关键字，模型建表时带了引号）或 concurrency_limit
    limit_col = '"limit"' if "limit" in cols else "concurrency_limit"
    conn.execute(text(f"""
        INSERT INTO {NEW}
            (worker_id, instance_id, status, last_seen, first_seen,
             concurrency, concurrency_limit, in_flight, handled, note, meta)
        SELECT worker_id, 'default', status, last_seen, first_seen,
               concurrency, {limit_col}, in_flight, handled, note, meta
        FROM {TABLE}
    """))
    conn.execute(text(f"DROP TABLE {TABLE}"))
    conn.execute(text(f"ALTER TABLE {NEW} RENAME TO {TABLE}"))
    conn.execute(text(f"CREATE INDEX ix_{TABLE}_last_seen ON {TABLE} (last_seen)"))
