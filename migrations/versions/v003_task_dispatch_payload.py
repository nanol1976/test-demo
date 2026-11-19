"""v003 增量：task_ledger 增加 dispatch_payload 列。

动机（Batch9 真 Worker 接入暴露的真实缺口）：
平台原来只记任务"状态"，不记任务"内容"。Master 派发报文里的 payload
（干什么、给谁、怎么执行）落不进台账，于是真 Worker 按 task_id 去查
`GET /api/tasks/{task_id}` 只能看到状态，看不到要干什么——轮询式接入直接断掉。

本列存派发报文 payload（校验后的 JSON 文本），只在派发 / 重派时写入：
- 加列、可空，老库升级后历史行保持 NULL，兼容；
- 不做业务解释，平台只做搬运，不判 payload 语义（业务无关原则）。

注意：只加列与更新，不提供删除。
"""
VERSION = 3
NOTE = "task_ledger.dispatch_payload 落派发载荷（可空，JSON 文本）"

TABLE = "task_ledger"
COLUMN = "dispatch_payload"


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    if COLUMN in existing:
        return
    conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT"))


def down(conn):
    """仅作书面参考：本框架不提供自动回滚。"""
    from sqlalchemy import text

    conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"))
