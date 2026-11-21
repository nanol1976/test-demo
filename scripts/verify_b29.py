"""Batch29 验收：优雅停止（stop flag），8 项。

守的是什么：
- 单一实现：stopflag.py 是唯一读写停止标记的地方
- 环境变量覆盖：MWP_STOP_FILE 指向临时目录，验收不碰真实 data/
- master:stop 写标记 + 审计；master:resume 清标记 + 审计
- 只读口令发 master:stop → 403（不是 401/422）
- 不带 trace_id 也能成功（像 master:worker-limit）
- worker 里有 should_stop、--stop-file、stop_flag_seen
- 静态检查：ALLOWED_COMMANDS 含 master:stop；stopflag 关键函数齐全
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# 导入 _proc_drain（同 verify_b28.py）
from _proc_drain import drain

FAILED = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


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
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = {"raw": body}
        return exc.code, parsed


def wait_up(base, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = request("GET", base + "/api/health", timeout=2)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    # 创建临时目录作为测试隔离区
    tmpdir = tempfile.mkdtemp(prefix="verify_b29_")
    stop_file = os.path.join(tmpdir, "STOP")
    os.environ["MWP_STOP_FILE"] = stop_file

    DB = os.path.join(tmpdir, "verify_b29.db")
    ARCHIVE = os.path.join(tmpdir, "archive")

    FULL = "b29-full-token"
    RO = "b29-readonly-token"

    try:
        # 清理环境
        for path in (DB,):
            if os.path.exists(path):
                os.remove(path)
        if os.path.isdir(ARCHIVE):
            shutil.rmtree(ARCHIVE)
        os.makedirs(os.path.dirname(DB), exist_ok=True)

        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        env["DATABASE_URL"] = f"sqlite:///{DB}"
        env["ARCHIVE_ROOT"] = ARCHIVE
        env["API_TOKEN"] = FULL
        env["API_TOKEN_READONLY"] = RO
        env["SCAN_INTERVAL_SECONDS"] = "3600"

        # 找个空闲端口
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
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

            print("=== 1) stopflag 单元测试 ===")
            # 导入 stopflag 模块测试
            import importlib.util
            spec = importlib.util.spec_from_file_location("stopflag", os.path.join(ROOT, "app", "core", "stopflag.py"))
            stopflag = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(stopflag)

            # 1.1 is_stopped() 初始 False
            check("is_stopped() 初始 False", stopflag.is_stopped() is False)

            # 1.2 request_stop 写文件
            path = stopflag.request_stop("verify")
            check("request_stop 返回路径", path == stop_file)
            check("标记文件已生成", os.path.exists(stop_file))
            with open(stop_file, encoding="utf-8") as f:
                content = f.read().strip()
            check("内容含 ISO 时间", "T" in content and "verify" in content)

            # 1.3 is_stopped() 变 True
            check("is_stopped() 变 True", stopflag.is_stopped() is True)

            # 1.4 clear_stop 删除
            check("clear_stop 返回 True", stopflag.clear_stop() is True)
            check("标记文件已删", not os.path.exists(stop_file))
            check("再次 clear_stop 返回 False（幂等）", stopflag.clear_stop() is False)

            print("=== 2) master:stop 接口 ===")
            # 2.1 全权口令 POST master:stop
            code, body = request("POST", base + "/api/commands",
                                 {"command": "master:stop", "reason": "verify"}, token=FULL)
            check("master:stop → 200", code == 200, f"{code} {body}")
            check("返回 stopping=true", body and body.get("stopping") is True, str(body))
            check("标记文件已生成", os.path.exists(stop_file))
            with open(stop_file, encoding="utf-8") as f:
                content = f.read().strip()
            check("标记内容含日期串", "T" in content and "verify" in content)

            # 2.2 审计多出一行 command="master:stop"
            code, body = request("GET", base + "/api/audit?limit=5", token=FULL)
            check("审计接口 200", code == 200, str(code))
            items = body.get("items", []) if body else []
            stop_audit = [r for r in items if r.get("command") == "master:stop"]
            check("审计有 master:stop 行", len(stop_audit) == 1, f"找到 {len(stop_audit)} 行")
            if stop_audit:
                row = stop_audit[0]
                check("审计 worker_id=master", row.get("worker_id") == "master")
                check("审计 instance_id=data", row.get("instance_id") == "data")
                check("审计 reason=verify", row.get("reason") == "verify")

            print("=== 3) 只读口令拦截 ===")
            # 3.1 只读口令发 master:stop → 403
            code, body = request("POST", base + "/api/commands",
                                 {"command": "master:stop", "reason": "ro"}, token=RO)
            check("只读口令 master:stop → 403", code == 403, f"{code} {body}")

            print("=== 4) 不带 trace_id 也能成功 ===")
            # 重新发一次 master:stop 确认不带 trace_id 也成功（第 2 步已验证，这里再发一次防止回归）
            code2, body2 = request("POST", base + "/api/commands",
                                   {"command": "master:stop", "reason": "verify2"}, token=FULL)
            check("master:stop 不带 trace_id 也 200", code2 == 200, f"{code2} {body2}")

            print("=== 5) master:resume 清标记 ===")
            # 先重新写一个标记
            stopflag.request_stop("resume-test")
            check("标记重新写入", os.path.exists(stop_file))

            # 找个存在的 trace_id（从 pipeline 里拿或造一个）
            # 直接调 master:resume + 任意 trace_id（不存在也没关系，因为 resume 清标记是幂等的）
            # 但接口要求 trace_id 必须存在，所以先插一条 pipeline
            conn = sqlite3.connect(DB)
            conn.execute("INSERT INTO pipeline_ledger (trace_id, original_demand, start_time, overall_status, pause_flag) VALUES (?,?,?,?,?)",
                         ("t-resume", "test", datetime.now(timezone.utc).isoformat(), "pending", 0))
            conn.commit()
            conn.close()

            code, body = request("POST", base + "/api/commands",
                                 {"command": "master:resume", "trace_id": "t-resume", "reason": "clean"}, token=FULL)
            check("master:resume → 200", code == 200, f"{code} {body}")
            check("返回 stopping=false", body and body.get("stopping") is False, str(body))
            check("标记文件被删除", not os.path.exists(stop_file))

            # 审计再多一行 master:resume（instance_id 存的是 trace:t-resume）
            code, body = request("GET", base + "/api/audit?limit=10", token=FULL)
            items = body.get("items", []) if body else []
            resume_audit = [r for r in items if r.get("command") == "master:resume" and r.get("instance_id") == "trace:t-resume"]
            check("审计有 master:resume 行", len(resume_audit) >= 1)

            print("=== 6) should_stop 纯函数测试 ===")
            # 导入 worker 模块里的 should_stop
            import importlib.util
            spec2 = importlib.util.spec_from_file_location("agent_worker", os.path.join(ROOT, "worker", "agent_worker.py"))
            agent_worker = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(agent_worker)

            check("should_stop(不存在路径) = False", agent_worker.should_stop("/not/exist/STOP") is False)
            # 创建文件
            with open(stop_file, "w") as f:
                f.write("x")
            check("should_stop(存在文件) = True", agent_worker.should_stop(stop_file) is True)
            os.remove(stop_file)

            print("=== 7) 静态检查 ===")
            # 7.1 routes_commands.py 有 master:stop
            routes = open(os.path.join(ROOT, "app", "api", "routes_commands.py"), encoding="utf-8").read()
            check("ALLOWED_COMMANDS 含 master:stop", '"master:stop"' in routes or "'master:stop'" in routes)
            check("CommandResult 有 stopping", "stopping: bool" in routes)
            check("import stopflag", "from app.core.stopflag import" in routes)

            # 7.2 stopflag.py 关键标识
            stopflag_src = open(os.path.join(ROOT, "app", "core", "stopflag.py"), encoding="utf-8").read()
            check("STOP_FILE_NAME 常量", "STOP_FILE_NAME" in stopflag_src)
            check("MWP_STOP_FILE 环境变量", "MWP_STOP_FILE" in stopflag_src)
            check("os.replace 原子写", "os.replace" in stopflag_src)
            check("def request_stop", "def request_stop" in stopflag_src)
            check("def clear_stop", "def clear_stop" in stopflag_src)

            # 7.3 worker 关键标识
            worker_src = open(os.path.join(ROOT, "worker", "agent_worker.py"), encoding="utf-8").read()
            check("def should_stop", "def should_stop" in worker_src)
            check("stop_file 参数", "stop_file" in worker_src)
            check("--stop-file 参数", "--stop-file" in worker_src)
            check("stop_flag_seen 日志", "stop_flag_seen" in worker_src)

        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    finally:
        # 清理临时目录
        shutil.rmtree(tmpdir, ignore_errors=True)

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
