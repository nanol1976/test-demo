"""Batch3 验收脚本：台账查询增强 + 导出视图 + 鉴权 + 契约 + 三个遗留边界。

A 流水线分页/筛选 / B 任务筛选 / C 导出视图 / D 鉴权 / E 三个边界 / F 契约与收尾
"""
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite:///./data/verify_b3.db"
os.environ["ARCHIVE_ROOT"] = "./archive/_verify_b3"
os.environ["API_TOKEN"] = "verify-token"
os.environ["BUS_CAPACITY"] = "200"
os.environ["BUS_WORKERS"] = "4"

if os.path.exists("./data/verify_b3.db"):
    os.remove("./data/verify_b3.db")
if os.path.isdir("./archive/_verify_b3"):
    shutil.rmtree("./archive/_verify_b3")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_engine, get_session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.repositories.ledger import LedgerRepository  # noqa: E402

Base.metadata.create_all(get_engine())

H = {"X-API-Token": os.environ["API_TOKEN"]}
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def body(trace_id, task_id, msg_type, payload=None, sender="master", ts=None):
    return {
        "trace_id": trace_id,
        "task_id": task_id,
        "msg_type": msg_type,
        "sender": sender,
        "timestamp": (ts or datetime.now(timezone.utc)).isoformat(),
        "version": "v1.0",
        "payload": payload or {},
    }


# ---------------------------------------------------------------- 夹具
def seed():
    db = get_session_factory()()
    try:
        repo = LedgerRepository(db)
        base = datetime.now(timezone.utc) - timedelta(hours=3)
        for i in range(25):
            tid = f"t-b3-{i:02d}"
            repo.create_pipeline(tid, f"夹具流水线 {i}")
            repo.create_task(tid, f"{tid}-a", "dsh", status="待拆解")
            if i % 5 == 0:
                repo.update_pipeline_status(tid, "success")
            elif i % 5 == 1:
                repo.update_pipeline_status(tid, "failed")
        return base
    finally:
        db.close()


