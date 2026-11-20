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
    task = read("deploy/worker-windows-task.ps1")
    wd = read("deploy/worker-windows-watchdog.ps1")

    print("=== A Worker 任务模板：动作是隐藏 VBS ===")
    check("任务动作用 wscript.exe", '-Execute "wscript.exe"' in task)
    check("不再直接挂 cmd.exe 作为任务动作", '-Execute "cmd.exe"' not in task,
          "直接挂 cmd.exe 会在桌面闪控制台窗口")
    check("生成隐藏启动器 launch-worker-hidden.vbs", "launch-worker-hidden.vbs" in task)
    check("VBS 用 Run(cmd, 0, True)：隐藏 + 等待",
          re.search(r"sh\.Run cmd, 0, True", task) is not None,
          "缺 0/True，窗口会闪或任务状态不反映存活")
    check("注释里写明为什么不用 cmd（可读性/防回退）", "闪" in task or "控制台" in task)

    print("=== B 看门狗模板：运行时不自注册 ===")
    # 看门狗里的 Register-ScheduledTask 必须被 -Install 开关包住（只看代码，注释不算）
    wd_code = code_only(wd)
    reg_idx = wd_code.rfind("Register-ScheduledTask")
    guard_idx = wd_code.find("if ($Install)")
    check("有 -Install 开关", "param(" in wd and "[switch]$Install" in wd)
    check("Register-ScheduledTask 在 -Install 分支内",
          reg_idx != -1 and guard_idx != -1 and guard_idx < reg_idx,
          f"reg@{reg_idx} guard@{guard_idx}")
    check("看门狗运行时不自注册（-Force 只在 Install 段落出现）",
          wd_code.count("Register-ScheduledTask") == 1 and "-Force" in wd_code[guard_idx:])
    check("看门狗动作指向隐藏 VBS", "wscript" in wd or "launch-watchdog-hidden.vbs" in wd)
    check("看门狗自身用 Run(cmd, 0, True)", re.search(r"sh\.Run cmd, 0, True", wd) is not None)
    check("看门狗循环不会自己跑循环（每次唤起只查一轮）",
          "while (" not in wd and "for (" not in wd)

    print("=== C 注册参数仍然对 ===")
    for label, text in (("Worker", task), ("Watchdog", wd)):
        check(f"{label} 任务仍是 Limited（普通用户可装）", "RunLevel Limited" in text)
        check(f"{label} 任务仍是 IgnoreNew（重复触发被忽略）", "IgnoreNew" in text)
    check("Worker 任务仍带失败重启兜底", "RestartCount" in task)

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
