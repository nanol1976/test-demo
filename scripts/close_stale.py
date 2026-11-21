"""人工收尾滞留流水线（Batch31-E 遗留处置工具）。

为什么需要它：平台只有下发、暂停、恢复、熔断复位、优雅停止五种指令，
没有「关掉一条卡了很久的单」。历史上的滞留单只能靠人翻库改状态——绕过服务层，
既没留痕也不设防。这个脚本把这件事变成一次可审的动作：

  1. 默认 dry-run，只打印将要发生什么；加 `--apply` 才写库；
  2. 只允许处置 start_time 早于 N 小时前的流水线（默认 24h），防手滑关掉正在跑的单；
  3. 只推进状态、不删任何行——台账铁律照旧；
  4. 收尾理由写进任务的 `log_ref`，事后能回答"谁为什么关的"。

用法：
    python scripts/close_stale.py --trace t-win-e2e --verdict pass --reason "探针 exit 0，无验收标准，据回执验收"
    python scripts/close_stale.py --trace t-win-e2e --verdict pass --reason "..." --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api.deps import get_engine  # noqa: E402
from app.core.clock import ensure_utc  # noqa: E402
from app.models import PipelineLedger, TaskLedger  # noqa: E402
from app.repositories.ledger import LedgerRepository  # noqa: E402

# 未完结的任务状态（需要被这次收尾推进）
OPEN_TASK_STATUSES = {"待拆解", "待调度下发", "执行中", "待验收"}

VERDICTS = {
    "pass": ("验收通过", "success"),
    "fail": ("执行失败", "failed"),
    "abort": ("执行失败", "aborted"),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="人工收尾滞留流水线")
    ap.add_argument("--trace", required=True, help="trace_id")
    ap.add_argument("--verdict", required=True, choices=sorted(VERDICTS), help="pass/fail/abort")
    ap.add_argument("--reason", required=True, help="收尾理由（写进任务 log_ref）")
    ap.add_argument("--min-age-hours", type=int, default=24,
                    help="只处置开始时间早于 N 小时的流水线（默认 24，防手滑）")
    ap.add_argument("--apply", action="store_true", help="真的写库（缺省只演练）")
    args = ap.parse_args()

    task_status, pipeline_status = VERDICTS[args.verdict]
    Session = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
    db = Session()
    try:
        repo = LedgerRepository(db)
        pipeline = repo.get_pipeline(args.trace)
        if pipeline is None:
            print(f"找不到流水线 {args.trace}")
            return 2

        start = ensure_utc(pipeline.start_time) if pipeline.start_time else None
        age = (datetime.now(timezone.utc) - start) if start else timedelta(0)
        print(f"流水线 {args.trace} 当前={pipeline.overall_status} 起始={start} 已滞留={age}")

        if age < timedelta(hours=args.min_age_hours):
            print(f"拒绝处置：滞留 {age} 未满 {args.min_age_hours} 小时，可能仍在推进")
            return 3

        tasks = db.execute(
            select(TaskLedger).where(TaskLedger.trace_id == args.trace)
        ).scalars().all()
        todo = [t for t in tasks if t.status in OPEN_TASK_STATUSES]
        print(f"任务 {len(tasks)} 条，其中未完结 {len(todo)} 条：")
        for t in tasks:
            mark = "→ " + task_status if t.status in OPEN_TASK_STATUSES else "  保持"
            print(f"  {mark} {t.task_id} [{t.status}]")

        if not args.apply:
            print(f"\n演练结束。要真写：加 --apply")
            print(f"将置任务={task_status}，流水线={pipeline_status}，理由={args.reason}")
            return 0

        now = datetime.now(timezone.utc)
        for t in todo:
            t.log_ref = f"manual:close {now.isoformat()} reason={args.reason}"
            repo.update_task_status(t.task_id, task_status)
        repo.update_pipeline_status(args.trace, pipeline_status, end_time=now)
        print(f"\n已收尾：{len(todo)} 条任务 → {task_status}，流水线 → {pipeline_status}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
