"""v009 增量：worker_quota_audit 增加 operator 列。

Batch18 的审计表只记了"什么时间、对谁、从几改到几、为什么"，
没有"谁"——四个人共用一个平台时，翻审计也说不清是谁动的。

平台没有身份体系，所以这里只加一列"调用方自报的名字"（来自 X-Operator 请求头），
空字符串表示没填。它**不是**鉴权依据，只是给人看的线索，这一点写在字段注释里。

约束：只 ADD COLUMN，不重建表、不动已有行——台账铁律。
幂等：列已存在则直接返回。
"""
VERSION = 9
NOTE = "worker_quota_audit 增加 operator（操作者自报标识）"

TABLE = "worker_quota_audit"
COLUMN = "operator"


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    cols = {c["name"] for c in insp.get_columns(TABLE)}
    if COLUMN in cols:
        return
    conn.execute(text(
        f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(64) NOT NULL DEFAULT ''"
    ))
