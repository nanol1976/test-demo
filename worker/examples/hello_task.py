"""最小执行样例：Worker 侧任务脚本长什么样。

Worker 只负责"按 payload.exec 跑一个 .py 并回执"，具体干什么由任务脚本决定。
这个样例不碰平台、不碰数据库，只按约定把产出写到 artifacts/ 下。

约定（跟平台无关，是 Worker 与任务脚本之间的私有约定）：
- 入参：位置参数原样来自 `payload.exec.args`；
- 产出：写到 `artifacts/<name>`，用 --artifact 声明以便回执里核验存在性；
- 退出码：0 = 成功；非 0 = 失败，Worker 会转成 task_reject 并带上 stderr 尾部。
"""
import argparse
import json
import os
from datetime import datetime, timezone


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="hello")
    p.add_argument("--artifact", default="artifacts/hello.txt")
    p.add_argument("--fail", action="store_true", help="故意失败，用于验证 reject 链路")
    args = p.parse_args()

    out_path = os.path.abspath(args.artifact)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    content = {
        "label": args.label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "这是 Worker 执行端产出的样例文件，用来证明流水线真的跑通了而不是演的",
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(content, fh, ensure_ascii=False, indent=2)
    print(f"written: {out_path}")
    if args.fail:
        print("人为失败：--fail 指定", file=os.sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
