"""Batch10 验收脚本：越权强校验 + 重放拦截。

平台原来只校验"trace 同源"，不校验"是不是你该干的活"——任何一端拿别人的
task_id 发 heartbeat / return / reject 都能落账、甚至把别人任务改状态，还能
靠"改 timestamp 重放同一条 reject"反复扣重试次数。本批把这两条堵死。

覆盖：
A 派发越权：只有 Master 能派；非 Master 派发 → 409，且流水线/任务都不建
B 执行方越权矩阵：heartbeat / return / reject 非归属方 → 409，状态不动、归档不增
C 中止权限：master 可叫停任何任务；其它非归属方 → 409
D 合法动作不受影响：归属方 heartbeat / return / reject 全部 202
E 重放拦截：同 sender+payload、换 timestamp 重发 reject → 202 但不再扣次数、不增归档
F 重放拦截同样作用于 heartbeat（不刷归档、不伪造"还在干活"）
"""
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["DATABASE_URL"] = "sqlite:///./data/verify_b10.db"
os.environ["ARCHIVE_ROOT"] = "./archive/_verify_b10"
os.environ["API_TOKEN"] = "verify-token"
os.environ["BUS_CAPACITY"] = "200"
os.environ["BUS_WORKERS"] = "2"
os.environ["PYTHONPATH"] = ROOT

if os.path.exists("./data/verify_b10.db"):
    os.remove("./data/verify_b10.db")
if os.path.isdir("./archive/_verify_b10"):
    shutil.rmtree("./archive/_verify_b10")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_engine, get_session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.repositories.ledger import LedgerRepository  # noqa: E402

Base.metadata.create_all(get_engine())

H = {"X-API-Token": "verify-token"}
FAILS = []
T0 = datetime(2026, 11, 19, 16, 0, 0, tzinfo=timezone.utc)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def ts(offset_seconds: float = 0) -> str:
    return (T0 + timedelta(seconds=offset_seconds)).isoformat()


def env(trace_id, task_id, msg_type, sender, payload=None, offset=0):
    return {
        "trace_id": trace_id,
        "task_id": task_id,
        "msg_type": msg_type,
        "sender": sender,
        "timestamp": ts(offset),
        "version": "v1.0",
        "payload": payload or {},
    }


def post(client, body):
    return client.post("/api/messages", json=body, headers=H)


def task_of(task_id):
    db = get_session_factory()()
    try:
        return LedgerRepository(db).get_task(task_id)
    finally:
        db.close()


def rows(client, trace_id):
    r = client.get(f"/api/archive/{trace_id}", headers=H)
    return r.json()["index_rows"] if r.status_code == 200 else -1


def dispatch(client, trace_id, task_id, target_worker, offset=0):
    return post(client, env(trace_id, task_id, "task_dispatch", "master",
                            {"task_goal": f"{trace_id} 用例", "target_worker": target_worker},
                            offset))


