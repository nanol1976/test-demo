"""R2 验收自检脚本（主审用）。"""
import sqlite3
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base
from app.schemas import Envelope
from app.services import ArchiveService
from app.repositories.ledger import LedgerRepository

DB = "data/verify_r2.db"   # Batch24：不再借用历史空库名

# Batch24：换库名后要自己建表（原来依赖那个历史空库里已经建好的结构）
import os
os.makedirs("data", exist_ok=True)
if os.path.exists(DB):
    os.remove(DB)
_engine = create_engine(f"sqlite:///./{DB}")
Base.metadata.create_all(_engine)
_engine.dispose()

print("=== A. 表结构里的约束 ===")
con = sqlite3.connect(DB)
rows = con.execute(
    "select name, sql from sqlite_master where type='table' "
    "and name in ('pipeline_ledger','task_ledger','message_archive')"
).fetchall()
for name, sql in rows:
    has_check = "CHECK" in (sql or "")
    has_uq = "UNIQUE" in (sql or "")
    print(f"{name}: CHECK={has_check} UNIQUE={has_uq}")

print("=== B. 仓储层写入 + 状态更新 ===")
engine = create_engine(f"sqlite:///./{DB}")
with Session(engine) as s:
    repo = LedgerRepository(s)
    p = repo.create_pipeline("t-repo", "验收用流水线")
    t = repo.create_task("t-repo", "t-repo-01", "dsh", depends_on=[], is_high_risk=False)
    print("pipeline:", p.trace_id, p.overall_status)
    print("task:", t.task_id, t.status, "remaining_retry=", t.remaining_retry)
    repo.update_task_status("t-repo-01", "执行中")
    print("updated:", repo.get_task("t-repo-01").status)
    repo.record_reject("t-repo-01", "结构不完整")
    print("after reject: remaining_retry=", repo.get_task("t-repo-01").remaining_retry)
    print("tasks in trace:", len(repo.list_tasks("t-repo")))

print("=== C. 非法状态被仓储层拒绝 ===")
with Session(engine) as s:
    repo = LedgerRepository(s)
    try:
        repo.update_task_status("t-repo-01", "乱写状态")
        print("FAIL 未拦截")
    except ValueError as e:
        print("OK 被拦截:", e)

print("=== D. 归档追加 + 幂等唯一约束 ===")
with Session(engine) as s:
    svc = ArchiveService(s, archive_root="./archive/_verify_r2")
    env = Envelope(
        trace_id="t-arc", task_id="t-arc-01", msg_type="task_return",
        sender="dsh", timestamp=datetime.now(timezone.utc),
    )
    r1 = svc.append_archive("t-arc", "t-arc-01", env)
    print("first append id=", r1.id)
    path = svc.get_archive_path("t-arc")
    print("jsonl:", path, "lines=", sum(1 for _ in open(path, encoding="utf-8")))
    try:
        svc.append_archive("t-arc", "t-arc-01", env)
        print("注意：同一条报文重复写入成功（唯一约束未生效）")
    except Exception as e:
        print("OK 重复报文被唯一约束拒绝:", type(e).__name__)

print("=== E. 归档目录日期为 UTC ===")
print("UTC today:", datetime.now(timezone.utc).strftime("%Y%m%d"))
sys.exit(0)
