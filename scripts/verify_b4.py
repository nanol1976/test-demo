"""Batch4 验收脚本：超时 / 心跳告警 / 熔断 / 暂停恢复 / DAG 依赖。

时间完全可控：用 FakeClock 推进，不靠 sleep 赌时序。
"""
import os
import shutil
import sys
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite:///./data/verify_b4.db"
os.environ["ARCHIVE_ROOT"] = "./archive/_verify_b4"
os.environ["API_TOKEN"] = ""
os.environ["BUS_CAPACITY"] = "200"
os.environ["BUS_WORKERS"] = "2"
os.environ["TASK_TIMEOUT_SECONDS"] = "60"
os.environ["HEARTBEAT_TIMEOUT_SECONDS"] = "30"
os.environ["FAILURE_THRESHOLD"] = "3"
os.environ["SCAN_INTERVAL_SECONDS"] = "3600"

if os.path.exists("./data/verify_b4.db"):
    os.remove("./data/verify_b4.db")
if os.path.isdir("./archive/_verify_b4"):
    shutil.rmtree("./archive/_verify_b4")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_engine, get_session_factory  # noqa: E402
from app.core.clock import FakeClock, set_clock  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.repositories.ledger import LedgerRepository  # noqa: E402
from app.services.breaker import breaker  # noqa: E402
from app.services.scheduler import run_once  # noqa: E402

Base.metadata.create_all(get_engine())

T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
clock = FakeClock(T0)
set_clock(clock)
breaker.clear()

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
        "timestamp": (ts or clock.now()).isoformat(),
        "version": "v1.0",
        "payload": payload or {},
    }


def scan():
    db = get_session_factory()()
    try:
        return run_once(db)
    finally:
        db.close()


def task_of(task_id):
    db = get_session_factory()()
    try:
        return LedgerRepository(db).get_task(task_id)
    finally:
        db.close()


