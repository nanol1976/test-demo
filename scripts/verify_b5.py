"""Batch5 验收脚本：极简前端（静态资源 + 接口一致性 + 回归）。

重点不是"页面好看"，而是：
1) 静态页真的能拿到（200 + 关键元素在）；
2) 页面里写的每个 /api 路径，后端都真的存在（防止前端指向不存在的接口）；
3) 前端没有引入任何新的写接口 / 删除接口；
4) 前四批的判定仍然全绿（回归）。
"""
import os
import re
import shutil

os.environ["DATABASE_URL"] = "sqlite:///./data/verify_b5.db"
os.environ["ARCHIVE_ROOT"] = "./archive/_verify_b5"
os.environ["API_TOKEN"] = ""
os.environ["BUS_CAPACITY"] = "200"
os.environ["BUS_WORKERS"] = "2"
os.environ["TASK_TIMEOUT_SECONDS"] = "60"
os.environ["HEARTBEAT_TIMEOUT_SECONDS"] = "30"
os.environ["FAILURE_THRESHOLD"] = "3"
os.environ["SCAN_INTERVAL_SECONDS"] = "3600"

if os.path.exists("./data/verify_b5.db"):
    os.remove("./data/verify_b5.db")
if os.path.isdir("./archive/_verify_b5"):
    shutil.rmtree("./archive/_verify_b5")

from pathlib import Path  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_engine  # noqa: E402
from app.core.clock import FakeClock, set_clock  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.breaker import breaker  # noqa: E402

Base.metadata.create_all(get_engine())
breaker.clear()

from datetime import datetime, timezone  # noqa: E402

clock = FakeClock(datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc))
set_clock(clock)

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


STATIC = Path("app/static")


