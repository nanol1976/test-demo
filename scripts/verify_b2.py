"""Batch2 验收脚本：总线 + 报文路由 + 状态迁移 + 归档一致性 + 背压/并发。

五组自检：A 主链路 / B 状态迁移全表 / C 非法输入四种 / D 幂等与归档一致 / E 背压与并发
"""
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

# 独立库与归档目录，避免污染主库
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_b2.db"
os.environ["ARCHIVE_ROOT"] = "./archive/_verify_b2"
os.environ["BUS_CAPACITY"] = "200"
os.environ["BUS_WORKERS"] = "4"

if os.path.exists("./data/verify_b2.db"):
    os.remove("./data/verify_b2.db")
if os.path.isdir("./archive/_verify_b2"):
    shutil.rmtree("./archive/_verify_b2")

from fastapi.testclient import TestClient  # noqa: E402

from app.api import routes_messages  # noqa: E402
from app.api.deps import get_engine, get_session_factory  # noqa: E402
from app.bus import MessageBus  # noqa: E402
from app.bus.router import HandlerResult  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.repositories.ledger import LedgerRepository  # noqa: E402

Base.metadata.create_all(get_engine())

FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def post(client, body):
    return client.post("/api/messages", json=body)


def env(trace_id, task_id, msg_type, sender="master", payload=None, ts=None):
    return {
        "trace_id": trace_id,
        "task_id": task_id,
        "msg_type": msg_type,
        "sender": sender,
        "timestamp": (ts or datetime.now(timezone.utc)).isoformat(),
        "version": "v1.0",
        "payload": payload or {},
    }


def archive_rows(trace_id):
    from app.models import MessageArchive
    from sqlalchemy import select

    db = get_session_factory()()
    try:
        return len(db.execute(select(MessageArchive).where(MessageArchive.trace_id == trace_id)).scalars().all())
    finally:
        db.close()


def jsonl_lines(trace_id):
    from app.services import ArchiveService

    db = get_session_factory()()
    try:
        path = ArchiveService(db).get_archive_path(trace_id)
    finally:
        db.close()
    if not path or not path.exists():
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for _ in f if _.strip())


