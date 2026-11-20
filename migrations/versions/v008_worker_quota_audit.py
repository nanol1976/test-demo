"""v008 增量：新建 worker_quota_audit（并发配额下发审计）。

Batch15 只把"当前期望值"写进注册表，改过几次、从几改到几、为什么改，都没有痕迹。
运维事后要回答"这个实例的配额外谁动过"，只能翻日志。

本迁移新建一张 append-only 审计表：
- 每次 master:worker-limit 落一行（worker_id + instance_id + 新值 + 旧值 + reason + 时间）；
- 不更新、不删除，与台账同一条铁律；
- 独立建表，不改注册表结构，也不动台账两表。

幂等：表已存在则直接返回。
"""
VERSION = 8
NOTE = "新建 worker_quota_audit（并发配额下发审计，append-only）"

TABLE = "worker_quota_audit"


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if TABLE in set(insp.get_table_names()):
        return
    conn.execute(text(f"""
        CREATE TABLE {TABLE} (
            id            INTEGER      PRIMARY KEY AUTOINCREMENT,
            worker_id     VARCHAR(64)  NOT NULL,
            instance_id   VARCHAR(64)  NOT NULL,
            desired_limit INTEGER      NOT NULL,
            prev_limit    INTEGER      NOT NULL DEFAULT 0,
            command       VARCHAR(64)  NOT NULL DEFAULT 'master:worker-limit',
            reason        TEXT,
            created_at    DATETIME     NOT NULL,
            CONSTRAINT ck_quota_audit_range CHECK (desired_limit >= 0 AND desired_limit <= 64)
        )
    """))
    conn.execute(text(
        f"CREATE INDEX ix_{TABLE}_target ON {TABLE} (worker_id, instance_id, created_at)"
    ))
