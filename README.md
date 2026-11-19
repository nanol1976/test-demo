# Master-Worker 主控集群平台 · 文档分支

[![ci](https://github.com/nanol1976/test-demo/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/nanol1976/test-demo/actions/workflows/ci.yml)

> 本分支（`main`）**只放文档，不放代码**。代码、测试、CI 门禁全在 [`master`](https://github.com/nanol1976/test-demo/tree/master) 分支。

给「多 Agent 主控集群」用的基础设施层：收报文、排队、路由、落台账、判超时/心跳/熔断、提供查询与看板。

**业务无关**——它不认识"订单""持仓""回测"，只认协议字段和状态机。量化、回测只是演示用例，任何业务都能套。

## 一句话定位

> Master 负责判断，Worker 负责干，平台只负责**传输 + 记账 + 兜底**。

## 目录

| 想看什么 | 去哪 |
| --- | --- |
| 代码、脚本、跑测试 | [`master` 分支](https://github.com/nanol1976/test-demo/tree/master) |
| 架构与协议（四层架构、协议 v1.0、七状态机） | [docs/base/00-索引目录.md](docs/base/00-索引目录.md) |
| 基线规范 v1.0 | [docs/00-基线规范-v1.0.md](docs/00-基线规范-v1.0.md) |
| 每个 Batch 的验收单（Batch2~8） | [docs/13-Batch8-验收单.md](docs/13-Batch8-验收单.md) |
| 迁移怎么用（含 `--baseline` 接管老库） | [docs/base/13-迁移规范.md](docs/base/13-迁移规范.md) |
| CI 怎么接（GitHub / Gitee / 内网 GitLab） | [docs/14-CI接入指南.md](docs/14-CI接入指南.md) |
| Master / Worker 契约 | [docs/base/08-Master主契约.md](docs/base/08-Master主契约.md)、[docs/base/09-Worker契约.md](docs/base/09-Worker契约.md) |
| 故障案例 | [docs/base/10-故障案例.md](docs/base/10-故障案例.md) |

## 分支约定

| 分支 | 用途 | 内容 |
| --- | --- | --- |
| `master` | 代码分支（**默认分支**） | `app/`、`scripts/`、`deploy/`、`migrations/`、`.github/workflows/ci.yml`、依赖、测试 |
| `main` | 文档分支 | `README.md` + `docs/`，无代码、无 workflow |

- 往 `main` 提文档不会触发 CI；CI 只在 `master` 上跑（默认分支）。
- 代码改动一律进 `master`；验收单 / 规范 / 接入指南进 `main`。
- tag（`batch1-r2` … `batch8-r4`）挂在 `master` 的提交上，文档分支不含 tag。

## 本地怎么看文档

```bash
git clone https://github.com/nanol1976/test-demo.git
cd test-demo           # 默认分支 master，含代码
git checkout main      # 切到文档分支，只剩 README + docs/
```

## 文档边界（明确不做）

- 不写具体业务逻辑（业务永远在 Worker 侧）
- 不做数据/指标定义（那是业务域的事）
- 不做部署以外的运维手册

## 状态

- 代码分支最近一次 CI：见顶部徽章（`master`）
- 文档分支最后一次整理：2026-11-19，从 `master` 的 `1531fb0` 拆出，剥离代码与 workflow
