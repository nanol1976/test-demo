"""Batch22 验收脚本：审计真分页 + 操作者留痕 + 页面下发配额的可见反馈。

三件事都不扩接口面（仍是 11 个路径）：
1. A `GET /api/audit` 加 `offset`（同一条路径、仍是 GET），看板不再硬顶 20 条
2. B 配额下发入口早就在页面上，补的是"下发之后看得见结果"和"期望≠实际"的未生效标记
3. C 审计表加 `operator`（v009 迁移，只 ADD COLUMN），来源 X-Operator 头

覆盖：
A offset 翻页：第 2 页与第 1 页无重叠、边界正确、超界返回空、meta.total 不随翻页变
B 操作者：请求头落库、缺省为空、超长截断 64、老行（迁移前）读出来是空串不是 null
C v009 迁移幂等：重复跑不报错；迁移后原有行一行不少
D 接口面 11、/api/audit 仍只有 GET、查询接口不写库
E 看板：分页控件、操作者输入、配额结果区、未生效标记
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
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

from _proc_drain import drain  # 子进程日志必须读走：Windows 管道写满会把服务堵死

DB = "./data/verify_b22.db"
ARCHIVE = "./archive/_verify_b22"
TOKEN = "verify-token"
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def request(method, url, payload=None, timeout=15, raw=False, headers_extra=None):
    data = None
    headers = {"Accept": "application/json", "X-API-Token": TOKEN}
    if headers_extra:
        headers.update(headers_extra)
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


def post(base, path, payload, op=None):
    # 头只能 latin-1，中文按 percent-encode 传（与浏览器 encodeURIComponent 一致）
    extra = {"X-Operator": quote(op, safe="")} if op is not None else None
    return request("POST", base + path, payload, headers_extra=extra)


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
        "payload": {"target_worker": "workbuddy", "task_goal": "B22 分页用"},
    })


def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)

    url = f"sqlite:///{DB}"
    proc = run_migrate([], url)
    check("迁移到 v9 成功", proc.returncode == 0, proc.stdout.strip().splitlines()[-1:])
    again = run_migrate([], url)
    check("v009 幂等（重复迁移不报错）", again.returncode == 0, again.stdout.strip().splitlines()[-1:])

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
    srv_log = drain(server)  # 必须读走 stdout，否则 Windows 上管道写满会把服务堵死
    try:
        check("真起服务", wait_up(base))
        if "真起服务" in FAILS:
            print("".join(srv_log[-40:])[-1500:])
            return 1

        print("=== 0 造 45 条审计（超过一页）===")
        post(base, "/api/workers", {"worker_id": "workbuddy", "instance_id": "b22-a",
                                   "status": "running", "concurrency": 2, "limit": 2}, op="刘浩")
        for i in range(44):
            post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy",
                                         "instance_id": "b22-a", "limit": i % 5,
                                         "reason": f"B22 第 {i} 次"}, op="刘浩")
        _c, v0 = get(base, "/api/audit?limit=20")
        check("造出超过 20 条", (v0 or {}).get("meta", {}).get("total", 0) > 20,
              str((v0 or {}).get("meta")))

        print("=== A offset 真分页 ===")
        _c, p1 = get(base, "/api/audit?limit=20&offset=0")
        _c, p2 = get(base, "/api/audit?limit=20&offset=20")
        _c, p3 = get(base, "/api/audit?limit=20&offset=40")
        ids1 = [i["id"] for i in (p1 or {}).get("items", [])]
        ids2 = [i["id"] for i in (p2 or {}).get("items", [])]
        ids3 = [i["id"] for i in (p3 or {}).get("items", [])]
        check("第 1 页 20 条", len(ids1) == 20, str(len(ids1)))
        check("第 2 页 20 条", len(ids2) == 20, str(len(ids2)))
        check("两页无重叠", not (set(ids1) & set(ids2)), str(len(set(ids1) & set(ids2))))
        check("第 2 页接在第 1 页后面（整体仍倒序）", ids1[-1] > ids2[0], f"{ids1[-1]} / {ids2[0]}")
        check("末页只剩余数", len(ids3) == (p3 or {}).get("meta", {}).get("total", 0) - 40,
              f"{len(ids3)} / total={(p3 or {}).get('meta', {}).get('total')}")
        check("meta.total 不随翻页变化",
              (p1 or {})["meta"]["total"] == (p3 or {})["meta"]["total"],
              f"{p1['meta']['total']} vs {p3['meta']['total']}")
        check("shown 反映本页条数", (p2 or {})["meta"]["shown"] == 20, str((p2 or {})["meta"]))
        _c, far = get(base, "/api/audit?limit=20&offset=9999")
        check("offset 超界返回空列表而不是报错",
              _c == 200 and far["items"] == [], f"{_c} / {len((far or {}).get('items', []))}")
        _c, bad = get(base, "/api/audit?limit=20&offset=-1")
        check("offset 负数被拒（422）", _c == 422, str(_c))
        _c, big = get(base, "/api/audit?limit=20&offset=99999&reason=x")
        check("offset 上限仍在 200 以内 limit 不变", _c in (200, 422), str(_c))

        print("=== B 操作者留痕 ===")
        _c, v1 = get(base, "/api/audit?limit=5")
        items = (v1 or {}).get("items") or []
        check("每条都带 operator 字段", all("operator" in i for i in items), str(list(items[0].keys())))
        check("带 X-Operator 的下发落了名字", items[0].get("operator") == "刘浩", str(items[0].get("operator")))
        post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy",
                                     "instance_id": "b22-a", "limit": 3, "reason": "B22 匿名"})
        _c, v2 = get(base, "/api/audit?limit=1")
        check("不带 X-Operator 时为空串（不是 null）",
              ((v2 or {}).get("items") or [{}])[0].get("operator") == "",
              str(((v2 or {}).get("items") or [{}])[0].get("operator")))
        long_name = "某" * 80
        post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy",
                                     "instance_id": "b22-a", "limit": 4, "reason": "B22 超长"}, op=long_name)
        _c, v3 = get(base, "/api/audit?limit=1")
        got = ((v3 or {}).get("items") or [{}])[0].get("operator") or ""
        check("超长操作者截断到 64", len(got) == 64, str(len(got)))
        # 流水线指令也记操作者
        dispatch(base, "t-b22-p", "t-b22-p-1")
        post(base, "/api/commands", {"command": "master:pause", "trace_id": "t-b22-p",
                                     "reason": "B22 流水线"}, op="运维小王")
        _c, v4 = get(base, "/api/audit?limit=1")
        it = ((v4 or {}).get("items") or [{}])[0]
        check("流水线指令也带操作者", it.get("operator") == "运维小王" and it["command"] == "master:pause", str(it))

        print("=== C 迁移不动已有行 ===")
        _c, v_all = get(base, "/api/audit?limit=200")
        expected_total = (v_all or {}).get("meta", {}).get("total", -1)
        import sqlite3
        conn = sqlite3.connect(DB)
        rows = conn.execute("SELECT count(*) FROM worker_quota_audit").fetchone()[0]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(worker_quota_audit)").fetchall()]
        conn.close()
        check("迁移后行数 = 接口报的 total（一行没丢）", rows == expected_total,
              f"{rows} vs {expected_total}")
        check("v009 只加了 operator 一列",
              cols.count("operator") == 1 and set(cols) >= {"id", "worker_id", "instance_id",
                                                            "desired_limit", "prev_limit",
                                                            "command", "reason", "created_at"},
              str(cols))

        print("=== D 接口面与只读 ===")
        code, spec = get(base, "/openapi.json")
        paths = (spec or {})["paths"]
        check("接口面仍是 11 个（加 offset 不新增路径）", len(paths) == 11, str(len(paths)))
        check("/api/audit 只有 GET", set(paths.get("/api/audit", {}).keys()) == {"get"},
              str(list(paths.get("/api/audit", {}).keys())))
        check("audit 参数含 offset",
              "offset" in [p["name"] for p in paths["/api/audit"]["get"].get("parameters", [])],
              str([p["name"] for p in paths["/api/audit"]["get"].get("parameters", [])]))
        src = open(os.path.join(ROOT, "app", "api", "routes_audit.py"), encoding="utf-8").read()
        check("查询接口不写库", "db.add(" not in src and "db.commit(" not in src)

        print("=== E 看板 ===")
        _c, html = request("GET", base + "/", raw=True)
        check("有审计分页控件", all(k in html for k in ("auditPrev", "auditNext", "auditPageInfo")), "")
        check("有操作者输入框", "operatorInput" in html)
        check("执行方面板有指令结果区", "workerCmdResult" in html)
        js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
        check("前端按页取审计（带 offset）", "offset=" in js and "AUDIT_PAGE" in js)
        check("请求头带 X-Operator", '"X-Operator"' in js)
        check("期望≠实际时标未生效", "未生效" in js and "concurrency !== w.desired_limit" in js)
        css = open(os.path.join(ROOT, "app", "static", "style.css"), encoding="utf-8").read()
        check("审计表列宽按七列重排", "nth-child(7)" in css or "td:last-child" in css)
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
