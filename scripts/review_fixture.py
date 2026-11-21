"""复核实测夹具（Batch32-1）：造 N 个产物文件，供 artifacts 存在性比对。

为什么要它：异构复核判的是「验收标准有没有被逐条答满」，而"答满"的证据是
执行者真交出来的产物。没有产物就无法构造正例/负例，验收就变成看日志说话。

用法：
    python scripts/review_fixture.py --files 3 --out run/b32pos
输出：一行 JSON，列出实际落盘的文件（供人工核对；平台从 artifacts 声明里读存在性）。
"""
import argparse
import json
import os
import sys
from datetime import datetime


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="造 N 个产物文件")
    parser.add_argument("--files", type=int, default=3)
    parser.add_argument("--out", default="run/b32_fixture")
    args = parser.parse_args(argv)
    if args.files < 0 or args.files > 20:
        print("--files 必须在 0..20 之间", file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    written = []
    for i in range(1, args.files + 1):
        path = os.path.join(args.out, f"part{i}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"part {i} of {args.files}\n生成时间 {stamp}\n")
        written.append(os.path.relpath(path))
    print(json.dumps({"files": written, "count": len(written)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
