# deploy-smoke · 部署后三秒冒烟

## 什么时候用

平台改完代码、迁移上完、计划任务重启之后，用它判断"这一版是不是真的起来了"。
它不连运行中的服务，只核对仓库里的实物，所以可以在没有端口权限的机器上跑，也可以作为一级规则复核，机器秒级出结论。

## 怎么跑

```
python plugins/deploy-smoke/probes.py <repo_path> --schema 11
```

- `repo_path`：仓库根（默认当前目录）
- `--schema`：本版应达到的迁移版本号（整数）。不给就只报"最高版本是多少"，不判定

stdout 只打一行 JSON：`{"plugin":"deploy-smoke","verdict":"pass|fail","items":[{id,ok,evidence,reason}]}`。
退出码 0 = 全过，1 = 有条没过，2 = 参数不合法。

## 三条判据

| 编号 | 判据 | 看过的东西 |
| --- | --- | --- |
| c1 | 健康检查路由在位且声明 `/api/health` | `app/api/` 里的路由文件正文 |
| c2 | 迁移目录最高版本等于 `--schema` 给的版本 | `migrations/versions/` 文件名里的 `vNNN` |
| c3 | 文档索引在位（`docs/base/00-索引目录.md`） | 文件是否存在、非空 |

## 结论怎么用

- `pass`：这一版可以进入异构复核（B32-1），把 `criteria_template` 三条作为验收标准下发。
- `fail`：直接在派发侧拦下，别浪费一台复核机。fail 里 `reason` 会点出是哪一条没过。

## 边界

- 本插件不重启服务、不碰数据库，只读文件。
- 判据里凡是"看文件"的，都不代表运行时状态；运行时健康仍以 `/api/health` 的 200 为准（那是另一层）。