def main() -> int:
    with TestClient(app) as client:
        print("=== A 派发越权：只有 Master 能派 ===")
        r = post(client, env("t-b10-unauth", "t-b10-unauth-1", "task_dispatch", "codex",
                             {"task_goal": "Worker 想自己派活", "target_worker": "codex"}, 0))
        check("非 Master 派发 → 409", r.status_code == 409, f"status={r.status_code} body={r.text[:120]}")
        check("越权派发连流水线都不建",
              client.get("/api/pipelines/t-b10-unauth", headers=H).status_code == 404)
        check("越权派发不建任务",
              client.get("/api/tasks/t-b10-unauth-1", headers=H).status_code == 404)

        print("=== B 执行方越权矩阵（任务归属 codex）===")
        r = dispatch(client, "t-b10-x", "t-b10-x-1", "codex", 0)
        check("合法派发 202", r.status_code == 202, f"status={r.status_code}")
        t = task_of("t-b10-x-1")
        check("任务归属 codex", t.target_worker == "codex", t.target_worker)
        before = rows(client, "t-b10-x")
        check("派发后归档 1 行", before == 1, str(before))

        cases = [
            ("task_heartbeat", "dsh", {"progress": 0.5}, "心跳"),
            ("task_return", "dsh", {"output": {}}, "回传"),
            ("task_reject", "master", {"reason": "Master 假装干不了"}, "驳回"),
            ("task_abort", "workbuddy", {"reason": "路人叫停"}, "中止"),
        ]
        for i, (msg_type, sender, payload, label) in enumerate(cases):
            r = post(client, env("t-b10-x", "t-b10-x-1", msg_type, sender, payload, 10 + i))
            check(f"{label}：非归属方 {sender} → 409", r.status_code == 409,
                  f"status={r.status_code} body={r.text[:100]}")
            check(f"{label}：越权报文不落归档", rows(client, "t-b10-x") == before,
                  str(rows(client, "t-b10-x")))
            check(f"{label}：状态未被动过", task_of("t-b10-x-1").status == "执行中",
                  task_of("t-b10-x-1").status)

        print("=== C 中止权限：Master 可叫停 ===")
        r = post(client, env("t-b10-x", "t-b10-x-1", "task_abort", "master",
                             {"reason": "Master 叫停"}, 20))
        check("master 中止自己的/任何任务 → 202", r.status_code == 202, f"status={r.status_code}")
        check("中止后=执行失败", task_of("t-b10-x-1").status == "执行失败",
              task_of("t-b10-x-1").status)

        print("=== D 合法动作不受影响（任务归属 workbuddy）===")
        r = dispatch(client, "t-b10-ok", "t-b10-ok-1", "workbuddy", 30)
        check("合法派发 202", r.status_code == 202, f"status={r.status_code}")
        r = post(client, env("t-b10-ok", "t-b10-ok-1", "task_heartbeat", "workbuddy",
                             {"progress": 0.3}, 31))
        check("归属方心跳 202", r.status_code == 202, f"status={r.status_code}")
        r = post(client, env("t-b10-ok", "t-b10-ok-1", "task_return", "workbuddy",
                             {"output": {"k": "v"}}, 32))
        check("归属方回传 202", r.status_code == 202, f"status={r.status_code}")
        check("回传后=待验收", task_of("t-b10-ok-1").status == "待验收",
              task_of("t-b10-ok-1").status)
        r = post(client, env("t-b10-ok", "t-b10-ok-1", "task_reject", "workbuddy",
                             {"reason": "产出不合格，重做"}, 33))
        check("归属方驳回 202", r.status_code == 202, f"status={r.status_code}")
        t = task_of("t-b10-ok-1")
        check("驳回后=验收驳回", t.status == "验收驳回", t.status)
        check("重试次数 3→2", t.remaining_retry == 2, str(t.remaining_retry))

        print("=== E 重放拦截：换 timestamp 重发同一条 reject ===")
        r = dispatch(client, "t-b10-r", "t-b10-r-1", "codex", 40)
        check("合法派发 202", r.status_code == 202, f"status={r.status_code}")
        reject_body = {"reason": "缺关键字段"}
        r = post(client, env("t-b10-r", "t-b10-r-1", "task_reject", "codex", reject_body, 41))
        check("首次驳回 202", r.status_code == 202, f"status={r.status_code}")
        t = task_of("t-b10-r-1")
        check("驳回后重试 3→2", t.remaining_retry == 2, str(t.remaining_retry))
        rows_before = rows(client, "t-b10-r")

        r = post(client, env("t-b10-r", "t-b10-r-1", "task_reject", "codex", reject_body, 45))
        check("重放（同 payload、新 timestamp）仍 202", r.status_code == 202, f"status={r.status_code}")
        note = (r.json() or {}).get("note", "")
        check("响应注明是重放拦截", "重放" in note, note)
        check("status_changed=false", (r.json() or {}).get("status_changed") is False, str(r.json()))
        check("重试次数没被二次扣减（仍 2）",
              task_of("t-b10-r-1").remaining_retry == 2,
              str(task_of("t-b10-r-1").remaining_retry))
        check("重放不追加归档", rows(client, "t-b10-r") == rows_before,
              f"{rows_before} → {rows(client, 't-b10-r')}")

        print("=== F 心跳是周期报文：同内容不同时间必须照常受理 ===")
        r = dispatch(client, "t-b10-hb", "t-b10-hb-1", "workbuddy", 50)
        check("合法派发 202", r.status_code == 202, f"status={r.status_code}")
        hb = {"progress": 0.5, "message": "半程"}
        r = post(client, env("t-b10-hb", "t-b10-hb-1", "task_heartbeat", "workbuddy", hb, 51))
        check("首次心跳 202", r.status_code == 202, f"status={r.status_code}")
        rows_before = rows(client, "t-b10-hb")
        # 同 payload、新 timestamp：这是 Worker 每秒/每几秒一次的常规心跳，必须受理
        r = post(client, env("t-b10-hb", "t-b10-hb-1", "task_heartbeat", "workbuddy", hb, 55))
        check("同内容的心跳（新时间戳）照常受理", r.status_code == 202 and "重放" not in (r.json() or {}).get("note", ""),
              f"status={r.status_code} note={(r.json() or {}).get('note')}")
        check("心跳落地（归档 +1）", rows(client, "t-b10-hb") == rows_before + 1,
              f"{rows_before} → {rows(client, 't-b10-hb')}")
        # 同 payload + 同 timestamp 才是真重复，由幂等键挡
        r = post(client, env("t-b10-hb", "t-b10-hb-1", "task_heartbeat", "workbuddy", hb, 55))
        check("同时间戳重复投递被幂等挡（不追加归档）",
              r.status_code == 202 and "幂等" in (r.json() or {}).get("note", ""),
              f"status={r.status_code} note={(r.json() or {}).get('note')}")
        check("幂等不刷归档", rows(client, "t-b10-hb") == rows_before + 1,
              f"→ {rows(client, 't-b10-hb')}")

        print("=== G 篡改 payload 的重放仍按新报文走（不被误杀）===")
        rows_g = rows(client, "t-b10-hb")
        r = post(client, env("t-b10-hb", "t-b10-hb-1", "task_heartbeat", "workbuddy",
                             {"progress": 0.8, "message": "后程"}, 60))
        check("payload 不同的心跳正常受理", r.status_code == 202, f"status={r.status_code}")
        check("归档 +1", rows(client, "t-b10-hb") == rows_g + 1, str(rows(client, "t-b10-hb")))

        print("=== H 接口面与台账铁律 ===")
        spec = client.get("/openapi.json").json()
        check("接口数仍为 9", len(spec["paths"]) == 9, str(len(spec["paths"])))
        check("方法只有 GET/POST",
              {m for p in spec["paths"].values() for m in p} <= {"get", "post"},
              str({m for p in spec["paths"].values() for m in p}))

    print(f"\n结果：{'全部通过' if not FAILS else '有失败项'}（{len(FAILS)} 项失败）")
    if FAILS:
        for f in FAILS:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
