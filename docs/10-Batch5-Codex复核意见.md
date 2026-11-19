# Batch5 Codex 独立复核意见（方案 A 执行记录）

- 执行时间：2026-11-19 15:47–15:49
- 方式：**代码内联进 prompt + 禁止读写文件 + 单次作答**（依据 `docs/08-Codex可用性实测.md`：让它自己读文件必撞 429）
- 输入：`app/main.py` 前 45 行 + `app/static/app.js` 两段（渲染与请求封装），共 7,612 字节
- 输出：5 条，耗时约 2 分钟，tokens used 6,231
- **定位：仅供参考，不作验收依据**（单次内联评审召回率有限，见 08 文档第四节）

## 一、Codex 原始输出

```
app/main.py:ensure_schema - 中 - 并发启动时 create_all 非原子可能竞态；缺表清单写入 app.state 并仅警告日志 - 生产改用迁移脚本（批次6），或启动加分布式锁/单进程守护
app/main.py:lifespan - 中 - 仅见前45行，StaticFiles挂载顺序未确认；若 mount("/") 在 include_router 前会劫持 /api 导致 404 - 确保 API 路由注册在 StaticFiles 之前，或静态页改挂 /static
app/static/app.js:api - 低 - fetch 无超时控制，网络异常可能长时间挂起 - 增加 AbortController 超时（如 30s）
app/static/app.js:detail渲染 - 低 - export 链接 href 经 encodeURIComponent 但 download 属性若浏览器回退文件名可能注入 - trace_id 已编码风险极低，可显式校验或移除 download 交由后端 Content-Disposition
app/static/app.js:showResult - 低 - JSON.stringify(payload,null,2) 若载荷极大可能阻塞主线程 - 限制显示长度（如 50KB）或分页展示
```

## 二、逐条裁定

| # | 意见 | 裁定 | 处理 |
| --- | --- | --- | --- |
| 1 | `create_all` 并发竞态、只警告日志 | **成立（中）** | **已改**：`create_all` 包 `try/except SQLAlchemyError`，撞锁时记 ERROR 但不阻断启动；持久化建表仍是 Batch6 迁移脚本的事 |
| 2 | `mount("/")` 在 `include_router` 前会劫持 `/api` | **不成立（误报）** | 不改。它只看到前 45 行，未看到后段：实际挂载的是 `mount("/static")`，且 4 个 `include_router` 全部在挂载之前。实测 `/api/pipelines`、`/api/health`、`/api/commands`、`/api/export` 全 200，鉴权 401 也正常触发——路由没被劫持 |
| 3 | fetch 无超时 | **成立（低）** | **已改**：`api()` 加 `AbortController`，30s 超时（`API_TIMEOUT_MS`） |
| 4 | `download` 属性可能注入 | **不成立（低，风险已压到极低）** | 不改。`trace_id` 经 `encodeURIComponent`，文件名最终由后端 `Content-Disposition` 决定；它自己也标注"风险极低" |
| 5 | `showResult` 大载荷阻塞主线程 | **成立（低）** | **已改**：超过 20,000 字符截断并提示原始长度（`RESULT_MAX_CHARS`） |

**统计：采纳 3 条（1 中 2 低）、证伪 1 条、不改 1 条。**

## 三、我另外自查、它没提的

| # | 问题 | 级别 | 处理 |
| --- | --- | --- | --- |
| 1 | 空库启动时 `/api/pipelines` 直接 500，被中间件吞成 "Internal Server Error" | **高（真缺陷）** | 已修：新增 `ensure_schema()` 启动自检。**这是真实起 uvicorn 才暴露的，TestClient 里假绿** |
| 2 | 状态色块映射靠中文字面量硬编码，新增状态会静默落到 idle 灰色 | 低 | 保留（7 个状态已定稿，取值稳定），验收脚本已断言中文状态值 |
| 3 | 令牌存 `localStorage` | 中 | 保留并记入遗留，正式部署换 HttpOnly Cookie（Batch6） |
| 4 | 页面无自动刷新 | 低 | 保留（刻意，先不做轮询） |

## 四、对方案 A 的评估（本轮实际跑完后的判断）

- **有用**：它确实挑出了 `create_all` 并发、fetch 超时、大载荷渲染三处我漏掉的点，成本只有 2 分钟。
- **局限**：它会基于"不完整的上下文"下结论——第 2 条就是典型误报，因为它只拿到前 45 行。**所以每次内联评审必须在 prompt 里写清"你看到的是片段"，并且我要用实测证据去证伪，不能照单全收。**
- **结论**：方案 A 可常态化，但定位就是"挑刺器"，不是守门员。
