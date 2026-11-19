"""显式迁移入口（替代"启动时自动建表"的兜底做法）。

启动自检 ensure_schema() 只是兜底，生产应显式迁移。本脚本把版本化迁移串起来：

用法：
    python deploy/migrate.py                # 升级到最新版本
    python deploy/migrate.py --check        # 只检查，不改库（上线前预检）
    python deploy/migrate.py --status       # 列出每个版本与是否已应用
    python deploy/migrate.py --to 2         # 只升到 v002
    python deploy/migrate.py --dry-run      # 只打印将要执行什么

退出码：0 成功 / 通过；1 不通过或失败。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import create_engine, inspect  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.models import Base  # noqa: E402
from migrations import runner  # noqa: E402


def connect():
    settings = get_settings()
    url = settings.database_url
    return create_engine(url, future=True), url


def flag(name: str) -> bool:
    return name in sys.argv


def target_version():
    if "--to" not in sys.argv:
        return None
    idx = sys.argv.index("--to")
    if idx + 1 >= len(sys.argv):
        print("--to 需要一个版本号，例如 --to 2")
        raise SystemExit(1)
    try:
        return int(sys.argv[idx + 1])
    except ValueError:
        print(f"--to 的版本号不是整数：{sys.argv[idx + 1]}")
        raise SystemExit(1)


def main() -> int:
    check_only = flag("--check")
    show_status = flag("--status")
    dry_run = flag("--dry-run")
    target = target_version()

    engine, url = connect()
    print(f"数据库：{url}")

    tables_needed = [t.name for t in Base.metadata.sorted_tables]
    existing = set(inspect(engine).get_table_names())
    missing = [t for t in tables_needed if t not in existing]
    ver = runner.current_version(engine)
    latest = runner.latest_available()

    print(f"期望表：{tables_needed}")
    print(f"现有表：{sorted(existing)}")
    print(f"schema_version：{ver}（目标 {latest}）")

    if show_status:
        print("")
        print(f"{'版本':<8}{'状态':<8}{'说明'}")
        for row in runner.status_rows(engine):
            state = "已应用" if row["applied"] else "待应用"
            print(f"v{row['version']:03d}    {state:<8}{row['note']}")
        return 0

    pend = runner.pending(engine)
    if target is not None:
        pend = [m for m in pend if m.version <= target]

    if check_only:
        ok = not missing and ver >= (target or latest)
        print(f"预检结果：{'通过' if ok else '不通过'}")
        if not ok:
            detail = []
            if missing:
                detail.append(f"缺表：{missing}")
            if ver < (target or latest):
                detail.append(f"版本落后：{ver} < {target or latest}")
            print("  " + "；".join(detail))
            if pend:
                print(f"  待应用迁移：{[f'v{m.version:03d}' for m in pend]}")
            return 1
        return 0

    if dry_run:
        if not pend:
            print("没有待应用的迁移")
        for m in pend:
            print(f"（dry-run）将应用 v{m.version:03d}：{m.note}")
        return 0

    # 表被手工删掉但版本已记录的情况：版本号骗不了人，但缺表必须补，否则服务起不来
    if missing and ver > 0:
        print(f"→ 补建缺失表（版本已记录但表缺失，属异常场景）：{missing}")
        Base.metadata.create_all(engine)

    applied = runner.upgrade(engine, target=target, log=lambda msg: print(msg))
    if not applied:
        print("→ 无待应用迁移（已是最新）")

    after = set(inspect(engine).get_table_names())
    still = [t for t in tables_needed if t not in after]
    final = runner.current_version(engine)
    print(f"schema_version：{ver} → {final}")
    print(f"迁移完成，缺表：{still or '无'}")

    if pend and not applied:
        # 有待应用却没执行，说明被 --to 限制住了
        print(f"（因 --to {target} 限制，仍有 {len(pend)} 条未应用）")
    return 0 if not still else 1


if __name__ == "__main__":
    raise SystemExit(main())
