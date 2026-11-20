"""Batch25 验收：explorer 守护 + data/ 每日巡检（10 项）。

守的是什么：
- 守护件必须在仓库里（不能再出现"只活在某台机器的 run\ 里"）；
- MW-Guard 必须由同一个安装脚本注册，动作必须是 wscript 跑隐藏 VBS（否则每 2 分钟闪黑窗）；
- 守护脚本必须带 UTF-8 BOM 且各文件 CRLF（PS 5.1 无 BOM 会按 ANSI 解析中文，直接语法崩）；
- 守护可以清 data/ 残留，但绝不能碰 mvp.db；
- 守护运行时不自注册任务（要装就转发给 install-windows-tasks.ps1）。
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def check(label, ok, detail=""):
    if ok:
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" —— {detail}" if detail else ""))
        FAILED.append(label)


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def read_bytes(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


GUARD = "deploy/explorer-guard.ps1"
VBS = "deploy/launch-guard-hidden.vbs"
INST = "deploy/install-windows-tasks.ps1"

print("=== A 守护件在仓库里 ===")
check("explorer 守护脚本在仓库", os.path.exists(os.path.join(ROOT, GUARD)))
check("隐藏启动器在仓库", os.path.exists(os.path.join(ROOT, VBS)))

guard = read(GUARD)
vbs = read(VBS)
inst = read(INST)

print("=== B 一条命令装得上 ===")
check("安装脚本注册 MW-Guard", '"MW-Guard"' in inst)
check("MW-Guard 走隐藏 VBS（不闪窗）", "launch-guard-hidden.vbs" in inst)
check("隐藏启动器用 wscript 跑、Run 参数 0 不建控制台",
      "explorer-guard.ps1" in vbs and 'sh.Run cmd, 0, True' in vbs)
check("启动器路径由脚本自身位置推出（不写死用户名）",
      "GetParentFolderName" in vbs and "C:\\Users" not in vbs)

print("=== C 编码红线 ===")
bom_ok = all(read_bytes(r)[:3] == b"\xef\xbb\xbf" for r in (GUARD, INST))
check("ps1 带 UTF-8 BOM", bom_ok)
crlf_ok = all(b"\r\n" in read_bytes(r) and read_bytes(r).count(b"\n") == read_bytes(r).count(b"\r\n")
              for r in (GUARD, INST, VBS))
check("部署件是 CRLF（VBS/PS1 的换行语义不允许被悄悄改成 LF）", crlf_ok)

print("=== D 生产库绝不动 ===")
body = "\n".join(l for l in guard.splitlines() if not l.strip().startswith("#"))
check("mvp.db 在保留白名单里", '$keep = @("mvp.db")' in guard)
check("清理只清白名单，别的只报告", "不在清理白名单里" in guard)
check("守护运行时不自注册任务", "Register-ScheduledTask" not in body)

print("=== E 文档写了怎么用 ===")
doc = read("docs/base/11-启动自检清单.md")
check("自检清单有 J 节且写了 MW-Guard",
      "## J. explorer 守护" in doc and "MW-Guard" in doc and "explorer-guard.log" in doc)

print()
if FAILED:
    print(f"结果：{len(FAILED)} 项失败 —— {FAILED}")
    sys.exit(1)
print("结果：全部通过（0 项失败）")
