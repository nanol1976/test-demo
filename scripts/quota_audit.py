"""查并发配额下发审计（Batch16）。

为什么不加成接口：接口面 10 个路径是跟用户约定过的，加审计查询要先征得同意。
审计表本身就是落库的，先用 CLI 读，等确认要往上长再做接口。

用法：
    python scripts/quota_audit.py                      # 最近 20 条
    python scripts/quota_audit.py --limit 50
    python scripts/quota_audit.py --worker workbuddy
    python scripts/quota_audit.py --instance DESKTOP-C1V00J3
    python scripts/quota_audit.py --json               # 机器可读
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="并发配额下发审计")
    p.add_argument("--worker", default="", help="只看某个执行方")
    p.add_argument("--instance", default="", help="只看某个实例")
    p.add_argument("--limit", type=int, default=20, help="最多返回几条（默认 20）")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    args = p.parse_args(argv)

    sys.path.insert(0, ROOT)
    from app.api.deps import get_session_factory  # noqa: WPS433
    from app.models import WorkerQuotaAudit, WorkerRegistry  # noqa: WPS433
    from sqlalchemy import select  # noqa: WPS433

    from sqlalchemy.exc import OperationalError  # noqa: WPS433
    db = get_session_factory()()
    try:
        stmt = select(WorkerQuotaAudit).order_by(WorkerQuotaAudit.id.desc()).limit(max(1, args.limit))
        if args.worker:
            stmt = stmt.where(WorkerQuotaAudit.worker_id == args.worker)
        if args.instance:
            stmt = stmt.where(WorkerQuotaAudit.instance_id == args.instance)
        try:
            rows = db.execute(stmt).scalars().all()
        except OperationalError:
            print("（当前库还没迁移到 v008，没有 worker_quota_audit 表；先跑 deploy/migrate.py）")
            return 2
        out = [{
            "id": r.id,
            "worker_id": r.worker_id,
            "instance_id": r.instance_id,
            "prev_limit": r.prev_limit,
            "desired_limit": r.desired_limit,
            "command": r.command,
            "reason": r.reason,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]
        current = db.execute(select(WorkerRegistry)).scalars().all()
        cur_map = {f"{c.worker_id}/{c.instance_id}": c.desired_limit for c in current}
    finally:
        db.close()

    if args.json:
        print(json.dumps({"total_shown": len(out), "current": cur_map, "items": out},
                         ensure_ascii=False, indent=2))
        return 0

    if not out:
        print("（还没有任何配额下发记录）")
    else:
        print(f"{'id':>4}  {'目标':<34} {'旧→新':<9} {'时间':<26} 原因")
        for r in out:
            target = f"{r['worker_id']}/{r['instance_id']}"
            print(f"{r['id']:>4}  {target:<34} {r['prev_limit']}→{r['desired_limit']:<6} "
                  f"{(r['created_at'] or '')[:19]:<26} {r['reason'] or '—'}")
    print("\n当前注册表里的期望值：")
    if not cur_map:
        print("  （注册表为空）")
    for k, v in sorted(cur_map.items()):
        print(f"  {k}  desired_limit={v}{'（未下发）' if not v else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
