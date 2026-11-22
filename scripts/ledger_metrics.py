"""台账口径模块（Batch36 P0）——所有指标只在这里算一次。

为什么单独成模块：脚本 `python scripts/ledger_metrics.py` 与 `GET /api/tasks`
返回体里的 `metrics` 字段共用同一份实现，于是「脚本与看板误差 0」不是靠人对，
而是同源。改口径只改这一个文件，看板跟着变。

三条硬规矩：
1. **只读**：不写库、不改状态、不删文件；
2. **剔除要能说清**：测试单（`t-bN…` 前缀 + `config/test_orders.txt` 白名单）
   默认不计入评价，但必须把剔掉的单号列出来，不许"大概不算它"；
3. **不编数**：回执里没有 token usage，成本只给「模型 + 档位 + 耗时」小计，
   明确标注为估算口径。

用法：
    python scripts/ledger_metrics.py            # 人读表格
    python scripts/ledger_metrics.py --json     # 机器读（看板/别处复用）
"""
import json
import os
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "mvp.db")

TEST_PREFIX = re.compile(r"t-b\d+")
WHITELIST_REL = os.path.join("config", "test_orders.txt")
# 真活单不到这个数，只出账不出评价（样本太小，评价等于噪音）
MIN_EVAL_ORDERS = 30

PASS = "验收通过"
BAD = ("验收驳回", "执行失败")


def load_whitelist(root=ROOT):
    """读测试单白名单：一行一个 task_id，`#` 后面是注释。文件不存在就当空表。"""
    ids = set()
    path = os.path.join(root, WHITELIST_REL)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    ids.add(line)
    except OSError:
        pass
    return ids


def is_test(task_id, whitelist=None):
    tid = str(task_id or "")
    return bool(TEST_PREFIX.match(tid)) or tid in (whitelist or set())


def _row(task):
    """台账行 → 纯 dict。ORM 对象和 dict 两种输入都吃。"""
    if isinstance(task, dict):
        return task
    return {
        "task_id": task.task_id,
        "status": task.status,
        "dispatch_time": getattr(task, "dispatch_time", None),
        "return_time": getattr(task, "return_time", None),
        "done_instance": getattr(task, "done_instance", None),
        "target_instance": getattr(task, "target_instance", None),
        "dispatch_payload": getattr(task, "dispatch_payload", None),
        "review_of": getattr(task, "review_of", None),
        "review_verdict": getattr(task, "review_verdict", None),
        "remaining_retry": getattr(task, "remaining_retry", 0),
        "max_retry": getattr(task, "max_retry", 3),
    }