with TestClient(app) as client:
    # ------------------------------------------------------------ A 主链路
    print("=== A 主链路：一条 dispatch 报文驱动任务 ===")
    body = env("t-b2-a", "t-b2-a-01", "task_dispatch",
               payload={"task_goal": "验证主链路", "max_retry": 3,
                        "target_worker": "codex"})
    r = post(client, body)
    check("POST /api/messages 返回 202", r.status_code == 202, f"status={r.status_code}")
    data = r.json()
    check("返回 archive_id", isinstance(data.get("archive_id"), int), str(data.get("archive_id")))
    check("status_changed=True", data.get("status_changed") is True)

    p = client.get("/api/pipelines/t-b2-a")
    check("pipeline 自动建立", p.status_code == 200, f"status={p.status_code}")
    if p.status_code == 200:
        pv = p.json()
        check("pipeline 初始状态 pending", pv["overall_status"] == "pending", pv["overall_status"])
        check("trace 内有 1 个任务", len(pv["tasks"]) == 1, str(len(pv["tasks"])))
        check("任务状态=执行中", pv["tasks"][0]["status"] == "执行中", pv["tasks"][0]["status"])
        check("dispatch_time 非空", pv["tasks"][0]["dispatch_time"] is not None)
    check("归档索引 1 行", archive_rows("t-b2-a") == 1, str(archive_rows("t-b2-a")))

    # ------------------------------------------------------------ B 状态迁移全表
    print("=== B 状态迁移全表 ===")
    r = post(client, env("t-b2-a", "t-b2-a-01", "task_heartbeat", sender="codex",
                         payload={"progress": 0.4, "message": "半程"}))
    check("heartbeat 202", r.status_code == 202, f"status={r.status_code}")
    check("heartbeat 不改状态", r.json().get("status_changed") is False)

    r = post(client, env("t-b2-a", "t-b2-a-01", "task_return", sender="codex",
                         payload={"output": {"k": "v"}}))
    check("task_return 202", r.status_code == 202, f"status={r.status_code}")
    t = client.get("/api/tasks/t-b2-a-01").json()
    check("回传后=待验收", t["status"] == "待验收", t["status"])
    check("return_time 非空", t["return_time"] is not None)

    r = post(client, env("t-b2-a", "t-b2-a-01", "task_reject", sender="codex",
                         payload={"reason": "缺关键字段"}))
    check("task_reject 202", r.status_code == 202, f"status={r.status_code}")
    t = client.get("/api/tasks/t-b2-a-01").json()
    check("驳回后=验收驳回", t["status"] == "验收驳回", t["status"])
    check("remaining_retry 3→2", t["remaining_retry"] == 2, str(t["remaining_retry"]))
    check("驳回原因落库", t["reject_reason"] == "缺关键字段", str(t["reject_reason"]))

    r = post(client, env("t-b2-a", "t-b2-a-01", "task_abort", payload={"reason": "超范围"}))
    check("task_abort 202", r.status_code == 202, f"status={r.status_code}")
    t = client.get("/api/tasks/t-b2-a-01").json()
    check("中止后=执行失败", t["status"] == "执行失败", t["status"])
    p = client.get("/api/pipelines/t-b2-a").json()
    check("pipeline=aborted", p["overall_status"] == "aborted", p["overall_status"])

    # ------------------------------------------------------------ C 非法输入
    print("=== C 非法输入四种 ===")
    before = archive_rows("t-b2-a")
    r = post(client, env("t-b2-bad", "x-1", "task_unknown"))
    check("未知 msg_type → 400", r.status_code == 400, f"status={r.status_code}")
    check("400 未落库", archive_rows("t-b2-a") == before)

    r = post(client, {
        "trace_id": "t-b2-c1", "task_id": "t-b2-c1-01", "msg_type": "task_dispatch",
        "sender": "master", "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {},
    })
    check("缺 task_goal → 422", r.status_code == 422, f"status={r.status_code}")

    r = post(client, {
        "trace_id": "t-b2-c2", "task_id": "t-b2-c2-01", "msg_type": "task_dispatch",
        "sender": "master", "timestamp": "2026-11-19T10:00:00",
        "payload": {"task_goal": "naive"},
    })
    check("naive timestamp → 422", r.status_code == 422, f"status={r.status_code}")

    # 造一个「待拆解」任务，对它发 return，必须 409 且状态不动
    db = get_session_factory()()
    try:
        repo = LedgerRepository(db)
        if repo.get_pipeline("t-b2-c3") is None:
            repo.create_pipeline("t-b2-c3", "待拆解场景")
        repo.create_task("t-b2-c3", "t-b2-c3-01", "codex", status="待拆解")
    finally:
        db.close()
    r = post(client, env("t-b2-c3", "t-b2-c3-01", "task_return", sender="codex",
                         payload={"output": {}}))
    check("待拆解任务 task_return → 409", r.status_code == 409, f"status={r.status_code}")
    t = client.get("/api/tasks/t-b2-c3-01").json()
    check("409 后状态未变（仍待拆解）", t["status"] == "待拆解", t["status"])

    # ------------------------------------------------------------ D 幂等与归档一致
    print("=== D 幂等与归档一致性 ===")
    ts = datetime.now(timezone.utc)
    dup = env("t-b2-d", "t-b2-d-01", "task_dispatch",
              payload={"task_goal": "幂等验证"}, ts=ts)
    r1 = post(client, dup)
    r2 = post(client, dup)
    check("首次 202", r1.status_code == 202, f"status={r1.status_code}")
    check("重投 202（不报错）", r2.status_code == 202, f"status={r2.status_code}")
    check("两次 archive_id 相同",
          r1.json().get("archive_id") == r2.json().get("archive_id"),
          f"{r1.json().get('archive_id')} vs {r2.json().get('archive_id')}")
    check("索引仍 1 行", archive_rows("t-b2-d") == 1, str(archive_rows("t-b2-d")))
    check("JSONL 仍 1 行", jsonl_lines("t-b2-d") == 1, str(jsonl_lines("t-b2-d")))

    base = datetime.now(timezone.utc)
    ok = 0
    for i in range(20):
        r = post(client, env("t-b2-d20", f"t-b2-d20-{i:02d}", "task_dispatch",
                             payload={"task_goal": f"批量 {i}"},
                             ts=base + timedelta(seconds=i)))
        if r.status_code == 202:
            ok += 1
    check("20 条全部 202", ok == 20, f"成功 {ok}/20")
    check("索引 20 行", archive_rows("t-b2-d20") == 20, str(archive_rows("t-b2-d20")))
    check("JSONL 20 行", jsonl_lines("t-b2-d20") == 20, str(jsonl_lines("t-b2-d20")))
    a = client.get("/api/archive/t-b2-d20").json()
    check("归档接口行数一致", a["index_rows"] == 20, str(a["index_rows"]))
    check("归档接口给出 JSONL 路径", bool(a["jsonl_path"]), str(a["jsonl_path"]))

    # ------------------------------------------------------------ E 背压与并发
    print("=== E 背压与并发 ===")
    from collections import deque

    bus_unit = MessageBus(capacity=2, workers=1)
    raises = 0
    for i in range(5):
        try:
            bus_unit.publish("t-b2-e", type("Dummy", (), {
                "trace_id": "t-b2-e", "task_id": f"e-{i}", "msg_type": type("M", (), {"value": "task_heartbeat"})(),
                "sender": type("S", (), {"value": "codex"})(),
                "model_dump": lambda self: {},
            })())
        except Exception as exc:
            if type(exc).__name__ == "BusBackpressureError":
                raises += 1
    check("容量 2 投 5 条：至少 3 条被明确拒绝", raises >= 3, f"拒绝 {raises} 条")
    check("队列内仍保留 2 条（未丢消息）", bus_unit.pending("t-b2-e") == 2,
          str(bus_unit.pending("t-b2-e")))

    # HTTP 侧 503：容量 1 + 消费端阻塞；第 5 条必然撞满队列
    gate = threading.Event()

    def blocking_consumer(envelope):
        gate.wait(5)
        return HandlerResult(0, False, "blocked")

    bus_slow = MessageBus(capacity=1, workers=1, consumer=blocking_consumer, result_timeout=1.0)
    bus_slow.start()
    routes_messages.configure(bus_slow, routes_messages.build_message_router())

    results = []

    def send(idx):
        r = post(client, env("t-b2-e2", f"e2-{idx}", "task_heartbeat",
                             sender="codex", payload={}))
        results.append(r)

    senders = [threading.Thread(target=send, args=(i,), daemon=True) for i in range(5)]
    for th in senders:
        th.start()
        time.sleep(0.05)
    for th in senders:
        th.join(timeout=4)

    codes = [r.status_code for r in results]
    busy = [r for r in results if r.status_code == 503]
    check("队列满时 HTTP 返回 503", len(busy) >= 1, str(codes))
    retry_after = [r.headers.get("retry-after") for r in busy]
    check("503 带 Retry-After 提示（背压路径）", any(v is not None for v in retry_after), str(retry_after))
    gate.set()
    bus_slow.stop()

    # 并发 20 线程投递
    bus_conc = MessageBus(capacity=50, workers=4, consumer=routes_messages._consume,
                          result_timeout=10.0)
    bus_conc.start()
    routes_messages.configure(bus_conc, routes_messages.build_message_router())
    errors = deque()
    base2 = datetime.now(timezone.utc)

    def worker(i):
        try:
            payload = env("t-b2-conc", f"t-b2-conc-{i:02d}", "task_dispatch",
                          payload={"task_goal": f"并发 {i}"},
                          ts=base2 + timedelta(seconds=i))
            r = post(client, payload)
            if r.status_code != 202:
                errors.append(f"{i}:{r.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{i}:{type(exc).__name__}:{exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=15)
    check("并发 20 条无异常", len(errors) == 0, str(list(errors)[:3]))

    def wait_count(fn, expected, timeout=8.0):
        """等到计数到位（JSONL/索引的落盘顺序不保证与请求返回同刻，Windows 上更明显）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if fn() == expected:
                return expected
            time.sleep(0.2)
        return fn()

    check("并发索引 20 行", wait_count(lambda: archive_rows("t-b2-conc"), 20) == 20,
          str(archive_rows("t-b2-conc")))
    check("并发 JSONL 20 行", wait_count(lambda: jsonl_lines("t-b2-conc"), 20) == 20,
          str(jsonl_lines("t-b2-conc")))
    bus_conc.stop()

    # ------------------------------------------------------------ 收尾校验
    print("=== F 收尾：健康检查与无删除路径 ===")
    check("health 仍 200", client.get("/api/health").json() == {"status": "ok"})
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    methods = {m.upper() for p in paths.values() for m in p}
    check("无 DELETE / PUT / PATCH",
          not any(m in methods for m in ("DELETE", "PUT", "PATCH")), str(sorted(methods)))

print()
if FAILS:
    print(f"结果：FAIL {len(FAILS)} 项 -> {FAILS}")
    sys.exit(1)
print("结果：全部通过")