with TestClient(app) as client:
    # ------------------------------------------------------------ A 派发超时
    print("=== A 派发超时判定 ===")
    r = client.post("/api/messages", json=body("t-b4-a", "t-b4-a-1", "task_dispatch",
                                              payload={"task_goal": "超时用例"}))
    check("派发 202", r.status_code == 202, f"status={r.status_code}")
    check("派发后状态执行中", task_of("t-b4-a-1").status == "执行中",
          task_of("t-b4-a-1").status)

    clock.advance(40)
    rep = scan()
    check("40s 未超时（阈值 60s）", rep.timed_out == [], str(rep.timed_out))
    check("状态仍执行中", task_of("t-b4-a-1").status == "执行中", task_of("t-b4-a-1").status)

    clock.advance(30)  # 累计 70s > 60s
    rep = scan()
    check("70s 判超时", rep.timed_out == ["t-b4-a-1"], str(rep.timed_out))
    t = task_of("t-b4-a-1")
    check("超时后状态执行失败", t.status == "执行失败", t.status)
    check("abort_reason 标注 [超时]", (t.abort_reason or "").startswith("[超时]"),
          (t.abort_reason or "")[:24])

    rep2 = scan()
    check("超时只处理一次（幂等）", rep2.timed_out == [], str(rep2.timed_out))

    # ------------------------------------------------------------ B 心跳告警
    print("=== B 心跳超时告警（只告警不改状态）===")
    r = client.post("/api/messages", json=body("t-b4-b", "t-b4-b-1", "task_dispatch",
                                              payload={"task_goal": "心跳用例",
                                                       "target_worker": "codex"}))
    check("派发 202", r.status_code == 202, f"status={r.status_code}")
    clock.advance(10)
    r = client.post("/api/messages", json=body("t-b4-b", "t-b4-b-1", "task_heartbeat",
                                              payload={"progress": 0.5}, sender="codex"))
    check("心跳 202", r.status_code == 202, f"status={r.status_code}")

    clock.advance(20)
    rep = scan()
    check("距最后心跳 20s（阈值 30s）不告警", rep.heartbeat_alerted == [],
          str(rep.heartbeat_alerted))

    clock.advance(20)  # 距最后心跳 40s > 30s，但距派发 50s < 60s 超时
    rep = scan()
    check("距最后心跳 40s 触发告警", rep.heartbeat_alerted == ["t-b4-b-1"],
          str(rep.heartbeat_alerted))
    t = task_of("t-b4-b-1")
    check("告警后状态不变（仍执行中）", t.status == "执行中", t.status)
    check("abort_reason 标注 [心跳告警]", (t.abort_reason or "").startswith("[心跳告警]"),
          (t.abort_reason or "")[:28])

    # ------------------------------------------------------------ C 熔断
    print("=== C 熔断 ===")
    db = get_session_factory()()
    try:
        repo = LedgerRepository(db)
        repo.create_pipeline("t-b4-c", "熔断用例")
        for i in range(3):
            repo.create_task("t-b4-c", f"t-b4-c-{i}", "codex", status="执行中")
            repo.update_task_status(f"t-b4-c-{i}", "执行失败")
    finally:
        db.close()
    rep = scan()
    check("失败 3 个达阈值触发熔断", rep.tripped == ["t-b4-c"], str(rep.tripped))
    check("熔断表已打开", breaker.is_open("t-b4-c"))
    p = client.get("/api/pipelines/t-b4-c").json()
    check("pipeline 置 failed", p["overall_status"] == "failed", p["overall_status"])

    r = client.post("/api/messages", json=body("t-b4-c", "t-b4-c-new", "task_dispatch",
                                              payload={"task_goal": "熔断后新派发"}))
    check("熔断后新派发 → 409", r.status_code == 409, f"status={r.status_code}")
    check("409 提示含熔断", "熔断" in r.text, r.text[:60])

    # ------------------------------------------------------------ D 暂停 / 恢复
    print("=== D 暂停与恢复 ===")
    r = client.post("/api/messages", json=body("t-b4-d", "t-b4-d-1", "task_dispatch",
                                              payload={"task_goal": "暂停用例"}))
    check("先正常派发 202", r.status_code == 202, f"status={r.status_code}")

    r = client.post("/api/commands", json={"command": "master:pause", "trace_id": "t-b4-d",
                                           "reason": "人工介入"})
    check("pause 200", r.status_code == 200, f"status={r.status_code}")
    check("paused=true", r.json()["paused"] is True, str(r.json()))

    r = client.post("/api/messages", json=body("t-b4-d", "t-b4-d-2", "task_dispatch",
                                              payload={"task_goal": "暂停期间派发"}))
    check("暂停期间派发 → 409", r.status_code == 409, f"status={r.status_code}")
    check("409 提示含暂停", "暂停" in r.text, r.text[:60])

    r = client.post("/api/commands", json={"command": "master:resume", "trace_id": "t-b4-d"})
    check("resume 200", r.status_code == 200, f"status={r.status_code}")
    check("paused=false", r.json()["paused"] is False, str(r.json()))

    r = client.post("/api/messages", json=body("t-b4-d", "t-b4-d-2", "task_dispatch",
                                              payload={"task_goal": "恢复后派发"}))
    check("恢复后派发 202", r.status_code == 202, f"status={r.status_code}")

    r = client.post("/api/commands", json={"command": "master:乱写", "trace_id": "t-b4-d"})
    check("未知指令 → 400", r.status_code == 400, f"status={r.status_code}")
    r = client.post("/api/commands", json={"command": "master:pause", "trace_id": "no-such"})
    check("不存在 trace → 404", r.status_code == 404, f"status={r.status_code}")

    # ------------------------------------------------------------ E DAG 依赖
    print("=== E DAG 依赖判定 ===")
    r = client.post("/api/messages", json=body("t-b4-e", "t-b4-e-a", "task_dispatch",
                                              payload={"task_goal": "上游任务"}))
    check("上游派发 202", r.status_code == 202, f"status={r.status_code}")

    r = client.post("/api/messages", json=body("t-b4-e", "t-b4-e-b", "task_dispatch",
                                              payload={"task_goal": "下游任务",
                                                       "depends_on": ["t-b4-e-a"]}))
    check("依赖未满足（上游执行中）→ 409", r.status_code == 409, f"status={r.status_code}")
    check("409 列出未满足依赖", "t-b4-e-a" in r.text, r.text[:80])

    db = get_session_factory()()
    try:
        LedgerRepository(db).update_task_status("t-b4-e-a", "验收通过")
    finally:
        db.close()
    r = client.post("/api/messages", json=body("t-b4-e", "t-b4-e-b", "task_dispatch",
                                              payload={"task_goal": "下游任务",
                                                       "depends_on": ["t-b4-e-a"]}))
    check("依赖满足后派发 202", r.status_code == 202, f"status={r.status_code}")
    check("下游任务执行中", task_of("t-b4-e-b").status == "执行中", task_of("t-b4-e-b").status)

    # ------------------------------------------------------------ F 收尾
    print("=== F 收尾 ===")
    check("health 200", client.get("/api/health").json() == {"status": "ok"})
    spec = client.get("/openapi.json").json()
    methods = {m.upper() for p in spec["paths"].values() for m in p}
    check("无 DELETE / PUT / PATCH", not any(x in methods for x in ("DELETE", "PUT", "PATCH")),
          str(sorted(methods)))
    check("接口总数 11（Batch18 加 /api/audit）", len(spec["paths"]) == 11, str(sorted(spec["paths"])))
    check("commands 有暂停指令说明", "master:pause" in str(spec["paths"]["/api/commands"]), "")

print()
if FAILS:
    print(f"结果：FAIL {len(FAILS)} 项 -> {FAILS}")
    sys.exit(1)
print("结果：全部通过")
