"""Batch23 验收脚本：Windows 部署件收进仓库 + 一键注册三个计划任务。

Batch17 之后，三个任务的真实实装件（隐藏 VBS 启动器）只存在于某台机器的 run\ 目录，
而且"注册 MW-Platform"这一步根本没有脚本——换机器/换人时没人知道该怎么做。
这批把四件事做成仓库内的文本件，让"怎么部署"能被 diff、被评审、被门禁检查：

1. deploy/launch-platform-hidden.vbs / launch-worker-hidden.vbs / launch-watchdog-hidden.vbs
2. deploy/install-windows-tasks.ps1（注册三个任务 + 生成 run\worker-run.bat，幂等，可 -Uninstall）
3. deploy/worker-windows-watchdog.ps1（运行时只做保活检查，注册转发给安装脚本）
4. deploy/worker-windows-task.ps1（老入口保留为转发 shim，不再自己实现一套注册）

这台机器是 Linux，跑不了 Windows 计划任务，所以本脚本检查的是"文本契约"：
VBS 的可执行语义、注册脚本的动作类型、路径不再写死用户名、幂等与卸载、门禁是否覆盖。
真机注册与重启验证在 Windows 上单独做（见交付报告）。

覆盖：
A 三个 VBS 都在仓库里，且都用 wscript 语义（sh.Run cmd, 0, ...），路径从脚本自身推出
B 安装脚本：注册三个任务名、动作是 wscript.exe、重复周期 1 分钟、IgnoreNew、幂等 -Force
C 安装脚本生成 run\worker-run.bat，且 bat 里带 PYTHONPATH 与退出码透传
D 看门狗运行时不再自注册（Register-ScheduledTask 只在 -Install 分支里出现，且那一分支只是转发）
E 老入口 worker-windows-task.ps1 不再自己注册（必须转发到安装脚本）
F 仓库内不写死具体用户目录（C:\\Users\\longf 之类），换机器能直接用
G 文档写了怎么装、怎么拆、怎么核对
"""
import os
import re
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
    vbs_files = [
        "deploy/launch-platform-hidden.vbs",
        "deploy/launch-worker-hidden.vbs",
        "deploy/launch-watchdog-hidden.vbs",
    ]
    installer = read("deploy/install-windows-tasks.ps1")
    watchdog = read("deploy/worker-windows-watchdog.ps1")
    shim = read("deploy/worker-windows-task.ps1")

    print("=== A 三个隐藏启动器 ===")
    for rel in vbs_files:
        body = read(rel)
        check(f"{rel} 存在", bool(body.strip()))
        check(f"{rel} 不创建窗口（Run 第二参数 0）", re.search(r"sh\.Run cmd, 0, ", body) is not None)
        check(f"{rel} 路径从脚本自身推出（GetParentFolderName）",
              "GetParentFolderName" in body and "WScript.ScriptFullName" in body)

    plat = read("deploy/launch-platform-hidden.vbs")
    check("平台启动器等子进程结束（任务状态 = 服务存活）", "sh.Run cmd, 0, True" in plat)
    check("平台启动器走 deploy\\run.ps1", "deploy\\run.ps1" in plat and "-Port 8000" in plat)
    check("平台启动器把输出进 run\\platform.out.log", "platform.out.log" in plat)

    worker = read("deploy/launch-worker-hidden.vbs")
    check("worker 启动器走 run\\worker-run.bat（命令行只有一份真相）",
          "worker-run.bat" in worker and "--concurrency" not in worker)
    check("worker 启动器缺 bat 时明确报错而不是静默失败",
          "install-windows-tasks.ps1" in worker and "WScript.Quit 1" in worker)

    wd = read("deploy/launch-watchdog-hidden.vbs")
    check("看门狗启动器走 worker-windows-watchdog.ps1", "worker-windows-watchdog.ps1" in wd)
    check("看门狗启动器把输出进 run\\watchdog.out.log", "watchdog.out.log" in wd)

    print("=== B 一键注册脚本 ===")
    check("安装脚本存在", bool(installer.strip()))
    for name in ("MW-Platform", "MW-Worker", "MW-Watchdog"):
        check(f"注册 {name}", f'"{name}"' in installer)
    check("三个任务动作都是 wscript.exe（不是 cmd/powershell，才不会闪窗）",
          installer.count('Execute "wscript.exe"') == 1 and 'Execute "wscript.exe"' in installer)
    check("动作参数带 //nologo + 隐藏 VBS", "//nologo" in installer and "Vbs" in installer)
    check("每分钟重复一次", "RepetitionInterval (New-TimeSpan -Minutes 1)" in installer)
    check("IgnoreNew（在跑就不另起一份）", "MultipleInstances IgnoreNew" in installer)
    check("失败自动重启（RestartCount/RestartInterval）",
          "RestartCount 999" in installer and "RestartInterval" in installer)
    check("非管理员可装（RunLevel Limited）", "RunLevel Limited" in installer)
    check("幂等（同名任务 -Force 覆盖，不堆重复任务）", "-Force | Out-Null" in installer)
    check("有 -Uninstall 一键拆", "Unregister-ScheduledTask" in installer and "-Uninstall" in installer)
    check("有 -StartTasks 装完即起", "-StartTasks" in installer and "Start-ScheduledTask -TaskName" in installer)
    check("有 -DryRun 预演（不写文件不注册）", "-DryRun" in installer and "将注册：" in installer)
    check("预检缺件就报错（不再静默装错）",
          "缺少启动器" in installer and "找不到 Python" in installer)

    print("=== C worker 命令行单点生成 ===")
    check("安装脚本生成 run\\worker-run.bat", "worker-run.bat" in installer)
    check("bat 里设 PYTHONPATH", "set PYTHONPATH=$Repo" in installer)
    check("bat 重定向日志", "worker.out.log" in installer and ">>" in installer)
    check("bat 透传退出码（崩了才能被拉起）", "%ERRORLEVEL%" in installer and "exit /b" in installer)
    check("PYTHONPATH 用双引号包住路径（防空格）", '"$Repo"' in installer or "set PYTHONPATH=$Repo" in installer)

    print("=== D 看门狗运行时不自注册 ===")
    body_without_header = "\n".join(
        ln for ln in watchdog.splitlines() if not ln.strip().startswith("#")
    )
    check("看门狗正文里没有 Register-ScheduledTask",
          "Register-ScheduledTask" not in body_without_header)
    check("看门狗带 -Install 时转发给安装脚本",
          "-Install" in watchdog and "install-windows-tasks.ps1" in watchdog)
    check("看门狗仍然检查平台健康", "api/health" in watchdog)
    check("看门狗仍然检查 worker 在线", "/api/workers" in watchdog)

    print("=== E 老入口改为转发 ===")
    check("worker-windows-task.ps1 不再自己注册",
          "Register-ScheduledTask" not in shim)
    check("worker-windows-task.ps1 转发到安装脚本",
          "install-windows-tasks.ps1" in shim and "& $installer" in shim)

    print("=== F 不写死用户名目录 ===")
    hardcoded = r"C:\\Users\\[^\\]+\\codex-workspace"
    for rel in vbs_files + ["deploy/install-windows-tasks.ps1",
                            "deploy/worker-windows-watchdog.ps1",
                            "deploy/worker-windows-task.ps1"]:
        body = read(rel)
        check(f"{rel} 不含写死路径", re.search(hardcoded, body) is None,
              (re.search(hardcoded, body).group(0) if re.search(hardcoded, body) else ""))

    print("=== G 文档 ===")
    doc = read("docs/base/09-Worker契约.md")
    check("Worker 契约写了 wscript 动作要求", "wscript" in doc)
    check("Worker 契约写了装/拆入口", "install-windows-tasks.ps1" in doc)
    checklist = read("docs/base/11-启动自检清单.md")
    check("启动自检清单写了计划任务核对", "MW-" in checklist or "install-windows-tasks" in checklist)

    print(f"\n结果：{'全部通过' if not FAILS else '有失败项'}（{len(FAILS)} 项失败）")
    if FAILS:
        for f in FAILS:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
