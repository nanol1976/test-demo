"""Batch32-2 派单侧验收：插件授权（三级判定）+ 拒单审计。

守的是什么（每条都能红）：
1. 不带 plugin 的老单照旧派发（零回归）；
2. 带 plugin 但不指定岗位 → 拒，且审计留下 reason；
3. 未授权实例 → 拒；岗位代号命中 granted_roles → 放行（实例优先、岗位兜底）；
4. revoked_instances 一票否决（即使 granted_to 命中，也拒）；
5. review_state 不是 reviewed（draft/revoked）→ 拒；
6. 未登记的插件 → 拒；
7. 拒单不建任务、不建流水线，只落 plugin_audit（台账行数不变，审计行数＋1）；
8. 审计 append-only：多次判定只追加，旧行不变；
9. index.json 热改即生效（不重启平台）；
10. 接口仍 11 条路径。

用法：python scripts/verify_b32_2b.py
退出码：0 全绿 / 1 有失败。
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
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

from _proc_drain import drain

FAILED = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


def _ts(value):
    """把审计时间串解析成可比时间：两种写法（带 T 的 ISO / 带斜杠的旧式）都认。"""
    text = (value or "").replace("/", "-").replace(" ", "T")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min
    return dt if dt.tzinfo else dt.replace(tzinfo=None)


def _ordered(snap):
    """只守「时间倒序」这一条（同刻的先后由服务端的 source/id 兜底，不在这里判）。"""
    return all(_ts(snap[i][0]) >= _ts(snap[i + 1][0]) for i in range(len(snap) - 1))


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


def dispatch(base, trace, task, instance, plugin=None, criteria=("c1", "c2")):
    payload = {
        "task_goal": "B32-2 插件授权验收",
        "target_worker": "workbuddy",
        "max_retry": 3,
        "acceptance_criteria": list(criteria),
        "exec": {"script": "scripts/review_fixture.py", "args": ["--files", "0"]},
    }
    if instance:
        payload["target_instance"] = instance
    if plugin:
        payload["plugin"] = plugin
    return request("POST", base + "/api/messages", {
        "trace_id": trace, "task_id": task, "msg_type": "task_dispatch",
        "sender": "master", "timestamp": now_iso(), "version": "v1.0", "payload": payload,
    })


def db_query(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def count(db_path, table):
    return db_query(db_path, f"select count(*) from {table}")[0][0]


def audit_rows(db_path):
    return db_query(db_path, "select id, plugin_name, instance_id, decision, reason "
                            "from plugin_audit order by id")


def set_index(plugins_dir, **entry_updates):
    path = os.path.join(plugins_dir, "index.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["plugins"][0].update(entry_updates)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="verify_b32_2b_")
    DB = os.path.join(tmpdir, "verify_b32_2b.db")
    PLUGINS = os.path.join(tmpdir, "plugins")
    try:
        shutil.copytree(os.path.join(ROOT, "plugins"), PLUGINS)

        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        env["DATABASE_URL"] = f"sqlite:///{DB}"
        env["ARCHIVE_ROOT"] = os.path.join(tmpdir, "archive")
        env["API_TOKEN"] = ""
        env["API_TOKEN_READONLY"] = ""
        env["API_TOKEN_WORKER"] = ""
        env["SCAN_INTERVAL_SECONDS"] = "3600"
        env["MWP_PLUGINS_DIR"] = PLUGINS

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

            print("=== 0) 准备：注册三个岗位（ops-win / ops-mac / qa-mac） ===")
            for inst in ("ops-win", "ops-mac", "qa-mac"):
                code, _ = request("POST", base + "/api/workers", {
                    "worker_id": "workbuddy", "instance_id": inst, "status": "running",
                    "concurrency": 2, "limit": 2, "in_flight": 0, "handled": 0,
                })
                check(f"实例 {inst} 已注册在线", code == 202, f"code={code}")

            check("索引初态：reviewed + granted_roles=[ops] + 未逐实例授权",
                  db_query(DB, "select 1") == [(1,)])

            print("\n=== 1) 零回归：不带 plugin 的老单 ===")
            code, body = dispatch(base, "b32p-old", "t-b32p-old", "ops-win")
            check("不带 plugin 的派单照旧成功", code == 202, f"code={code}")

            print("\n=== 2) 带 plugin 但没指定岗位 ===")
            tasks_before = count(DB, "task_ledger")
            pipes_before = count(DB, "pipeline_ledger")
            audits_before = count(DB, "plugin_audit")
            code, body = dispatch(base, "b32p-noplug-pos", "t-b32p-noinst",
                                  instance=None, plugin="deploy-smoke")
            msg = json.dumps(body or {}, ensure_ascii=False)
            check("未指定岗位被拒", code in (409, 422) and "target_instance" in msg, f"code={code}")
            check("拒单没有新增任务行", count(DB, "task_ledger") == tasks_before)
            check("拒单没有新增流水线", count(DB, "pipeline_ledger") == pipes_before)
            check("拒单留了一条审计", count(DB, "plugin_audit") == audits_before + 1)
            last = audit_rows(DB)[-1]
            check("审计写的 reason 是「必须指定岗位」",
                  last[3] == "deny" and "必须指定" in (last[4] or ""), str(last[4])[:80])

            print("\n=== 3) 未授权实例（qa-mac，角色 qa 不在 granted_roles） ===")
            code, body = dispatch(base, "b32p-deny", "t-b32p-deny", "qa-mac", plugin="deploy-smoke")
            msg = json.dumps(body or {}, ensure_ascii=False)
            check("未授权实例被拒", code in (409, 422) and "不能用插件" in msg, f"code={code}")
            check("拒单理由点出「均未授权」",
                  "均未授权" in (audit_rows(DB)[-1][4] or ""))

            print("\n=== 4) 岗位兜底：ops-mac（ops 角色命中 granted_roles） ===")
            code, body = dispatch(base, "b32p-role", "t-b32p-role", "ops-mac", plugin="deploy-smoke")
            check("岗位兜底放行", code == 202, f"code={code}")
            last = audit_rows(DB)[-1]
            check("审计记 allow 且理由点出岗位命中",
                  last[3] == "allow" and "岗位命中" in (last[4] or ""), str(last[4])[:80])

            print("\n=== 5) 实例白名单：热改 index.json 给 qa-mac 单开授权 ===")
            set_index(PLUGINS, granted_to=["qa-mac"])
            code, body = dispatch(base, "b32p-inst", "t-b32p-inst", "qa-mac", plugin="deploy-smoke")
            check("实例白名单生效（不重启）", code == 202, f"code={code}")
            check("审计理由点出实例命中 + 一级复核通过",
                  "实例命中 granted_to" in (audit_rows(DB)[-1][4] or "")
                  and "一级复核通过" in (audit_rows(DB)[-1][4] or ""))

            print("\n=== 6) 撤权一票否决（revoked 命中，即使 granted_to 也拒） ===")
            set_index(PLUGINS, granted_to=["qa-mac"], revoked_instances=["qa-mac"])
            code, body = dispatch(base, "b32p-revoke", "t-b32p-revoke", "qa-mac", plugin="deploy-smoke")
            msg = json.dumps(body or {}, ensure_ascii=False)
            check("撤权实例被拒", code in (409, 422) and "撤权" in msg, f"code={code}")
            check("审计理由点出撤权优先",
                  "撤权" in (audit_rows(DB)[-1][4] or ""))

            print("\n=== 7) 未过审插件（draft） ===")
            set_index(PLUGINS, granted_to=["qa-mac"], revoked_instances=[], review_state="draft")
            code, body = dispatch(base, "b32p-draft", "t-b32p-draft", "qa-mac", plugin="deploy-smoke")
            msg = json.dumps(body or {}, ensure_ascii=False)
            check("draft 插件被拒", code in (409, 422) and "未过审核" in msg, f"code={code}")

            print("\n=== 8) 未登记插件 ===")
            set_index(PLUGINS, review_state="reviewed")
            code, body = dispatch(base, "b32p-unknown", "t-b32p-unknown", "qa-mac", plugin="no-such")
            msg = json.dumps(body or {}, ensure_ascii=False)
            check("未登记插件被拒", code in (409, 422) and "未登记" in msg, f"code={code}")

            print("\n=== 8b) 一级规则复核：probes 不过就拒（授权放行也不行） ===")
            bad_dir = os.path.join(PLUGINS, "bad-probe")
            shutil.copytree(os.path.join(ROOT, "plugins", "deploy-smoke"), bad_dir)
            with open(os.path.join(bad_dir, "probes.py"), "w", encoding="utf-8", newline="\n") as f:
                f.write("import sys\nsys.exit(3)\n")
            idx_path = os.path.join(PLUGINS, "index.json")
            with open(idx_path, "r", encoding="utf-8") as f:
                idx = json.load(f)
            idx["plugins"].append({
                "name": "bad-probe", "latest": "1.0.0", "versions": ["1.0.0"],
                "review_state": "reviewed", "granted_to": [],
                "granted_roles": ["ops"], "revoked_instances": [],
            })
            with open(idx_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(idx, f, ensure_ascii=False, indent=2)
                f.write("\n")
            code, body = dispatch(base, "b32p-probe", "t-b32p-probe", "ops-win", plugin="bad-probe")
            msg = json.dumps(body or {}, ensure_ascii=False)
            check("探针不过被拒（授权明明是放行的）",
                  code in (409, 422) and "一级规则复核未通过" in msg, f"code={code}")
            check("审计理由同时写了「岗位命中」和「一级复核未通过」",
                  "岗位命中" in (audit_rows(DB)[-1][4] or "")
                  and "一级规则复核未通过" in (audit_rows(DB)[-1][4] or ""),
                  str(audit_rows(DB)[-1][4])[:90])

            print("\n=== 9) plugin 字段格式非法 ===")
            code, body = dispatch(base, "b32p-badfmt", "t-b32p-badfmt", "qa-mac", plugin="../etc")
            msg = json.dumps(body or {}, ensure_ascii=False)
            check("插件名带路径/大写被拒", code in (409, 422) and "只接受小写字母数字" in msg,
                  f"code={code}")

            print("\n=== 10) 审计 append-only 与值域 ===")
            rows = audit_rows(DB)
            denies = [r for r in rows if r[3] == "deny"]
            allows = [r for r in rows if r[3] == "allow"]
            check("授权判定逐次留痕（8 次判定 = 6 拒 + 2 放行）",
                  len(rows) == 8 and len(denies) == 6 and len(allows) == 2,
                  f"rows={len(rows)} deny={len(denies)} allow={len(allows)}")
            check("格式非法的插件名在入参层拦下（不占审计行，没有有效插件身份）",
                  all(r[1] != "../etc" for r in rows))
            check("audit.decision 只有 allow/deny",
                  all(r[3] in ("allow", "deny") for r in rows))
            check("审计 id 单调，不重写旧行", all(rows[i][0] < rows[i + 1][0] for i in range(len(rows) - 1)))
            check("拒单没有污染任务台账（拒单条数 > 建单条数）",
                  count(DB, "plugin_audit") >= count(DB, "task_ledger"))

            print("\n=== 10b) /api/audit 合并分页（配额 + 插件两源合成一页） ===")
            code, body = request("GET", base + "/api/audit?limit=50")
            items = (body or {}).get("items") or []
            check("审计接口把插件判定也吐出来", code == 200 and len(items) >= 8, f"code={code} n={len(items)}")
            plugin_rows = [i for i in items if str(i.get("command", "")).startswith("plugin:")]
            check("插件行带 plugin/decision 且没有配额字段",
                  plugin_rows and all(i.get("plugin") and i.get("decision") in ("allow", "deny")
                                      and i.get("prev_limit") is None for i in plugin_rows),
                  str(plugin_rows[0])[:120] if plugin_rows else "无插件行")
            snap = [(i.get("created_at"), i.get("command"), i.get("id")) for i in items]
            check("两源合并后按时间倒序（先用解析后时间比，兼容两种时间写法）",
                  _ordered(snap), str(snap)[:220])
            code, body = request("GET", base + "/api/audit?limit=50&command=plugin:deny")
            items2 = (body or {}).get("items") or []
            check("按 command=plugin:deny 只出插件拒单",
                  code == 200 and items2 and all(i.get("command") == "plugin:deny" for i in items2),
                  f"n={len(items2)}")
            code, body = request("GET", base + "/api/audit?limit=50&command=plugin:allow")
            check("按 command=plugin:allow 只出插件放行",
                  code == 200 and len((body or {}).get("items") or []) == 2,
                  f"n={len((body or {}).get('items') or [])}")
            code, body = request("GET", base + "/api/audit?limit=50&instance_id=qa-mac")
            items3 = (body or {}).get("items") or []
            check("按实例过滤时插件行一起收敛到该实例",
                  code == 200 and items3 and all(i.get("instance_id") == "qa-mac" for i in items3),
                  f"n={len(items3)}")
            code, body = request("GET", base + "/api/audit?limit=2")
            page_a = (body or {}).get("items") or []
            code, body = request("GET", base + "/api/audit?limit=2&offset=2")
            page_b = (body or {}).get("items") or []
            check("分页切片落在合并后的总序列上（不是各源各切一刀）",
                  len(page_a) == 2 and len(page_b) == 2 and
                  not set((i.get("command"), i.get("id")) for i in page_a) &
                  set((i.get("command"), i.get("id")) for i in page_b),
                  f"a={len(page_a)} b={len(page_b)}")
            code, body = request("GET", base + "/api/audit?limit=50&command=plugin:nosuch")
            check("未知插件动作返回空页而不是全量", code == 200 and ((body or {}).get("items") or []) == [])

            print("\n=== 11) 接口面仍是 11 条路径 ===")
            code, body = request("GET", base + "/openapi.json")
            paths = sorted((body or {}).get("paths", {}).keys())
            check(f"接口路径条数 = {len(paths)}（红线 11）", len(paths) == 11, f"{paths}")

            print("\n=== 12) 迁移：plugin_audit 在库，任务台账未被改动 ===")
            tables = [r[0] for r in db_query(DB, "select name from sqlite_master where type='table'")]
            check("plugin_audit 表在库", "plugin_audit" in tables)
            cols = [r[1] for r in db_query(DB, "PRAGMA table_info(task_ledger)")]
            check("任务台账仍只有既有列 + 复核三列（没被插广告列）",
                  set(cols) >= {"review_of", "review_verdict", "review_items"} and
                  "plugin" not in cols, f"{len(cols)} 列")
        finally:
            try:
                server.terminate()
                server.wait(timeout=10)
            except Exception:
                try:
                    server.kill()
                except Exception:
                    pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if FAILED:
        print(f"验收结果：失败（{len(FAILED)} 项）")
        for f in FAILED:
            print("  - " + f)
        return 1
    print("验收结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
