"""Batch24 验收脚本：库地址不再指向空库 + data/ 卫生规则。

起因：`app/core/config.py` 的默认库地址一直写着 `./data/mw_platform.db`——
那是个历史遗留的空库名（表结构全齐、一行数据没有）。生产之所以没出事，
只是因为 `deploy/run.ps1` 显式设了 `DATABASE_URL=./data/mvp.db`；
一旦有人手敲 `uvicorn app.main:app`（不带环境变量），连上的就是这个空壳：
看板不报错、数据全空，第一反应永远是"数据被删了"。

这批做两件事：
1. 默认库地址改回 mvp.db，并把测试脚本借用的空库名换成自己的独立名字；
2. 定一条 data/ 卫生规则（留 catted 生产库 + 最新备份，测试库与空壳一律清掉），写进文档，由门禁盯着。

覆盖：
A 默认库地址 = data/mvp.db
B 代码/脚本/部署件里不再出现 mw_platform（历史归档文档不算）
C run.ps1 仍然显式钉死 mvp.db（双保险，不靠默认值）
D .gitignore 挡住库文件与备份库（库不进版本库）
E 文档写了 data/ 留什么、删什么
F 仓库里没有残留库文件被跟踪（git 层面不该出现 *.db）
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def read(rel):
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        return ""
    return open(path, encoding="utf-8", errors="replace").read()


def main() -> int:
    cfg = read("app/core/config.py")
    run_ps1 = read("deploy/run.ps1")
    gitignore = read(".gitignore")

    print("=== A 默认库地址 ===")
    check("默认库地址是 data/mvp.db",
          'database_url: str = "sqlite:///./data/mvp.db"' in cfg, cfg.splitlines()[0] if cfg else "")
    check("默认值旁边写了为什么（防回退）", "Batch24" in cfg and "空库" in cfg)

    print("=== B 不再引用空库名 ===")
    offenders = []
    for rel in ("app/core/config.py", "deploy/run.ps1", "deploy/migrate.py",
                "scripts/verify_r2.py", "app/main.py"):
        if "mw_platform" in read(rel):
            offenders.append(rel)
    check("代码与部署件里没有 mw_platform", not offenders, ", ".join(offenders))
    check("verify_r2 用独立测试库名", 'DB = "data/verify_r2.db"' in read("scripts/verify_r2.py"))

    print("=== C 部署脚本显式钉库（不靠默认值）===")
    check("run.ps1 显式设 DATABASE_URL 到 mvp.db",
          'DATABASE_URL = "sqlite:///./data/mvp.db"' in run_ps1, "run.ps1 没钉库地址")

    print("=== D 库文件不进版本库 ===")
    check(".gitignore 忽略 data/*.db", "data/*.db" in gitignore)
    check(".gitignore 忽略 data/*.db.bak*", "data/*.db.bak*" in gitignore)

    print("=== E 文档写了 data/ 卫生规则 ===")
    doc = read("docs/base/11-启动自检清单.md")
    check("文档说明 data/ 留什么", "mvp.db" in doc and ("备份" in doc or "bak" in doc))
    check("文档说明空库/测试库要清", "mw_platform" in doc or "测试库" in doc)

    print("=== F 版本库里没有库文件 ===")
    try:
        out = subprocess.run(["git", "ls-files", "data"], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
    except Exception as exc:  # pragma: no cover
        out = f"<git 不可用: {exc}>"
    check("git 未跟踪任何 data/ 下的库文件", out == "", out[:80])

    print(f"\n结果：{'全部通过' if not FAILS else '有失败项'}（{len(FAILS)} 项失败）")
    if FAILS:
        for f in FAILS:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