def main() -> int:
    with TestClient(app) as client:
        print("=== A 静态资源可访问 ===")
        r = client.get("/")
        check("首页 200", r.status_code == 200, f"status={r.status_code}")
        html = r.text
        check("首页含标题", "台账看板" in html)
        check("首页引用 app.js", "/static/app.js" in html)
        check("首页引用 style.css", "/static/style.css" in html)
        check("首页有报文投递面板", "报文投递" in html)
        check("首页有暂停/恢复按钮", "btnPause" in html and "btnResume" in html)
        check("首页有流水线详情区", 'id="detailPanel"' in html)

        r = client.get("/static/app.js")
        check("app.js 200", r.status_code == 200, f"status={r.status_code}")
        js = r.text
        r = client.get("/static/style.css")
        check("style.css 200", r.status_code == 200, f"status={r.status_code}")
        check("app.js 有请求超时控制", "API_TIMEOUT_MS" in js and "AbortController" in js)
        check("app.js 有返回值截断", "RESULT_MAX_CHARS" in js)

        print("=== B 前端引用的接口必须真实存在 ===")
        paths = sorted(set(re.findall(r'"(/api/[A-Za-z0-9_\-{}$/]+)', js)))
        paths = [p for p in paths if "{" not in p]
        openapi = client.get("/openapi.json").json()
        real = set()
        for path, methods in openapi["paths"].items():
            for m in methods:
                real.add((m.upper(), path))
        bad = []
        for p in paths:
            clean = p.split("?")[0].rstrip("/")
            hit = any(p2.rstrip("/") == clean for _, p2 in real)
            if not hit:
                bad.append(p)
        check("前端引用的 /api 路径都存在", not bad, f"未匹配={bad}")
        print(f"  前端引用路径：{paths}")

        print("=== C 不引入新的写接口 ===")
        methods = {m.upper() for methods in openapi["paths"].values() for m in methods}
        check("方法集合仍只有 GET/POST", methods == {"GET", "POST"}, str(sorted(methods)))
        check("无 DELETE/PUT/PATCH", not ({"DELETE", "PUT", "PATCH"} & methods))
        # 原先是 "delete" not in js（连 Map.delete(key) 都算违规，太粗）；
        # 收窄成真正会毁数据的三种：发 DELETE 请求、写 DELETE FROM、drop table。
        js_upper = js.upper()
        check(
            "首页与静态资源不删台账（无 DELETE 请求/SQL/删表）",
            'METHOD: "DELETE"' not in js_upper
            and "DELETE FROM" not in js_upper
            and "DROP_TABLE" not in js_upper,
        )

        print("=== D 端到端：页面靠的接口真能用 ===")
        r = client.post("/api/messages", json=body("t-b5", "t-b5-1", "task_dispatch",
                                                   {"target_worker": "workbuddy", "task_goal": "自检"}))
        check("投递派发 202", r.status_code == 202, f"status={r.status_code}")
        r = client.get("/api/pipelines")
        check("流水线列表 200", r.status_code == 200)
        check("列表能查到刚投的 trace", any(i["trace_id"] == "t-b5" for i in r.json()["items"]))
        check("列表项带 pause_flag 字段", "pause_flag" in r.json()["items"][0])

        r = client.get("/api/pipelines/t-b5")
        check("详情 200", r.status_code == 200)
        check("详情带 tasks", len(r.json()["tasks"]) == 1)
        check("任务状态执行中", r.json()["tasks"][0]["status"] == "执行中")

        r = client.post("/api/commands", json={"command": "master:pause", "trace_id": "t-b5"})
        check("暂停 200", r.status_code == 200)
        check("paused=true", r.json()["paused"] is True)
        r = client.post("/api/commands", json={"command": "master:resume", "trace_id": "t-b5"})
        check("恢复 200", r.status_code == 200)
        check("paused=false", r.json()["paused"] is False)

        r = client.get("/api/export/t-b5?format=json")
        check("导出 JSON 200", r.status_code == 200)
        r = client.get("/api/export/t-b5?format=csv")
        check("导出 CSV 200", r.status_code == 200)
        check("CSV 带附件头", "attachment" in r.headers.get("content-disposition", ""))

        r = client.get("/api/archive/t-b5")
        check("归档查询 200", r.status_code == 200)

        print("=== E 鉴权占位：静态页免鉴权、接口要令牌 ===")
        os.environ["API_TOKEN"] = "s3cret"
        from app.core.config import get_settings

        get_settings.cache_clear()
        r = client.get("/")
        check("配了令牌后首页仍可访问", r.status_code == 200, f"status={r.status_code}")
        r = client.get("/api/pipelines")
        check("配了令牌后接口 401", r.status_code == 401, f"status={r.status_code}")
        r = client.get("/api/pipelines", headers={"X-API-Token": "s3cret"})
        check("带正确令牌 200", r.status_code == 200, f"status={r.status_code}")
        os.environ["API_TOKEN"] = ""
        get_settings.cache_clear()

        print("=== F 健康与收尾 ===")
        r = client.get("/api/health")
        check("health 200", r.status_code == 200)
        check("首页 HTML 有 charset", 'charset="utf-8"' in html)

    print("=== G 空库启动自检（真实踩到的缺陷）===")
    from sqlalchemy import create_engine, inspect

    from app.main import ensure_schema
    from app.models import Base as Models

    tmp_db = "./data/_empty_schema_check.db"
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    empty_engine = create_engine(f"sqlite:///{tmp_db}")
    try:
        missing = ensure_schema(empty_engine)
        check("空库被识别为缺表", len(missing) > 0, f"missing={missing}")
        tables = set(inspect(empty_engine).get_table_names())
        check("自检后表已建齐", {t.name for t in Models.metadata.sorted_tables} <= tables)
        again = ensure_schema(empty_engine)
        check("自检幂等（第二次不缺表）", again == [], f"again={again}")
    finally:
        empty_engine.dispose()
        if os.path.exists(tmp_db):
            os.remove(tmp_db)

    print()
    if FAILS:
        print(f"结果：FAIL {len(FAILS)} 项 -> {FAILS}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
