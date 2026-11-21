"""Batch32-1 验收：异构复核（18 项）。

守的是什么（每一条都能红）：
1. 有验收标准的单回执后，平台自动派生复核单 `<task_id>-rv`；
2. 复核者 ≠ 执行者（防作弊第 1 条），且复核单 `review_of` 指向原单；
3. 复核单不再派生复核单（防作弊第 2 条，不递归）；
4. 逐项条数必须等于标准条数（防作弊第 3 条）；缺项理由点出编号（c3）；
5. pass → 原单留「待验收」等签收；fail → 原单「验收驳回」并扣 retry；
6. 无合格复核者 / 复核者被指定成执行者本人 → 标 `unreviewed`，签收需 force_accept；
7. 结论只写一次（防作弊第 4 条）：第二次回执 409；
8. 无标准的单走老路径（不派生复核、签收照旧）；
9. 接口仍是 11 条路径，TaskView 多出三个复核字段；
10. 规则复核脚本真能判：拿平台下发的 b64 上下文跑一遍，正例 pass、负例 fail。

用法：python scripts/verify_b32.py
退出码：0 全绿 / 1 有失败。
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

from _proc_drain import drain

FAILED = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


def request(method, url, payload=None, timeout=15):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
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


_seq = [0]


def now_iso():
    _seq[0] += 1
    return datetime.now().astimezone().isoformat(timespec="microseconds") + f".{_seq[0]:04d}"


def dispatch(base, trace, task, criteria, instance, reviewer=None, goal="B32 复核验收"):
    payload = {
        "task_goal": goal,
        "target_worker": "workbuddy",
        "target_instance": instance,
        "max_retry": 3,
        "acceptance_criteria": criteria,
        "exec": {"script": "scripts/review_fixture.py", "args": ["--files", "0"]},
    }
    return request("POST", base + "/api/messages", {
        "trace_id": trace, "task_id": task, "msg_type": "task_dispatch",
        "sender": "master", "timestamp": now_iso(), "version": "v1.0", "payload": payload,
    })


def task_return(base, trace, task, instance, artifacts, logs="脚本正常退出（退出码 0）",
                reviewer=None):
    meta = {"instance_id": instance, "self_check": "夹具已落盘"}
    if reviewer:
        # 复核者建议写在 metadata 里（顶层自定义字段会被 schema 收敛掉）
        meta["reviewer"] = reviewer
    return request("POST", base + "/api/messages", {
        "trace_id": trace, "task_id": task, "msg_type": "task_return",
        "sender": "workbuddy", "timestamp": now_iso(), "version": "v1.0",
        "payload": {
            "output": {"artifacts": artifacts, "exit_code": 0, "duration_ms": 12},
            "logs": logs,
            "metadata": meta,
        },
    })


def confirm(base, trace, force=False):
    payload = {"approved": True, "accept_tasks": True}
    if force:
        payload["force_accept"] = True
    return request("POST", base + "/api/messages", {
        "trace_id": trace, "task_id": trace, "msg_type": "human_confirm_resp",
        "sender": "user", "timestamp": now_iso(), "version": "v1.0", "payload": payload,
    })


def get_task(base, task_id):
    """GET /api/tasks/{id} 直接返回 TaskView（没有 item 包裹）。"""
    code, body = request("GET", base + f"/api/tasks/{task_id}")
    if code != 200 or not isinstance(body, dict):
        return None
    return body if "task_id" in body else body.get("item")


def arts(n, exists=True):
    return [{"path": f"/tmp/b32/part{i}.txt", "exists": exists, "size_bytes": 12} for i in range(1, n + 1)]


def run_review_script(b64):
    """真跑一遍规则复核脚本，拿它的 stdout（平台也按整段 JSON 解析 logs）。"""
    proc = subprocess.run([PY, os.path.join(ROOT, "scripts", "task_review.py"),
                           "--review-b64", b64],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="verify_b32_")
    DB = os.path.join(tmpdir, "verify_b32.db")
    ARCHIVE = os.path.join(tmpdir, "archive")
    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        env["DATABASE_URL"] = f"sqlite:///{DB}"
        env["ARCHIVE_ROOT"] = ARCHIVE
        env["API_TOKEN"] = ""
        env["API_TOKEN_READONLY"] = ""
        env["API_TOKEN_WORKER"] = ""
        env["SCAN_INTERVAL_SECONDS"] = "3600"

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

            print("=== 0) 准备：两台在线实例（ops-win 与 qa-mac） ===")
            for inst in ("ops-win", "qa-mac"):
                code, _ = request("POST", base + "/api/workers", {
                    "worker_id": "workbuddy", "instance_id": inst, "status": "running",
                    "concurrency": 2, "limit": 2, "in_flight": 0, "handled": 0,
                })
                check(f"实例 {inst} 已注册在线", code == 202, f"code={code}")

            print("\n=== 1) 正例：标准 3 条、产物 3 个 ===")
            code, body = dispatch(base, "b32-pos", "t-b32-pos", ["a", "b", "c"], "ops-win")
            check("派单成功（执行中）", code == 202, f"code={code}")
            code, body = task_return(base, "b32-pos", "t-b32-pos", "ops-win", arts(3))
            note = (body or {}).get("note", "")
            check("回执后自动派生复核单", code == 202 and "-rv" in note, note[:120])
            rv = get_task(base, "t-b32-pos-rv")
            check("复核单存在且指向原单", bool(rv) and rv.get("review_of") == "t-b32-pos")
            check("复核者不是执行者（防作弊 1）",
                  bool(rv) and rv.get("target_instance") not in (None, "ops-win"),
                  f"复核者={rv.get('target_instance') if rv else None}")
            check("复核单不带验收标准（不递归的前提）",
                  bool(rv) and not (rv.get("payload") or {}).get("acceptance_criteria"))
            b64 = ((rv or {}).get("payload") or {}).get("exec", {}).get("args", ["", ""])[1] if rv else ""
            rc, out, err = run_review_script(b64)
            verdict_obj = {}
            try:
                verdict_obj = json.loads(out)
            except ValueError:
                pass
            check("规则复核脚本可跑通并给出 pass", rc == 0 and verdict_obj.get("verdict") == "pass",
                  f"rc={rc} out={out[:120]} err={err[:80]}")
            check("正例逐项结论 3 条（条数对齐）", len(verdict_obj.get("items") or []) == 3)
            code, body = task_return(base, "b32-pos", "t-b32-pos-rv", "qa-mac", [],
                                     logs=out)
            check("复核回执被接受", code == 202, f"code={code} {(body or {}).get('note','')[:80]}")
            pos = get_task(base, "t-b32-pos")
            check("原单结论 pass 且逐项结论落账",
                  bool(pos) and pos.get("review_verdict") == "pass" and len(pos.get("review_items") or []) == 3)
            check("复核单自身直接验收通过",
                  (get_task(base, "t-b32-pos-rv") or {}).get("status") == "验收通过")
            check("不递归：没有 t-b32-pos-rv-rv", get_task(base, "t-b32-pos-rv-rv") is None)
            code, body = confirm(base, "b32-pos")
            pos = get_task(base, "t-b32-pos")
            check("签收后原单验收通过", code == 202 and pos.get("status") == "验收通过",
                  (body or {}).get("note", "")[:100])

            print("\n=== 2) 负例 A：标准 3 条、只交 2 个产物 ===")
            dispatch(base, "b32-neg", "t-b32-neg", ["a", "b", "c"], "ops-win")
            code, body = task_return(base, "b32-neg", "t-b32-neg", "ops-win", arts(2))
            neg_rv = get_task(base, "t-b32-neg-rv")
            b64 = ((neg_rv or {}).get("payload") or {}).get("exec", {}).get("args", ["", ""])[1] if neg_rv else ""
            rc, out, err = run_review_script(b64)
            obj = {}
            try:
                obj = json.loads(out)
            except ValueError:
                pass
            check("负例 A 脚本判 fail 且点出 c3",
                  rc == 0 and obj.get("verdict") == "fail" and "c3" in (obj.get("missing") or []),
                  f"missing={obj.get('missing')}")
            code, body = task_return(base, "b32-neg", "t-b32-neg-rv", "qa-mac", [], logs=out)
            neg = get_task(base, "t-b32-neg")
            check("复核 fail → 原单转验收驳回", neg.get("status") == "验收驳回",
                  f"status={neg.get('status')}")
            check("驳回理由点出缺失项编号 c3", "c3" in (neg.get("reject_reason") or ""),
                  (neg.get("reject_reason") or "")[:80])
            check("驳回时扣了一次重试（3 → 2）", neg.get("remaining_retry") == 2,
                  f"remaining={neg.get('remaining_retry')}")

            print("\n=== 3) 负例 B：复核者被指定成执行者本人 ===")
            dispatch(base, "b32-self", "t-b32-self", ["a", "b"], "ops-win")
            code, body = task_return(base, "b32-self", "t-b32-self", "ops-win", arts(2),
                                     reviewer="ops-win")
            note = (body or {}).get("note", "")
            check("同实例复核 → 不建复核单", get_task(base, "t-b32-self-rv") is None, note[:100])
            self_task = get_task(base, "t-b32-self")
            check("任务被标 unreviewed", self_task.get("review_verdict") == "unreviewed")
            code, body = confirm(base, "b32-self")
            check("unreviewed 不带 force_accept 时被闸门拦下",
                  get_task(base, "t-b32-self").get("status") == "待验收",
                  (body or {}).get("note", "")[:120])
            code, body = confirm(base, "b32-self", force=True)
            check("force_accept 才放行", get_task(base, "t-b32-self").get("status") == "验收通过",
                  (body or {}).get("note", "")[:120])

            print("\n=== 4) 结论只写一次（防作弊 4） ===")
            code, body = task_return(base, "b32-pos", "t-b32-pos-rv", "qa-mac", [],
                                     logs=json.dumps({"verdict": "fail", "items": []}))
            check("二次复核回执被 409 拒绝", code == 409,
                  f"code={code} {str(body)[:80]}")
            check("原单结论没有被改写",
                  get_task(base, "t-b32-pos").get("review_verdict") == "pass")

            print("\n=== 5) 老路径：无标准的单不受影响 ===")
            payload = {
                "task_goal": "无标准的老单", "target_worker": "workbuddy",
                "target_instance": "ops-win", "max_retry": 3,
                "exec": {"script": "scripts/review_fixture.py", "args": ["--files", "0"]},
            }
            request("POST", base + "/api/messages", {
                "trace_id": "b32-plain", "task_id": "t-b32-plain", "msg_type": "task_dispatch",
                "sender": "master", "timestamp": now_iso(), "version": "v1.0", "payload": payload,
            })
            code, body = task_return(base, "b32-plain", "t-b32-plain", "ops-win", arts(1))
            plain = get_task(base, "t-b32-plain")
            check("无标准不派生复核单", get_task(base, "t-b32-plain-rv") is None)
            check("无标准单 review_verdict 为空（老行为）", plain.get("review_verdict") in (None, ""))
            code, body = confirm(base, "b32-plain")
            check("无标准单签收照旧通过", get_task(base, "t-b32-plain").get("status") == "验收通过")

            print("\n=== 6) 接口面与字段 ===")
            code, spec = request("GET", base + "/openapi.json")
            paths = len((spec or {}).get("paths") or {})
            check("接口路径仍是 11 条（没为复核加路径）", paths == 11, f"{paths} 条")
            code, body = request("GET", base + "/api/tasks/t-b32-pos")
            item = body if isinstance(body, dict) else {}
            check("TaskView 带 review_of / review_verdict / review_items 三字段",
                  code == 200 and all(k in item for k in ("review_of", "review_verdict", "review_items")),
                  f"code={code} 字段={sorted(item.keys())[:12]}")

        finally:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
    finally:
        if os.path.isdir(tmpdir):
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