def _dt(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _payload(row):
    raw = row.get("dispatch_payload")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        got = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return got if isinstance(got, dict) else {}


def _adapter(row):
    exec_spec = _payload(row).get("exec") or {}
    return str(exec_spec.get("adapter") or "").strip().lower()


def _pass_rate(rows):
    ok = sum(1 for r in rows if r.get("status") == PASS)
    bad = sum(1 for r in rows if r.get("status") in BAD)
    decided = ok + bad
    rate = round(ok / decided * 100, 1) if decided else None
    return {
        "orders": len(rows),
        "ok": ok,
        "bad": bad,
        "other": len(rows) - ok - bad,
        "decided": decided,
        # 一单都没定案时给 None，不硬报 0%——0% 会被误读成"全砸了"
        "rate": rate,
    }


def _row_secs(r):
    """单张单的耗时（秒）；没有起止或明显是脏数据就返回 None。"""
    a, b = _dt(r.get("dispatch_time")), _dt(r.get("return_time"))
    if not a or not b:
        return None
    gap = (b - a).total_seconds()
    # 负数或超过 7 天的多半是脏数据（时区/手填），不参与耗时口径
    if not (0 <= gap < 7 * 86400):
        return None
    return gap


def _durations(rows):
    secs = []
    for r in rows:
        gap = _row_secs(r)
        if gap is not None:
            secs.append(gap)
    secs.sort()
    if not secs:
        return {"samples": 0, "median_s": None, "mean_s": None, "max_s": None}
    return {
        "samples": len(secs),
        "median_s": round(secs[len(secs) // 2], 1),
        "mean_s": round(sum(secs) / len(secs), 1),
        "max_s": round(secs[-1], 1),
    }


def _artifact_stats():
    """产物只看「本机能看到的」：优先复用平台索引，拿不到就自己数目录。"""
    try:
        from app.services import artifacts as art
        info = art.scan()
    except Exception:  # noqa: BLE001 —— 脚本在没有 app 依赖的环境也要能跑
        info = {}
        art_dir = os.path.join(ROOT, "run", "artifacts")
        if os.path.isdir(art_dir):
            for name in os.listdir(art_dir):
                if name.endswith(".log"):
                    full = os.path.join(art_dir, name)
                    try:
                        info[name[:-4]] = {"path": full, "bytes": os.path.getsize(full)}
                    except OSError:
                        continue
    sizes = [int(v.get("bytes") or 0) for v in info.values()]
    return {
        "files": len(info),
        "bytes": sum(sizes),
        "max_bytes": max(sizes) if sizes else 0,
        "task_ids": sorted(info.keys())[:20],
    }


def build(tasks, whitelist=None):
    """算全套指标。`tasks` 可以是 ORM 行，也可以是 dict 列表。返回可 JSON 化的 dict。"""
    wl = load_whitelist() if whitelist is None else set(whitelist)
    rows = [_row(t) for t in tasks]

    groups = {"all": [], "test": [], "real": []}
    for r in rows:
        kind = "test" if is_test(r.get("task_id"), wl) else "real"
        groups[kind].append(r)
        groups["all"].append(r)

    pass_rates = {k: _pass_rate(v) for k, v in groups.items()}

    # 岗位只看真活单：测试单的岗位分布没有评价意义
    inst_rows = defaultdict(list)
    for r in groups["real"]:
        inst_rows[r.get("done_instance") or "(未记岗位)"].append(r)
    instances = []
    for name, rs in sorted(inst_rows.items(), key=lambda kv: -len(kv[1])):
        instances.append({
            "instance": name,
            "pass": _pass_rate(rs),
            "duration": _durations(rs),
        })

    # dsh：模型/档位/耗时（成本口径就靠这三样，且明确是估算）
    dsh_rows = [r for r in rows if _adapter(r) == "dsh"]
    models, efforts = defaultdict(int), defaultdict(int)
    cell = defaultdict(lambda: {"orders": 0, "secs": []})
    for r in dsh_rows:
        spec = _payload(r).get("exec") or {}
        model = str(spec.get("model") or "(默认)").strip() or "(默认)"
        effort = str(spec.get("reasoning_effort") or "(默认)").strip() or "(默认)"
        models[model] += 1
        efforts[effort] += 1
        c = cell[f"{model} · {effort}"]
        c["orders"] += 1
        sec = _row_secs(r)
        if sec is not None:
            c["secs"].append(sec)
    dsh = {
        "orders": len(dsh_rows),
        "pass": _pass_rate(dsh_rows),
        "duration": _durations(dsh_rows),
        "models": dict(sorted(models.items(), key=lambda kv: -kv[1])),
        "efforts": dict(sorted(efforts.items(), key=lambda kv: -kv[1])),
        # 成本估算：只有"跑了多久"，没有 token；档位高低只作人工参考。
        # 单量永远有，耗时样本可能为 0（老 dsh 回执没落起止时间），此时 samples=0、别编数。
        "estimate": {
            k: {"orders": v["orders"], "samples": len(v["secs"]),
                "total_s": round(sum(v["secs"]), 1) if v["secs"] else None,
                "median_s": round(statistics.median(v["secs"]), 1) if v["secs"] else None}
            for k, v in sorted(cell.items())
        },
        "note": "成本为估算口径：模型 + 档位 + 耗时，不含 token（回执里没有 usage 字段）",
    }

    review = {
        "pass": sum(1 for r in rows if r.get("review_verdict") == "pass"),
        "fail": sum(1 for r in rows if r.get("review_verdict") == "fail"),
        "unreviewed": sum(1 for r in rows if r.get("review_verdict") == "unreviewed"),
    }

    excluded = {
        "count": len(groups["test"]),
        "by_prefix": sum(1 for r in groups["test"] if TEST_PREFIX.match(str(r.get("task_id") or ""))),
        "by_whitelist": sum(1 for r in groups["test"]
                            if not TEST_PREFIX.match(str(r.get("task_id") or ""))),
        "task_ids": [r.get("task_id") for r in groups["test"]][:20],
    }

    policy = {
        "test_prefix": TEST_PREFIX.pattern,
        "whitelist_file": WHITELIST_REL.replace(os.sep, "/"),
        "min_eval_orders": MIN_EVAL_ORDERS,
        "real_orders": len(groups["real"]),
        "evaluable": len(groups["real"]) >= MIN_EVAL_ORDERS,
        "why_not_evaluable": (
            "" if len(groups["real"]) >= MIN_EVAL_ORDERS else
            f"真活单只有 {len(groups['real'])} 张，不到 {MIN_EVAL_ORDERS} 张，"
            "先出账、不做岗位评价（样本太小，评价等于噪音）"
        ),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "orders": len(rows),
            "test": len(groups["test"]),
            "real": len(groups["real"]),
        },
        "pass_rate": pass_rates,
        "duration": _durations(rows),
        "instances": instances,
        "dsh": dsh,
        "artifacts": _artifact_stats(),
        "review": review,
        "excluded": excluded,
        "policy": policy,
    }


def load_from_sqlite(path=DB_PATH):
    """脚本入口：直接读真库，把行喂给同一个 `build()`。"""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT task_id, status, dispatch_time, return_time, done_instance, target_instance, "
            "dispatch_payload, review_of, review_verdict, remaining_retry, max_retry FROM task_ledger"
        ).fetchall()]
    finally:
        con.close()


