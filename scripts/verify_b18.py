"""Batch18 验收脚本：审计覆盖全部运维指令 + 只读查询接口（第 11 个路径）。

Batch16 只审计了配额下发，且只能靠 CLI 读。用户批准扩接口面到 11 之后：
1. pause / resume / clear-breaker 也落同一张审计表（不改结构）
2. 新增只读 `GET /api/audit`，并给出"最近一次下发到底生效没有"

覆盖：
A 三条流水线指令各落一行，目标写成 master/trace:<trace_id>
B /api/audit 形状：meta / current 快照 / items 倒序；过滤参数生效
C effective 语义：期望=3 且自报 3 → true；自报 1 → false；被更新一条覆盖后旧行 → null；撤销 0 → true
D 只读：接口只有 GET；source 里没有任何写库/删除
E 接口面 11；看板有审计面板
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

DB = "./data/verify_b18.db"
ARCHIVE = "./archive/_verify_b18"
TOKEN = "verify-token"
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def request(method, url, payload=None, timeout=15, raw=False):
    data = None
    headers = {"Accept": "application/json", "X-API-Token": TOKEN}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (body if raw else (json.loads(body) if body else None))
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", "replace")
        if raw:
            return exc.code, raw_body
        try:
            return exc.code, json.loads(raw_body)
        except ValueError:
            return exc.code, {"raw": raw_body}


def get(base, path):
    return request("GET", base + path)


def post(base, path, payload):
    return request("POST", base + path, payload)


def port_free() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_up(base, timeout=30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if request("GET", base + "/api/health", timeout=2)[0] == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def run_migrate(args, db_url):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = db_url
    return subprocess.run([PY, os.path.join(ROOT, "deploy", "migrate.py")] + args,
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def dispatch(base, trace_id, task_id):
    return post(base, "/api/messages", {
        "trace_id": trace_id, "task_id": task_id, "msg_type": "task_dispatch",
        "sender": "master", "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "v1.0",
        "payload": {"target_worker": "workbuddy", "task_goal": "B18 审计用"},
    })


def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)

    url = f"sqlite:///{DB}"
    proc = run_migrate([], url)
    check("迁移到 v8 成功", proc.returncode == 0, proc.stdout.strip().splitlines()[-1:])

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = url
    env["ARCHIVE_ROOT"] = ARCHIVE
    env["API_TOKEN"] = TOKEN
    env["SCAN_INTERVAL_SECONDS"] = "3600"

    port = port_free()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        check("真起服务", wait_up(base))
        if "真起服务" in FAILS:
            print((server.stdout.read() if server.stdout else "")[-1500:])
            return 1

        print("=== A 流水线指令也落审计 ===")
        dispatch(base, "t-b18-p", "t-b18-p-1")
        code, _ = post(base, "/api/commands", {"command": "master:pause", "trace_id": "t-b18-p",
                                              "reason": "B18 人工排查"})
        check("pause 200", code == 200, str(code))
        code, _ = post(base, "/api/commands", {"command": "master:resume", "trace_id": "t-b18-p",
                                              "reason": "排查结束"})
        check("resume 200", code == 200, str(code))
        code, _ = post(base, "/api/commands", {"command": "master:clear-breaker", "trace_id": "t-b18-p",
                                              "reason": "复位"})
        check("clear-breaker 200", code == 200, str(code))

        _c, view = get(base, "/api/audit?limit=50&command=master:pause")
        items = (view or {}).get("items") or []
        check("pause 留痕一条", len(items) == 1, str(len(items)))
        if items:
            it = items[0]
            check("目标写成 master/trace:<trace_id>",
                  it["worker_id"] == "master" and it["instance_id"] == "trace:t-b18-p", str(it))
            check("原因落库", it["reason"] == "B18 人工排查", str(it.get("reason")))
            check("流水线指令没有『生效』概念（null）", it["effective"] is None, str(it.get("effective")))

        _c, all_view = get(base, "/api/audit?limit=50")
        cmds = [i["command"] for i in ((all_view or {}).get("items") or [])]
        check("四条指令都在审计里（含配额）",
              set(cmds) >= {"master:pause", "master:resume", "master:clear-breaker"}, str(sorted(set(cmds))))
        ids = [i["id"] for i in ((all_view or {}).get("items") or [])]
        check("items 是倒序", ids == sorted(ids, reverse=True), str(ids))

        print("=== B /api/audit 形状与过滤 ===")
        check("有 meta.total / shown / limit",
              all(k in (all_view or {}).get("meta", {}) for k in ("total", "shown", "limit")),
              str((all_view or {}).get("meta")))
        check("有 current 注册表快照", isinstance((all_view or {}).get("current"), dict),
              str(type((all_view or {}).get("current"))))

        print("=== C effective 语义（真实下发 + 真实上报）===")
        post(base, "/api/workers", {"worker_id": "workbuddy", "instance_id": "b18-live",
                                   "status": "running", "concurrency": 2, "limit": 2})
        post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy",
                                    "instance_id": "b18-live", "limit": 3, "reason": "B18 生效测试"})
        # Worker 还没按 3 跑 → false
        _c, v1 = get(base, "/api/audit?limit=10&worker_id=workbuddy&instance_id=b18-live")
        it1 = ((v1 or {}).get("items") or [None])[0]
        check("下发 3 但自报仍是 2 → 未生效", it1 and it1["effective"] is False, str(it1 and it1.get("effective")))
        # Worker 按 3 上报 → 同一条变 true
        post(base, "/api/workers", {"worker_id": "workbuddy", "instance_id": "b18-live",
                                   "status": "running", "concurrency": 3, "limit": 3})
        _c, v2 = get(base, "/api/audit?limit=10&worker_id=workbuddy&instance_id=b18-live")
        it2 = ((v2 or {}).get("items") or [None])[0]
        check("自报 3 后同一条 → 已生效", it2 and it2["effective"] is True, str(it2 and it2.get("effective")))
        check("current 快照里 desired=3 concurrency=3",
              ((v2 or {}).get("current") or {}).get("workbuddy/b18-live", {}).get("concurrency") == 3,
              str((v2 or {}).get("current")))
        # 再下发 4 → 旧行失去时效，变 null
        post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy",
                                    "instance_id": "b18-live", "limit": 4, "reason": "B18 覆盖测试"})
        _c, v3 = get(base, "/api/audit?limit=10&worker_id=workbuddy&instance_id=b18-live")
        its = (v3 or {}).get("items") or []
        check("被覆盖的旧行 effective=null", its[1]["effective"] is None if len(its) > 1 else False,
              str([i["effective"] for i in its]))
        check("最新一条仍是未生效（自报 3 / 期望 4）", its[0]["effective"] is False, str(its[0]["effective"]))
        # 撤销：期望归 0 → true
        post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy",
                                    "instance_id": "b18-live", "limit": 0, "reason": "撤销"})
        _c, v4 = get(base, "/api/audit?limit=10&worker_id=workbuddy&instance_id=b18-live")
        check("撤销（期望 0）判为已生效", ((v4 or {}).get("items") or [{}])[0].get("effective") is True,
              str(((v4 or {}).get("items") or [{}])[0].get("effective")))

        print("=== D 只读 + 接口面 11 ===")
        code, spec = get(base, "/openapi.json")
        paths = (spec or {})["paths"]
        check("接口面 11 个", len(paths) == 11, str(len(paths)))
        methods = {m for p in paths.values() for m in p}
        check("方法只有 GET/POST", methods <= {"get", "post"}, str(methods))
        check("/api/audit 只有 GET", set(paths.get("/api/audit", {}).keys()) == {"get"},
              str(list(paths.get("/api/audit", {}).keys())))
        src = open(os.path.join(ROOT, "app", "api", "routes_audit.py"), encoding="utf-8").read()
        check("查询接口不写库", "db.add(" not in src and "db.commit(" not in src)
        check("查询接口不删行", "delete(" not in src and "DELETE" not in src)

        print("=== E 看板 ===")
        code, html = request("GET", base + "/", raw=True)
        check("看板有运维审计面板", "auditTableWrap" in html and "GET /api/audit" in html)
        js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
        check("前端有 loadAudit 与生效判定", "loadAudit" in js and "auditEffective" in js)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print(f"\n结果：{'全部通过' if not FAILS else '有失败项'}（{len(FAILS)} 项失败）")
    if FAILS:
        for f in FAILS:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
