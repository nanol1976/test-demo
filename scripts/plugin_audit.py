#!/usr/bin/env python3
"""插件授权审计只读查询（Batch32-2 派单侧）。

为什么不走接口：接口面是 11 条红线，加路径要另开一批。审计表先提供只读 CLI，
谁被拒过、为什么被拒，命令行一条就能看到；接看板留到下一批。

用法：
    python scripts/plugin_audit.py                       # 最近 20 条
    python scripts/plugin_audit.py --plugin deploy-smoke
    python scripts/plugin_audit.py --instance qa-mac --limit 50
    python scripts/plugin_audit.py --db sqlite:///./data/mvp.db
退出码：0 有结果 / 1 空 / 2 输入不合法。纯标准库。
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "mvp.db")


def db_path(raw):
    if not raw:
        return DEFAULT_DB
    if raw.startswith("sqlite:///"):
        return raw[len("sqlite:///"):]
    if raw.startswith("sqlite://"):
        return raw[len("sqlite://"):]
    return raw


def main(argv=None):
    ap = argparse.ArgumentParser(description="插件授权审计（只读）")
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--plugin")
    ap.add_argument("--instance")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--only-deny", action="store_true")
    args = ap.parse_args(argv)

    path = db_path(args.db)
    if not os.path.exists(path):
        print("输入不合法：库文件不存在 %s" % path, file=sys.stderr)
        return 2

    sql = ("select created_at, trace_id, task_id, plugin_name, plugin_version, "
           "worker_id, instance_id, decision, reason from plugin_audit where 1=1")
    params = []
    if args.plugin:
        sql += " and plugin_name = ?"
        params.append(args.plugin)
    if args.instance:
        sql += " and instance_id = ?"
        params.append(args.instance)
    if args.only_deny:
        sql += " and decision = 'deny'"
    sql += " order by id desc limit ?"
    params.append(max(1, min(args.limit, 200)))

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if not rows:
        print("没有符合条件的审计记录（库：%s）" % path)
        return 1

    print("%-20s %-8s %-20s %-16s %-14s %-14s %-6s %s"
          % ("时间", "判定", "插件", "版本", "worker", "岗位", "任务", "理由"))
    for created, trace, task, name, ver, worker, inst, decision, reason in rows:
        stamp = str(created)[:19]
        print("%-20s %-8s %-20s %-16s %-14s %-14s %-6s %s"
              % (stamp, decision, name, ver or "-", worker, inst,
                 (task or "-")[-6:], (reason or "")[:80]))
    print("共 %d 条（库：%s）" % (len(rows), path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
