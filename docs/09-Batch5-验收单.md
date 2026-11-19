# Batch5 验收单：极简前端（台账看板 / 详情 / 暂停恢复 / 报文投递）

- 执行方：主审落地；Codex 按方案 A 做"内联代码单点复核"（意见仅供参考，不作验收依据）
- 执行机：Windows DESKTOP-C1V00J3，`.venv311`（Python 3.11）
- 执行日期：2026-11-19
- 验收脚本：`scripts/verify_b5.py`

## 一、交付物

| # | 文件 | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | `app/static/index.html` | 新增 | 单页：流水线列表 + 详情 + 报文投递 + 归档查询 |
| 2 | `app/static/app.js` | 新增 | fetch 封装 / 渲染 / 指令下发 / 模板填充 |
| 3 | `app/static/style.css` | 新增 | 样式，状态色块 |
| 4 | `app/main.py` | 修改 | 挂载 `/` = index.html、`/static` = 静态目录；**新增 `ensure_schema()` 启动自检** |
| 5 | `scripts/verify_b5.py` | 新增 | 7 组断言（含空库自检与接口一致性） |
| 6 | `docs/09-Batch5-验收单.md` | 新增 | 本文件 |

前端零依赖：不引框架、不引 npm、不引 CDN，一个 html + 一个 js + 一个 css，离线可用。

## 二、页面能力对照（每个按钮都对应真接口）

| 页面动作 | 调的接口 |
| --- | --- |
| 流水线列表 / 状态筛选 / 翻页 | `GET /api/pipelines` |
| 点行看详情（任务表 + 状态色块） | `GET /api/pipelines/{trace_id}` |
| 暂停派发 / 恢复派发 / 复位熔断 | `POST /api/commands` |
| 投递报文（含 8 种模板一键填入） | `POST /api/messages` |
| 导出 JSON / CSV | `GET /api/export/{trace_id}?format=` |
| 归档索引行数 | `GET /api/archive/{trace_id}` |
| 顶部连接检查 | `GET /api/health` |

## 三、真实输出（Windows 实跑）

```
=== A 静态资源可访问 ===
  [PASS] 首页 200 — status=200
  [PASS] 首页含标题
  [PASS] 首页引用 app.js
  [PASS] 首页引用 style.css
  [PASS] 首页有报文投递面板
  [PASS] 首页有暂停/恢复按钮
  [PASS] 首页有流水线详情区
  [PASS] app.js 200 — status=200
  [PASS] style.css 200 — status=200
  [PASS] app.js 有请求超时控制
  [PASS] app.js 有返回值截断
=== B 前端引用的接口必须真实存在 ===
  [PASS] 前端引用的 /api 路径都存在 — 未匹配=[]
  前端引用路径：['/api/commands', '/api/health', '/api/messages', '/api/pipelines', '/api/tasks']
=== C 不引入新的写接口 ===
  [PASS] 方法集合仍只有 GET/POST — ['GET', 'POST']
  [PASS] 无 DELETE/PUT/PATCH
  [PASS] 首页与静态资源不写台账
=== D 端到端：页面靠的接口真能用 ===
  [PASS] 投递派发 202
  [PASS] 流水线列表 200
  [PASS] 列表能查到刚投的 trace
  [PASS] 列表项带 pause_flag 字段
  [PASS] 详情 200
  [PASS] 详情带 tasks
  [PASS] 任务状态执行中
  [PASS] 暂停 200 / paused=true
  [PASS] 恢复 200 / paused=false
  [PASS] 导出 JSON 200 / 导出 CSV 200 / CSV 带附件头
  [PASS] 归档查询 200
=== E 鉴权占位：静态页免鉴权、接口要令牌 ===
  [PASS] 配了令牌后首页仍可访问 — status=200
  [PASS] 配了令牌后接口 401 — status=401
  [PASS] 带正确令牌 200 — status=200
=== F 健康与收尾 ===
  [PASS] health 200
  [PASS] 首页 HTML 有 charset
=== G 空库启动自检（真实踩到的缺陷）===
  [PASS] 空库被识别为缺表 — missing=['message_archive', 'pipeline_ledger', 'task_ledger']
  [PASS] 自检后表已建齐
  [PASS] 自检幂等（第二次不缺表） — again=[]
结果：全部通过
```

## 四、真实服务实测（TestClient 之外的第二重证据）

用 `uvicorn` 真起服务（127.0.0.1:8792），用 HTTP 直接打：

```
首页 status=200
api/pipelines status=200 body={"meta":{"total":0,"page":1,"size":5,"pages":1},"items":[]}
health status=200
app.js status=200 长度=13675
css status=200 长度=3273
```

## 五、自测中修掉的缺陷（都是真撞出来的）

| # | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | 真实起服务后 `/api/pipelines` 返回 **500 Internal Server Error** | 空库直接起服务，表不存在，第一个查询就炸，而 500 被中间件吞成一句话，看不出是缺表 | 新增 `ensure_schema()` 启动自检：启动时比对模型表与实际表，缺失则自动补建并 WARNING 日志；`app.state.missing_tables` 留痕 |
| 2 | 自检断言写不通（`missing=[]`） | `get_engine()` 带 `lru_cache`，测试里替换模块变量无效 | `ensure_schema(engine=None)` 改为可显式传 engine，测试直接传临时引擎 |

第一条值得记：这不是"测试没覆盖到"，是**换个启动方式就暴露的真缺陷**。TestClient 里脚本先 `create_all` 了，所以假绿；真实起服务才现形。教训：**只靠 TestClient 会漏"启动路径"的缺陷**。

## 六、回归（前四批）

- `verify_b2.py`：全部通过
- `verify_b3.py`：全部通过
- `verify_b4.py`：全部通过

## 七、监理侧扫描

- 业务词 0 命中；删除类调用 0 处；接口方法集合仍只有 GET / POST（9 个接口未增加）
- 前端渲染统一走 `textContent`，只有"页脚/空态提示"用固定字符串拼 `innerHTML`，不含外部字段
- 静态页不参与鉴权（否则令牌要写进 HTML），令牌由页面输入后放 `localStorage` 并随请求头带出

## 八、Codex 独立复核意见（方案 A）

见 `docs/10-Batch5-Codex复核意见.md`。结论：**仅供参考**，不作为验收依据。

## 九、遗留

1. **无自动刷新**：看板靠手动刷新，没做轮询/websocket。实时性要求不高，先不做。
2. **无删除/编辑**：刻意不做——台账铁律是只追加与状态更新。
3. **令牌存 localStorage**：XSS 场景下有泄漏面；当前渲染全部 `textContent`，风险已压低，但正式部署应换 HttpOnly Cookie 或后端会话（Batch6）。
4. **`ensure_schema` 自动建表**是兜底不是方案：生产要走迁移（Batch6）。
5. **静态目录硬编码**在 `app/static`，未做成配置项（够用，暂不改）。
6. **多 worker 并发启动**时自动建表可能撞一次，SQLite 下 `create_all` 幂等（IF NOT EXISTS），风险低但未做并发压测。
