"""Batch27 验收：手机访问（绑定可配 + token + 窄屏），10 项。

守的是什么：
- 绑定地址必须可配，且**默认仍只绑本机**——不能因为开了手机访问，让每台机器默认裸奔；
- 只有平台启动器显式传 -Bind 0.0.0.0，代码默认值不许改；
- 平台开了鉴权，Worker 必须带同一个 token，否则 401（这是最容易漏的一环）；
- token 走 .env、不进版本库、不打印明文；
- 窄屏要能读：单栏 + 表格横向滚，不许把列挤成竖排。
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


runps = read("deploy/run.ps1")
plat_vbs = read("deploy/launch-platform-hidden.vbs")
inst = read("deploy/install-windows-tasks.ps1")
css = read("app/static/style.css")
gitig = read(".gitignore")
sec = read("app/core/security.py")

print("=== A 绑定可配、默认不对外 ===")
check("run.ps1 有 -Bind 参数", "[string]$Bind" in runps)
check("默认值仍是 127.0.0.1（默认不裸奔）", '[string]$Bind = "127.0.0.1"' in runps)
check("起服务用 $Bind，不写死", "--host $Bind" in runps)
check("只有平台启动器显式传 0.0.0.0",
      "-Bind 0.0.0.0" in plat_vbs and "0.0.0.0" not in read("deploy/launch-worker-hidden.vbs"))
check("传了非本机地址时脚本会提示风险", "API_TOKEN 为空" in runps)

print("=== B 鉴权链路完整 ===")
check("security 仍保持「空 token=不校验 / 健康检查放行」",
      "require_token" in sec and "api_token" in sec and "健康检查" in sec)
check("安装脚本从 .env 取 token 拼进 worker 命令行",
      "API_TOKEN" in inst and "worker-run.bat" in inst)
check("安装脚本不打印 token 明文", "不打印内容" in inst)
check(".env 不在版本库", re.search(r"^\.env$", gitig, re.M) is not None)

print("=== C 窄屏可读 ===")
check("有 640px 断点", "@media (max-width: 640px)" in css)
check("表格给最小宽度并横向滚（不压扁列）",
      "min-width: 560px" in css and "overflow: auto" in css)
check("表头在窄屏折行", "flex-wrap: wrap" in css)

print()
if FAILED:
    print(f"结果：{len(FAILED)} 项失败 —— {FAILED}")
    sys.exit(1)
print("结果：全部通过（0 项失败）")
