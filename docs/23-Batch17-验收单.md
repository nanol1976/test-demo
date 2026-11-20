# Batch17 验收单 —— Windows 计划任务静默化（不再弹控制台窗口）

## 现象

`MW-Platform` / `MW-Worker` / `MW-Watchdog` 三个任务在"仅当用户登录时"运行，桌面上反复出现控制台窗口。

## 根因（两条，都已实测确认）

1. **控制台程序的动作必然先建窗口。** 任务动作直接挂 `cmd.exe` / `powershell.exe` 时，Windows 在交互会话里先创建控制台窗口，`-WindowStyle Hidden` 只是进程起来之后把窗口隐藏 —— 看到的就是"闪一下"。看门狗每分钟跑一次，所以每分钟闪一次。
2. **看门狗每次运行都在重建自己。** 旧版脚本末尾无条件执行 `Register-ScheduledTask -Force` 重建 `MW-Watchdog`：主体被重置回"仅当用户登录时"、触发器被重置成"现在 +1 分钟"，任务每分钟重建一次，调度抖动 + 新实例 + 新窗口一起出现。这是"不停跳出来"的主要放大器。

## 修复

| 项 | 改成 |
| --- | --- |
| 三个任务的动作 | `wscript.exe //nologo <隐藏 VBS>`，**不再直接挂 cmd/powershell** |
| VBS 启动方式 | `sh.Run cmd, 0, True` —— `0` 不创建控制台窗口；`True` 等子进程结束 |
| 为什么要 `True` | 任务状态才真实等于"服务还活着"；否则 wscript 秒退、任务每分钟被重复触发，反复尝试绑 8000 端口 |
| 看门狗自注册 | 删掉；只有显式 `-Install` 才注册（并且注册出来的动作也是隐藏 VBS） |
| 注册参数 | 仍 `RunLevel Limited`（普通用户可装）+ `IgnoreNew`（重复触发被忽略）+ Worker 的 `RestartCount 999` 兜底 |

## 现场验证（DESKTOP-C1V00J3）

- 改前：`MW-Watchdog` 动作 `powershell.exe ... -WindowStyle Hidden`；脚本末段无条件 `Register-ScheduledTask -Force`
- 改后：动作 `wscript.exe //nologo ...\deploy\launch-watchdog-hidden.vbs`；脚本只有 `-Install` 才注册
- 观察 75 秒（跨一个周期）：`LastRun 10:10:10 → Result 0`，`NextRun 10:11:11` 正常滚动，**没有残留 wscript/powershell 进程**
- Platform/Worker 用等待式隐藏 VBS 重启后：`/api/health` = ok，`workbuddy/DESKTOP-C1V00J3 online=true age=6.1s`，两条链各一个 python，任务状态 `Running`
- 另注：`Register-ScheduledTask -LogonType S4U`（"不管用户是否登录都运行"，连窗口都不在会话 1 里）在非管理员账号下返回"拒绝访问"，本机放弃该方案，改用隐藏 VBS；需要 S4U 的话得以管理员身份注册

## 门禁

`verify_b17.py` 静态契约 4 组：动作是 wscript / VBS 用 `0, True` / 看门狗只在 `-Install` 注册 / 注册参数仍是 Limited + IgnoreNew；外加文档同步检查。`ci.py` 同口径 4 条红线，`run_all.py` 纳入 Batch17（16 套件）。

## 未做

- 没把"隐藏启动"扩到 `worker-systemd.service` / `launchd.plist`：Linux/macOS 没有这个问题。
- 没有加"窗口是否真的没出现"的自动化断言（需要截屏比对），只做了进程与会话层面的证据。
