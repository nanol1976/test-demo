"""Batch17 验收脚本：Windows 计划任务静默化（不再弹控制台窗口）。

背景（现场现象）：
  三台计划任务（MW-Platform / MW-Worker / MW-Watchdog）在"仅当用户登录时"运行，
  动作直接挂 cmd.exe / powershell.exe。控制台程序在交互会话里被启动时，
  Windows 会先创建控制台窗口，`-WindowStyle Hidden` 只是事后隐藏 → 闪一下；
  更糟的是旧版看门狗每次运行都用 Register-ScheduledTask -Force 重新注册自己，
  等于每分钟重建任务 + 重置主体与触发器 → 窗口反复出现、调度抖动。

本批的契约（都在模板里，静态可验）：
1. 三个任务的**动作都是 wscript.exe 跑隐藏 VBS**，不直接挂 cmd.exe/powershell.exe
2. VBS 用 `sh.Run cmd, 0, True`：0 = 不创建窗口，True = 等子进程结束（任务状态=服务存活）
3. 看门狗**运行时不自注册**，只有显式 -Install 才注册
4. 注册出来的任务仍是 Limited + IgnoreNew（普通用户可装，重复触发被忽略）

Windows 侧运行态证据（本脚本在 Linux CI 上无法直接跑 PowerShell，故只做静态契约；
运行态证据见 Batch17 交付报告的实测记录）。
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def code_only(text):
    """去掉块注释 <# ... #> 与行注释，只留可执行代码，避免注释里的字样干扰判断。"""
    text = re.sub(r"<#[\s\S]*?#>", "", text)
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    inst = read("deploy/install-windows-tasks.ps1")
    wd = read("deploy/worker-windows-watchdog.ps1")
    vbs_worker = read("deploy/launch-worker-hidden.vbs")
    vbs_wd = read("deploy/launch-watchdog-hidden.vbs")
    vbs_plat = read("deploy/launch-platform-hidden.vbs")

    print("=== A 三个任务的动作用隐藏 VBS（Batch23：启动器已入库） ===")
    check("任务动作用 wscript.exe", '-Execute "wscript.exe"' in inst)
    check("不再直接挂 cmd.exe 作为任务动作", '-Execute "cmd.exe"' not in inst,
          "直接挂 cmd.exe 会在桌面闪控制台窗口")
    check("隐藏启动器在仓库里（不是装在某人机器的 run\\ 里）",
          all(x in inst for x in ("launch-platform-hidden.vbs", "launch-worker-hidden.vbs",
                                  "launch-watchdog-hidden.vbs")))
    for name, body in (("platform", vbs_plat), ("worker", vbs_worker), ("watchdog", vbs_wd)):
        check(f"{name} 启动器用 Run(cmd, 0, True)：隐藏 + 等待",
              re.search(r"sh\.Run cmd, 0, True", body) is not None,
              "缺 0/True，窗口会闪或任务状态不反映存活")
    check("注释里写明为什么不用 cmd（可读性/防回退）",
          any("控制台" in b or "闪" in b for b in (vbs_plat, vbs_worker, vbs_wd)))

    print("=== B 看门狗：运行时彻底不自注册 ===")
    wd_code = code_only(wd)
    check("有 -Install 开关（兼容老入口）", "param(" in wd and "[switch]$Install" in wd)
    check("看门狗正文里没有任何 Register-ScheduledTask",
          "Register-ScheduledTask" not in wd_code, "注册逻辑必须只在安装脚本里")
    check("-Install 分支转发给 install-windows-tasks.ps1",
          "install-windows-tasks.ps1" in wd)
    check("看门狗循环不会自己跑循环（每次唤起只查一轮）",
          "while (" not in wd and "for (" not in wd)
    check("看门狗仍然查平台健康与 Worker 在线",
          "api/health" in wd and "/api/workers" in wd)

    print("=== C 注册参数仍然对（看真正注册的那份脚本） ===")
    check("三个任务都登记（平台/Worker/看门狗）",
          all(f'"{n}"' in inst for n in ("MW-Platform", "MW-Worker", "MW-Watchdog")))
    check("任务仍是 Limited（普通用户可装）", "RunLevel Limited" in inst)
    check("任务仍是 IgnoreNew（重复触发被忽略）", "MultipleInstances IgnoreNew" in inst)
    check("任务仍带失败重启兜底", "RestartCount" in inst)
    check("注册幂等（重跑不堆重复任务）", "-Force | Out-Null" in inst)

    print("=== D 文档同步 ===")
    doc = read("docs/base/09-Worker契约.md")
    check("Worker 契约里写了 Windows 静默启动方式",
          "wscript" in doc or "隐藏 VBS" in doc or "launch-worker-hidden" in doc)

    print(f"\n结果：{'全部通过' if not FAILS else '有失败项'}（{len(FAILS)} 项失败）")
    if FAILS:
        for f in FAILS:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