with TestClient(app) as client:
    seed()

    # ------------------------------------------------------------ A 流水线分页/筛选
    print("=== A 流水线风水页与筛选 ===")
    r = client.get("/api/pipelines", params={"page": 1, "size": 10}, headers=H)
    check("列表 200", r.status_code == 200, f"status={r.status_code}")
    m = r.json()
    check("total=25", m["meta"]["total"] == 25, str(m["meta"]["total"]))
    check("首页 10 条", len(m["items"]) == 10, str(len(m["items"])))
    check("pages=3", m["meta"]["pages"] == 3, str(m["meta"]["pages"]))

    r = client.get("/api/pipelines", params={"page": 3, "size": 10}, headers=H)
    check("末页 5 条", len(r.json()["items"]) == 5, str(len(r.json()["items"])))

    r = client.get("/api/pipelines", params={"status": "success"}, headers=H)
    check("status=success 命中 5 条", r.json()["meta"]["total"] == 5, str(r.json()["meta"]["total"]))

    r = client.get("/api/pipelines", params={"status": "乱写"}, headers=H)
    check("非法状态 → 422", r.status_code == 422, f"status={r.status_code}")

    r = client.get("/api/pipelines", params={"size": 999}, headers=H)
    check("size 超上限 → 422", r.status_code == 422, f"status={r.status_code}")

    r = client.get("/api/pipelines", params={"page": 0}, headers=H)
    check("page=0 → 422", r.status_code == 422, f"status={r.status_code}")

    # ------------------------------------------------------------ B 任务筛选
    print("=== B 任务筛选 ===")
    r = client.get("/api/tasks", params={"status": "待拆解"}, headers=H)
    check("状态筛选有结果", r.json()["meta"]["total"] >= 20, str(r.json()["meta"]["total"]))
    r = client.get("/api/tasks", params={"target_worker": "dsh"}, headers=H)
    check("执行方筛选", r.json()["meta"]["total"] == 25, str(r.json()["meta"]["total"]))
    r = client.get("/api/tasks", params={"trace_id": "t-b3-00"}, headers=H)
    check("trace 筛选命中 1 条", r.json()["meta"]["total"] == 1, str(r.json()["meta"]["total"]))

    # ------------------------------------------------------------ C 导出视图
    print("=== C 导出视图 ===")
    r = client.get("/api/export/t-b3-00", params={"format": "json"}, headers=H)
    check("json 导出 200", r.status_code == 200, f"status={r.status_code}")
    check("json 含 tasks", len(r.json()["tasks"]) == 1, str(len(r.json().get("tasks", []))))
    check("导出即快照（状态一致）", r.json()["overall_status"] == "success", r.json()["overall_status"])

    r = client.get("/api/export/t-b3-00", params={"format": "csv"}, headers=H)
    check("csv 导出 200", r.status_code == 200, f"status={r.status_code}")
    check("csv Content-Type 为 text/csv", "text/csv" in r.headers.get("content-type", ""),
          r.headers.get("content-type"))
    lines = [ln for ln in r.text.strip().splitlines() if ln]
    check("csv 表头 + 1 数据行", len(lines) == 2, str(len(lines)))
    check("csv 含关键列", "remaining_retry" in lines[0] and "trace_id" in lines[0], lines[0][:40])

    r = client.get("/api/export/no-such-trace", headers=H)
    check("不存在 trace → 404", r.status_code == 404, f"status={r.status_code}")

    r = client.get("/api/export/t-b3-00", params={"format": "xml"}, headers=H)
    check("非法 format → 422", r.status_code == 422, f"status={r.status_code}")

    # ------------------------------------------------------------ D 鉴权
    print("=== D 鉴权占位 ===")
    check("健康检查不需要令牌", client.get("/api/health").status_code == 200)
    r = client.get("/api/pipelines")
    check("无令牌 → 401", r.status_code == 401, f"status={r.status_code}")
    r = client.get("/api/pipelines", headers={"X-API-Token": "wrong"})
    check("错令牌 → 401", r.status_code == 401, f"status={r.status_code}")
    r = client.post("/api/messages", json=body("t-b3-auth", "a-1", "task_dispatch",
                                               payload={"task_goal": "auth"}), headers={})
    check("投报文无令牌 → 401", r.status_code == 401, f"status={r.status_code}")
    r = client.post("/api/messages", json=body("t-b3-auth", "a-1", "task_dispatch",
                                               payload={"task_goal": "auth"}), headers=H)
    check("带令牌投报文 → 202", r.status_code == 202, f"status={r.status_code}")

    # ------------------------------------------------------------ E 三个遗留边界
    print("=== E 三个遗留边界 ===")
    r = client.post("/api/messages", json=body("t-b3-x", "t-b3-00-a", "task_heartbeat",
                                               payload={}, sender="codex"), headers=H)
    check("trace 与任务已有 trace 不一致 → 409", r.status_code == 409, f"status={r.status_code}")

    r = client.post("/api/messages", json=body("t-b3-long", "z" * 65, "task_dispatch",
                                               payload={"task_goal": "超长 id"}), headers=H)
    check("task_id 65 字符 → 422", r.status_code == 422, f"status={r.status_code}")

    # 剩余次数扣到 0 后再驳回一次，不得为负
    db = get_session_factory()()
    try:
        repo = LedgerRepository(db)
        repo.create_task("t-b3-00", "t-b3-00-retry", "dsh", max_retry=1, status="待验收")
    finally:
        db.close()
    r = client.post("/api/messages", json=body("t-b3-00", "t-b3-00-retry", "task_reject",
                                               payload={"reason": "第一次回购"}, sender="dsh"),
                    headers=H)
    check("首次驳回 202", r.status_code == 202, f"status={r.status_code}")
    t = client.get("/api/tasks/t-b3-00-retry", headers=H).json()
    check("remaining_retry 1→0", t["remaining_retry"] == 0, str(t["remaining_retry"]))
    db = get_session_factory()()
    try:
        LedgerRepository(db).update_task_status("t-b3-00-retry", "待验收")
    finally:
        db.close()
    r = client.post("/api/messages", json=body("t-b3-00", "t-b3-00-retry", "task_reject",
                                               payload={"reason": "第二次驳回"}, sender="dsh"),
                    headers=H)
    check("再驳一次仍 202（不崩溃）", r.status_code == 202, f"status={r.status_code}")
    t = client.get("/api/tasks/t-b3-00-retry", headers=H).json()
    check("remaining_retry 不为负", t["remaining_retry"] >= 0, str(t["remaining_retry"]))

    # ------------------------------------------------------------ F 契约与收尾
    print("=== F 契约与收尾 ===")
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    methods = {m.upper() for p in paths.values() for m in p}
    check("无 DELETE / PUT / PATCH", not any(x in methods for x in ("DELETE", "PUT", "PATCH")),
          str(sorted(methods)))
    post = paths["/api/messages"]["post"]
    check("POST /messages 有幂等键说明", "幂等键" in (post.get("description") or ""), "")
    required = {
        "/api/health", "/api/messages", "/api/pipelines/{trace_id}",
        "/api/tasks/{task_id}", "/api/archive/{trace_id}",
        "/api/pipelines", "/api/tasks", "/api/export/{trace_id}",
    }
    check("Batch3 必需接口齐备", required.issubset(set(paths)), str(sorted(set(paths) - required)))
    check("导出接口标注 csv 类型",
          "text/csv" in str(paths["/api/export/{trace_id}"]["get"].get("responses", {})), "")

print()
if FAILS:
    print(f"结果：FAIL {len(FAILS)} 项 -> {FAILS}")
    sys.exit(1)
print("结果：全部通过")
