"""v011 增量：task_ledger 增加 review_of / review_verdict / review_items 三列。

动机（Batch32-1 异构复核）：
验收原来是"人一句话全通过"——标准（acceptance_criteria）写了没人用，
签收（human_confirm_resp + accept_tasks）把整条流水线无条件推成通过，
判断依据只有执行者自己写的一句 self_check。等于机器对验收零发言权。

这里只加三列，把"谁复核了、结论是什么、逐项证据"落进台账：
- `review_of`：本行是复核单时，指向被复核的 task_id（普通单为空）；
- `review_verdict`：被复核任务的结论 pass / fail / unreviewed（复核单自身为空）；
- `review_items`：逐项结论 JSON，形如 [{"id":"c1","ok":true,"evidence":"..."}]。

复核者是谁不再加列，复用 Batch31-D 的 `done_instance`（复核单回执照常落它）。
只加可空列，不重建表、不动任何既有行（台账铁律）。
幂等：列已存在则直接返回。
"""
VERSION = 11
NOTE = "task_ledger 增加 review_of / review_verdict / review_items（异构复核与逐项证据）"

TABLE = "task_ledger"
COLUMNS = [
    ("review_of", "VARCHAR(64)"),
    ("review_verdict", "VARCHAR(16)"),
    ("review_items", "TEXT"),
]


def up(conn):
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if TABLE not in set(insp.get_table_names()):
        # 全新库由 create_all 建表（含这三列），这里什么都不做
        return

    cols = {c["name"] for c in insp.get_columns(TABLE)}
    for name, ddl in COLUMNS:
        if name in cols:
            continue
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}"))
