"""Batch28 验收：只读口令（能看不能动），12 项。

守的是什么：
- 只读口令能读全部查询接口，但任何写操作在**服务端**就被 403 拦掉，不靠前端自觉；
- 全权口令不受影响，该写还是能写；
- 没配任何口令时保持 Batch3 的零门槛行为（本地开发不用带 token）；
- 权限档通过响应头 X-API-Mode 回填，前端据此亮「只读」章，而不是点一下才报错；
- 被 403 拦下的请求不许在审计里留下痕迹（没写就没记录）。
"""

import json
import os
import sqlite3
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

from _proc_drain import drain  # 子进程日志必须读走：Windows 管道写满会把服务堵死

DB = "./data/verify_b28.db"
ARCHIVE = "./archive/_verify_b28"
FULL = "b28-full-token"
RO = "b28-readonly-token"
FAILED = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(method, url, payload=None, token=None, timeout=15):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-API-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            mode = resp.headers.get("X-API-Mode")
            return resp.status, (json.loads(body) if body else None), mode
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = {"raw": body}
        return exc.code, parsed, exc.headers.get("X-API-Mode")


def wait_up(base, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _, _ = request("GET", base + "/api/health", timeout=2)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def audit_total(base, token):
    code, body, _ = request("GET", base + "/api/audit?limit=20", token=token)
    if code != 200 or not isinstance(body, dict):
        return -1
    return body.get("meta", {}).get("total", -1)


def main() -> int:
    for path in (DB,):
        if os.path.exists(path):
            os.remove(path)
    import shutil
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = f"sqlite:///{DB}"
    env["ARCHIVE_ROOT"] = ARCHIVE
    env["API_TOKEN"] = FULL
    env["API_TOKEN_READONLY"] = RO   # 注意：os.environ 里可能有别的值，这里显式覆盖
    env["SCAN_INTERVAL_SECONDS"] = "3600"

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    srv_log = drain(server)
    try:
        check("真起服务", wait_up(base))
        if "真起服务" in FAILED:
            print("".join(srv_log[-40:])[-1500:])
            return 1

        print("=== A 鉴权基线 ===")
        code, _, _ = request("GET", base + "/api/health")
        check("健康检查不需要口令（200）", code == 200, str(code))
        code, _, _ = request("GET", base + "/api/pipelines")
        check("不带口令读接口 → 401", code == 401, str(code))
        code, _, _ = request("GET", base + "/api/pipelines", token="wrong-token")
        check("错误口令 → 401", code == 401, str(code))

        print("=== B 全权口令 ===")
        code, body, mode = request("GET", base + "/api/pipelines", token=FULL)
        check("全权口令读接口 → 200", code == 200, str(code))
        check("响应头 X-API-Mode=full", mode == "full", str(mode))

        stamp = datetime.now(timezone.utc).isoformat()
        dispatch = {
            "trace_id": "t-b28", "task_id": "t-b28-1", "msg_type": "task_dispatch",
            "sender": "master", "timestamp": stamp, "version": "v1.0",
            "payload": {"target_worker": "workbuddy", "task_goal": "Batch28 只读口令验证"},
        }
        code, body, _ = request("POST", base + "/api/messages", dispatch, token=FULL)
        check("全权口令可写（投递报文 202）", code == 202, f"{code} {body}")
        code, body, _ = request("POST", base + "/api/commands",
                                {"command": "master:pause", "trace_id": "t-b28", "reason": "B28"}, token=FULL)
        check("全权口令可下发指令（200）", code == 200, f"{code} {body}")
        code, _, _ = request("POST", base + "/api/workers",
                             {"worker_id": "workbuddy", "instance_id": "b28-a", "status": "running",
                              "concurrency": 2, "limit": 2}, token=FULL)
        check("全权口令可上报执行方（202）", code == 202, str(code))

        print("=== C 只读口令：能读 ===")
        for path in ("/api/pipelines", "/api/tasks", "/api/workers", "/api/audit?limit=5"):
            code, _, _ = request("GET", base + path, token=RO)
            check(f"只读口令可读 {path}", code == 200, str(code))
        code, _, mode = request("GET", base + "/api/pipelines", token=RO)
        check("响应头 X-API-Mode=readonly", mode == "readonly", str(mode))

        print("=== D 只读口令：不能写（服务端 403）===")
        before = audit_total(base, FULL)
        code, body, _ = request("POST", base + "/api/commands",
                                {"command": "master:resume", "trace_id": "t-b28", "reason": "只读试写"}, token=RO)
        check("只读口令下发指令 → 403", code == 403, f"{code} {body}")
        code, body, _ = request("POST", base + "/api/messages", dispatch, token=RO)
        check("只读口令投递报文 → 403", code == 403, f"{code} {body}")
        code, body, _ = request("POST", base + "/api/workers",
                                {"worker_id": "workbuddy", "instance_id": "b28-a", "status": "stopped"}, token=RO)
        check("只读口令上报执行方 → 403", code == 403, f"{code} {body}")
        after = audit_total(base, FULL)
        check("被 403 拦下的请求没写审计（条数不变）", before == after, f"{before} → {after}")
        code, body, _ = request("GET", base + "/api/audit?limit=3", token=None)
        check("拦 401 时审计接口本身也不放行", code == 401, str(code))

        print("=== E 代码与看板 ===")
        sec = open(os.path.join(ROOT, "app", "core", "security.py"), encoding="utf-8").read()
        cfg = open(os.path.join(ROOT, "app", "core", "config.py"), encoding="utf-8").read()
        appjs = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
        html = open(os.path.join(ROOT, "app", "static", "index.html"), encoding="utf-8").read()
        css = open(os.path.join(ROOT, "app", "static", "style.css"), encoding="utf-8").read()
        routes = "".join(
            open(os.path.join(ROOT, "app", "api", f), encoding="utf-8").read()
            for f in ("routes_commands.py", "routes_messages.py", "routes_workers.py")
        )
        mainpy = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
        check("config 有 api_token_readonly", "api_token_readonly" in cfg)
        check("security 有 require_write", "def require_write" in sec)
        check("三条写接口都挂了 require_write", routes.count("Depends(require_write)") == 3,
              str(routes.count("Depends(require_write)")))
        check("响应头由中间件统一写", "MODE_HEADER" in mainpy and "middleware" in mainpy)
        check("看板认 X-API-Mode 并切只读态", "X-API-Mode" in appjs and "applyMode" in appjs)
        check("看板有只读章且会禁用写按钮", 'id="modeBadge"' in html and "WRITE_BTNS" in appjs)
        check("只读章有样式", ".mode-badge" in css)

        # 说明：不配口令时的零门槛行为由 smoke_http.py（env API_TOKEN=""）继续覆盖，
        # 这里不重复起第二个实例。
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        if os.path.exists(DB) and not os.environ.get("KEEP_DB"):
            pass  # 留给 data/ 卫生规则处理，不在这里删

    print()
    if FAILED:
        print(f"结果：有失败项（{len(FAILED)} 项失败）")
        for item in FAILED:
            print("  ✗ " + item)
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
