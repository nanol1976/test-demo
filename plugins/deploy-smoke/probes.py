#!/usr/bin/env python3
"""deploy-smoke · 部署后三秒冒烟（Batch32-2 首个插件）。

不连运行中的服务，只核仓库里的实物：
  c1 健康检查路由在位且声明 /api/health
  c2 迁移目录最高版本等于 --schema
  c3 文档索引（docs/base/00-索引目录.md）在位且非空

用法：
    python plugins/deploy-smoke/probes.py <repo_path> --schema 11

stdout 只打一行 JSON：
    {"plugin":"deploy-smoke","verdict":"pass|fail","items":[{...}]}
退出码：0 全过 / 1 有未过项 / 2 参数不合法。纯标准库。
"""
import argparse
import json
import os
import re
import sys

PLUGIN = "deploy-smoke"


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _check_health(repo):
    """健康路由 = routes_health.py 里声明 /health，main.py 用 /api 前缀挂上去。"""
    routes = os.path.join(repo, "app", "api", "routes_health.py")
    main_py = os.path.join(repo, "app", "main.py")
    body = _read(routes)
    mounted = _read(main_py)
    if not body:
        return False, "", "app/api/routes_health.py 不存在或为空"
    if '@router.get("/health")' not in body and "@router.get('/health')" not in body:
        return False, "app/api/routes_health.py", "routes_health.py 里没有声明 /health"
    if "health_router" not in mounted or 'prefix="/api"' not in mounted:
        return False, "app/api/routes_health.py", "main.py 没有把 health_router 挂在 /api 前缀上"
    return True, "routes_health.py:/health + main.py:prefix=/api  → /api/health", ""


def _check_schema(repo, target):
    versions_dir = os.path.join(repo, "migrations", "versions")
    tops = {}
    if os.path.isdir(versions_dir):
        for fn in os.listdir(versions_dir):
            m = re.match(r"^v(\d+)", fn)
            if m:
                tops[int(m.group(1))] = fn
    if not tops:
        return False, "", "migrations/versions 里没有 vNNN 迁移文件"
    top = max(tops)
    evidence = "最高 v%03d（%s）" % (top, tops[top])
    if target is None:
        return True, evidence + "；未给 --schema，不判等", "未给 --schema，只报不判"
    if top != target:
        return False, evidence, "迁移最高版本 v%03d ≠ 目标 v%03d" % (top, target)
    return True, evidence, ""


def _check_docs(repo):
    path = os.path.join(repo, "docs", "base", "00-索引目录.md")
    if not os.path.exists(path):
        return False, "", "docs/base/00-索引目录.md 不存在"
    size = os.path.getsize(path)
    if size < 200:
        return False, "%d 字节" % size, "索引目录疑似空壳（小于 200 字节）"
    return True, "%d 字节" % size, ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="deploy-smoke 冒烟探针")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--schema", type=int, default=None,
                    help="本版应达到的迁移版本号；不给则只报最高版本")
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        print(json.dumps({"plugin": PLUGIN, "verdict": "fail", "items": [],
                          "note": "仓库路径不存在：%s" % repo}, ensure_ascii=False))
        return 2

    items = []
    for cid, fn, desc in (
        ("c1", lambda: _check_health(repo), "健康检查路由在位且声明 /api/health"),
        ("c2", lambda: _check_schema(repo, args.schema), "迁移目录最高版本等于目标版本"),
        ("c3", lambda: _check_docs(repo), "文档索引在位"),
    ):
        ok, evidence, reason = fn()
        items.append({"id": cid, "ok": bool(ok), "desc": desc,
                      "evidence": evidence, "reason": reason})

    verdict = "pass" if all(i["ok"] for i in items) else "fail"
    payload = {"plugin": PLUGIN, "verdict": verdict, "items": items}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
