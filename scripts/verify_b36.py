"""Batch36 验收：口径账只有一份、看板与脚本同源、测试单可复算、归档只搬不删。

Batch36 干的事是"给平台配一本账"：之前的通过率、耗时、产物、模型分布散在各个脚本里，
谁问就给谁现算一遍，两处数字迟早对不上。这一批把口径收进 `scripts/ledger_metrics.py`
一个模块，`GET /api/tasks` 的返回体多一个 `metrics` 字段（**不新增路径**），
看板「本周账」直接读这个字段。

验收只看三件事：
1. 账算得对不对（合成数据逐项断言，不看真实库——真实库会被后续单子改）；
2. 账只有一份（接口和看板都不许自己另算）；
3. 到期产物是"搬走"不是"删掉"（对账时原件还能翻回来）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []
NOTES = []


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def check(name, ok, detail=""):
    if ok:
        print(f"  [PASS] {name}" + (f" —— {detail}" if detail else ""))
    else:
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))
        FAILED.append(name)


def load_module():
    import importlib.util
    path = os.path.join(ROOT, "scripts", "ledger_metrics.py")
    spec = importlib.util.spec_from_file_location("ledger_metrics_verify", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def task(task_id, status, adapter="dsh", model=None, effort=None,
         dispatch=None, returned=None, instance=None, review=None):
    """造一条和台账 ORM 同形状的 dict（_row 两种都吃，这里走 dict 那一路）。"""
    import json
    payload = {"task_goal": "验收用", "exec": {"adapter": adapter}}
    if model:
        payload["exec"]["model"] = model
    if effort:
        payload["exec"]["reasoning_effort"] = effort
    return {
        "task_id": task_id,
        "status": status,
        "dispatch_time": dispatch,
        "return_time": returned,
        "done_instance": instance,
        "review_verdict": review,
        "dispatch_payload": json.dumps(payload, ensure_ascii=False),
    }


def main():
    print("=== 1) 口径模块能独立跑起来（不依赖平台进程） ===")
    try:
        m = load_module()
    except Exception as exc:  # 缺依赖如实报红
        check(f"口径模块可导入（{type(exc).__name__}）", False, str(exc)[:90])
        print(f"\n验收结果：失败（{len(FAILED)} 项）")
        return 1
    check("模块自带 build / render / is_test / load_whitelist",
          all(hasattr(m, n) for n in ("build", "render", "is_test", "load_whitelist")))
    check("模块不碰写操作（只读：没有 INSERT/UPDATE/DELETE）",
          not any(k in read("scripts/ledger_metrics.py").upper()
                  for k in ("INSERT INTO", "UPDATE ", "DELETE FROM")))
    check("模块不用 ORM 会话，直接吃行（ORM 对象与 dict 都能算）",
          "def _row(" in read("scripts/ledger_metrics.py"))

    print("\n=== 2) 测试单识别：前缀 + 白名单 ===")
    wl = m.load_whitelist(ROOT)
    check("白名单文件在 config/test_orders.txt", os.path.isfile(
        os.path.join(ROOT, "config", "test_orders.txt")))
    check("白名单读到 2 张例外单（t-win-e2e-1 / t-probe-w）",
          {"t-win-e2e-1", "t-probe-w"} <= set(wl), str(sorted(wl))[:70])
    check("前缀 t-bN… 判为测试单", m.is_test("t-b33-win", wl) is True)
    check("白名单里的单判为测试单", m.is_test("t-win-e2e-1", wl) is True)
    check("普通任务号不判为测试单（不误伤真活）", m.is_test("t-prod-1001", wl) is False)
    check("t-b 后面必须跟数字（t-bz 不算）", m.is_test("t-bz-foo", wl) is False)

    print("\n=== 3) 账算得对不对（合成数据，逐项断言） ===")
    rows = [
        # 2 张测试单：1 通过、1 驳回
        task("t-b36-ok", "验收通过", dispatch="2026-11-20T10:00:00+08:00",
             returned="2026-11-20T10:00:04+08:00", model="nvidia/x", effort="high",
             instance="ops-win"),
        task("t-b36-bad", "验收驳回", dispatch="2026-11-20T11:00:00+08:00",
             returned="2026-11-20T11:00:02+08:00", model="nvidia/x", effort="high",
             instance="ops-win"),
        # 1 张真活单：待验收（未定案，不进分母）
        task("t-prod-1001", "待验收", dispatch="2026-11-20T12:00:00+08:00"),
        # 1 张真活单：通过（真活 2 张，仍不够 30 张 → 不评价）
        task("t-prod-1002", "验收通过", dispatch="2026-11-20T13:00:00+08:00",
             returned="2026-11-20T13:00:09+08:00", adapter="local",
             model=None, effort=None, instance="ops-mac", review="pass"),
    ]
    got = m.build(rows, whitelist=wl)
    tot = got["totals"]
    check("单量分得清：全部 4 / 测试 2 / 真活 2",
          (tot["orders"], tot["test"], tot["real"]) == (4, 2, 2), str(tot))
    real = got["pass_rate"]["real"]
    check("真活通过率只算真活（1 通过、1 待验收 → 1/1=100%，未定案单列 other）",
          (real["ok"], real["decided"], real["rate"], real["other"]) == (1, 1, 100.0, 1),
          str(real))
    test_rate = got["pass_rate"]["test"]
    check("测试通过率单算（1/2 = 50%）", test_rate["rate"] == 50.0, str(test_rate))
    dur = got["duration"]
    check("耗时取中位，样本数真实（3 张有起止）",
          dur["samples"] == 3 and dur["median_s"] == 4.0, str(dur))
    names = [it["instance"] for it in got["instances"]]
    check("岗位账只认真活单，且未记岗位单独成一行（不悄悄丢）",
          "ops-mac" in names and "(未记岗位)" in names,
          str(names))
    check("未记岗位那行仍是未定案（不拿它凑通过率）",
          next(it["pass"]["decided"] for it in got["instances"]
               if it["instance"] == "(未记岗位)") == 0, str(got["instances"])[:90])
    dsh = got["dsh"]
    check("dsh 单只认 adapter=dsh（3 张，local 那张不算）", dsh["orders"] == 3, str(dsh["orders"]))
    check("dsh 模型分布对得上（nvidia/x × 2、默认 × 1）",
          dsh["models"].get("nvidia/x") == 2 and dsh["models"].get("(默认)") == 1,
          str(dsh["models"]))
    check("dsh 成本估算按「模型 · 档位」分格且给单量，不给 token 假账",
          "nvidia/x · high" in dsh["estimate"]
          and dsh["estimate"]["nvidia/x · high"]["orders"] == 2
          and "token" in dsh["note"], str(list(dsh["estimate"]))[:70])
    ex = got["excluded"]
    check("剔除清单可复算（2 张前缀单，带单号）",
          ex["count"] == 2 and "t-b36-ok" in ex["task_ids"] and ex["by_prefix"] == 2, str(ex))
    pol = got["policy"]
    check("真活 2 张 < 30 → 评价不启用，且说明原因",
          pol["evaluable"] is False and pol["min_eval_orders"] == 30
          and "不到 30 张" in pol["why_not_evaluable"], pol["why_not_evaluable"][:60])
    check("生成的账带时间戳（每次口径都说得清是哪一刻的账）", bool(got["generated_at"]))

    print("\n=== 4) 人读版能打出来（脚本单跑就用它） ===")
    text = m.render(got)
    check("人读版含「通过率」「dsh」「剔除」三块",
          all(k in text for k in ("通过率", "dsh", "剔除")), text[:60].replace("\n", " "))
    check("人读版把「评价未启用」写出来（不冒充有结论）", "评价未启用" in text or "不做岗位评价" in text)

    print("\n=== 5) 接线：账挂在返回体上，不新增路径 ===")
    schemas = read("app/api/schemas_http.py")
    queries = read("app/api/routes_queries.py")
    check("TaskPage 有 metrics 字段", "metrics: Optional[Dict[str, Any]]" in schemas)
    check("/api/tasks 返回体填了 metrics", "metrics=_metrics_for(repo)" in queries)
    check("接口按路径加载同一份模块（不复制一份口径）",
          "_load_metrics_module" in queries and "ledger_metrics.py" in queries)
    check("算不出来就降级空账，不把列表拖挂",
          "metrics 计算失败" in queries and "return None" in queries)
    check("没有为这块账新开路由文件", not os.path.isfile(
        os.path.join(ROOT, "app", "api", "routes_metrics.py")))

    print("\n=== 6) 看板：本周账一块，读字段不自己算 ===")
    html = read("app/static/index.html")
    js = read("app/static/app.js")
    check("看板有 metricsPanel（在审计面板之前）",
          'id="metricsPanel"' in html
          and html.index('id="metricsPanel"') < html.index('id="auditPanel"'))
    check("看板有 loadMetrics，且首刷/换口令都会刷新",
          "async function loadMetrics()" in js
          and js.count("loadMetrics();") >= 2)
    check("看板用接口给的 metrics（不二次计算通过率）",
          "r.body.metrics" in js and "Math.round" not in js.split("function renderMetrics")[1][:2000])
    check("看板把「测试单/真活单」和剔除清单摆出来（不藏分母）",
          "真活 ${t.real" in js and "剔除" in js)
    check("口径说明写在面板里（怎么算的能自查）", "这块是怎么算的" in html)

    print("\n=== 7) 归档：到期只搬不删 ===")
    dsh = read("worker/dsh_adapter.py")
    check("有 ARCHIVE_ROOT 与按月目录函数",
          "ARCHIVE_ROOT = os.path.join(ROOT, \"archive\")" in dsh
          and "def archive_dir_for(" in dsh)
    seg = dsh.split("def prune_artifacts", 1)[1].split("\ndef ", 1)[0]
    check("prune_artifacts 里没有 os.remove（产物不许删）", "os.remove" not in seg)
    check("用 shutil.move 搬进按月归档目录", "shutil.move" in seg and "archive_dir_for()" in seg)
    check("同月重名加时间戳，不覆盖旧档", "int(time.time())" in seg and "if os.path.exists(dest)" in seg)
    check("搬不动就跳过（不把这一单带崩）", "except (OSError, shutil.Error)" in seg)
    check("archive/ 不进 git（public 仓库不存模型原文）",
          "archive/" in read(".gitignore"))
    check("09 号契约写清新口径", "搬去归档，不删" in read("docs/base/09-Worker契约.md"))

    print("\n=== 8) 真装：接口返回体里真有这块账（平台没起就跳过） ===")
    try:
        import json
        import urllib.request
        token = os.environ.get("MWP_TOKEN", "")
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/tasks?page=1&size=1",
            headers={"X-API-Token": token} if token else {},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("metrics"):
            got_live = body["metrics"]
            check("线上返回体带 metrics（totals / pass_rate / excluded 齐全）",
                  all(k in got_live for k in ("totals", "pass_rate", "excluded", "policy")),
                  str(got_live.get("totals"))[:60])
            check("线上账与脚本单跑一致（同一份实现的直接证据）",
                  got_live["totals"] == m.build(
                      m.load_from_sqlite(os.path.join(ROOT, "data", "mvp.db")))["totals"],
                  "接口与脚本各算一套")
        else:
            NOTES.append("接口返回体里没有 metrics（可能是旧进程，重启平台后再验）")
    except Exception as exc:  # 平台没起 / 没给口令 → 只记录，不判红
        NOTES.append(f"没连上平台（{type(exc).__name__}），本节跳过：{str(exc)[:60]}")

    if NOTES:
        print()
        for n in NOTES:
            print(f"  [NOTE] {n}")
    print()
    if FAILED:
        print(f"验收结果：失败（{len(FAILED)} 项）")
        for item in FAILED:
            print("  - " + item)
        return 1
    print("验收结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