def _fmt_rate(p):
    return "—" if p["rate"] is None else f"{p['rate']}%"


def render(m):
    lines = []
    lines.append(f"口径生成时间：{m['generated_at']}")
    lines.append(f"任务总数 {m['totals']['orders']}（测试单 {m['totals']['test']} / 真活单 {m['totals']['real']}）")
    lines.append("")
    lines.append("一、通过率（通过 ÷（通过 + 驳回 + 失败））")
    for key, label in (("real", "真活"), ("test", "测试"), ("all", "全部")):
        p = m["pass_rate"][key]
        lines.append(f"  {label}: {p['ok']}/{p['decided']} = {_fmt_rate(p)}"
                     + (f"（另有 {p['other']} 张未定案）" if p["other"] else ""))
    lines.append("")
    d = m["duration"]
    lines.append(f"二、耗时（有起止时间的 {d['samples']} 单）"
                 + (f"：中位 {d['median_s']}s / 均值 {d['mean_s']}s / 最长 {d['max_s']}s"
                    if d["samples"] else "：无样本"))
    lines.append("")
    lines.append("三、岗位（只看真活单）")
    if not m["instances"]:
        lines.append("  真活单为 0，暂无岗位账")
    for it in m["instances"]:
        p, dur = it["pass"], it["duration"]
        lines.append(f"  {it['instance']:<12} 单 {p['orders']:>3} / 通过率 {_fmt_rate(p):>6}"
                     + (f" / 中位耗时 {dur['median_s']}s" if dur["samples"] else ""))
    lines.append("")
    lines.append(f"四、dsh（{m['dsh']['orders']} 单，通过率 {_fmt_rate(m['dsh']['pass'])}）")
    for k, v in m["dsh"]["models"].items():
        lines.append(f"  模型 {k} × {v}")
    for k, v in m["dsh"]["efforts"].items():
        lines.append(f"  档位 {k} × {v}")
    lines.append(f"  ※ {m['dsh']['note']}")
    lines.append("")
    a = m["artifacts"]
    lines.append(f"五、产物：{a['files']} 份 / {a['bytes']} B / 最大 {a['max_bytes']} B")
    r = m["review"]
    lines.append(f"六、复核结论：pass {r['pass']} / fail {r['fail']} / unreviewed {r['unreviewed']}")
    lines.append("")
    e = m["excluded"]
    lines.append(f"七、剔除清单：{e['count']} 张（前缀 {e['by_prefix']} / 白名单 {e['by_whitelist']}）")
    if e["task_ids"]:
        lines.append("  " + "、".join(str(x) for x in e["task_ids"]))
    p = m["policy"]
    lines.append("")
    lines.append(f"口径：测试单 = {p['test_prefix']} 前缀 + {p['whitelist_file']} 白名单；"
                 f"真活单 {p['real_orders']} 张，评价{'已' if p['evaluable'] else '未'}启用")
    if p["why_not_evaluable"]:
        lines.append(f"  ※ {p['why_not_evaluable']}")
    return "\n".join(lines)


def main(argv):
    rows = load_from_sqlite()
    metrics = build(rows)
    if "--json" in argv:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(render(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
