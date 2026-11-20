/* Batch5 极简前端。纯 fetch 调已有接口，零依赖、无构建。 */
const ROUTES = {
  health: "/api/health",
  messages: "/api/messages",
  pipelines: "/api/pipelines",
  tasks: "/api/tasks",
  pipelineDetail: (t) => `/api/pipelines/${encodeURIComponent(t)}`,
  exportJson: (t) => `/api/export/${encodeURIComponent(t)}?format=json`,
  exportCsv: (t) => `/api/export/${encodeURIComponent(t)}?format=csv`,
  archive: (t) => `/api/archive/${encodeURIComponent(t)}`,
  commands: "/api/commands",
  workers: "/api/workers",
  audit: "/api/audit",
};

const STATUS_STYLE = {
  pending: ["idle", "pending"],
  running: ["info", "running"],
  success: ["ok", "success"],
  aborted: ["warn", "aborted"],
  failed: ["bad", "failed"],
  待拆解: ["idle"],
  待调度下发: ["idle"],
  执行中: ["info"],
  待验收: ["warn"],
  验收通过: ["ok"],
  验收驳回: ["bad"],
  执行失败: ["bad"],
};

const state = {
  token: "", page: 1, size: 20, pages: 1, total: 0, status: "",
  q: "", sort: "start_time_desc", trace: null, autoMs: 0,
  // Batch15：执行方视图筛选（默认全看；纯客户端过滤，不打多余请求）
  workerFilter: "", workerOnlineOnly: false, workerInstance: "",
  // Batch18：运维审计筛选
  auditKeyword: "",
  // Batch22：审计分页（服务端 offset 翻页）+ 操作者
  auditPage: 1, auditPages: 1, auditTotal: 0,
  operator: "",
};

const API_TIMEOUT_MS = 30000;
// Batch12：执行方在线视图的心跳阈值。与平台 heartbeat_timeout_seconds 默认值保持一致口径。
const WORKER_ONLINE_MS = 120000;
const WORKER_VIEW_SIZE = 100;
const RESULT_MAX_CHARS = 20000;
const AUTO_KEY = "mwp_auto_refresh_ms";
const AUDIT_PAGE = 20;            // 审计每页条数（Batch22）
const OPERATOR_KEY = "mwp_operator";
// Batch26：卡住判定与心跳口径
const STALL_MS = 10 * 60 * 1000;            // 未结束且超过 10 分钟 → 判停滞
const STALL_STATUSES = ["pending", "running"];
const HEARTBEAT_WARN_MS = WORKER_ONLINE_MS;  // 与平台心跳超时同口径
const SNAP_TZ = "本地时间";

function el(id) { return document.getElementById(id); }

function styleFor(value) {
  const hit = STATUS_STYLE[value];
  if (!hit) return "idle";
  return hit[0];
}

function tag(value) {
  const span = document.createElement("span");
  span.className = "tag " + styleFor(value);
  span.textContent = value;
  return span;
}

function text(value, cls) {
  const span = document.createElement("span");
  span.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  if (cls) span.className = cls;
  return span;
}

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (state.token) headers["X-API-Token"] = state.token;
  // Batch22：把操作者名字带上去，平台只记不自证（没有身份体系）
  // 头只能装 latin-1，中文名字先 percent-encode（服务端 unquote 还原）
  if (state.operator) headers["X-Operator"] = encodeURIComponent(state.operator);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  try {
    const resp = await fetch(path, Object.assign({}, options, { headers, signal: controller.signal }));
    let body = null;
    const type = resp.headers.get("content-type") || "";
    if (type.includes("application/json")) {
      try { body = await resp.json(); } catch (e) { body = null; }
    } else {
      body = await resp.text();
    }
    return { ok: resp.ok, status: resp.status, body };
  } finally {
    clearTimeout(timer);
  }
}

function showResult(nodeId, ok, payload) {
  const node = el(nodeId);
  node.className = "result " + (ok ? "ok" : "err");
  let out = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
  if (out.length > RESULT_MAX_CHARS) {
    out = out.slice(0, RESULT_MAX_CHARS) + `\n…已截断（原始 ${out.length} 字符）`;
  }
  node.textContent = out;
}

function clearResult(nodeId) {
  const node = el(nodeId);
  node.className = "";
  node.textContent = "";
}

async function checkHealth() {
  const badge = el("healthBadge");
  try {
    const r = await api(ROUTES.health);
    if (r.ok) {
      badge.textContent = "健康检查 200";
      badge.style.color = "#1f9d55";
    } else {
      badge.textContent = "健康检查 " + r.status;
      badge.style.color = "#d1382a";
    }
  } catch (e) {
    badge.textContent = "无法连接后端";
    badge.style.color = "#d1382a";
  }
}

/* ---------- 差量渲染 ----------
   列表按 trace_id、任务表按 task_id 复用 DOM 行，只改变化的单元格。
   自动刷新时不再整表重绘，滚动位置、选中行、鼠标悬停都不会跳。 */
const listRows = new Map();
const taskRows = new Map();

function setCellText(cell, value, cls) {
  cell.innerHTML = "";
  cell.appendChild(text(value, cls));
}

function renderPipelines(payload) {
  const wrap = el("pipeTableWrap");
  const meta = payload.meta || {};
  state.pages = meta.pages || 1;
  state.total = meta.total || 0;
  el("pipeCount").textContent = `共 ${state.total} 条`;
  el("pipePageInfo").textContent = `第 ${meta.page || 1} / ${state.pages} 页`;
  el("pipePrev").disabled = (meta.page || 1) <= 1;
  el("pipeNext").disabled = (meta.page || 1) >= state.pages;

  const items = payload.items || [];
  lastPipes = { items, meta };
  if (!items.length) {
    wrap.innerHTML = '<div class="empty">没有符合条件的流水线</div>';
    listRows.clear();
    if (el("pipeStall")) el("pipeStall").textContent = "";
    state.pipeMaxMs = 0;
    return;
  }

  // Batch26：本页最长耗时做时间轴归一，停滞条数进抬头
  const now = Date.now();
  let maxMs = 0;
  items.forEach((p) => {
    const span = pipeSpan(p, now);
    if (Number.isFinite(span.ms) && span.ms > maxMs) maxMs = span.ms;
  });
  state.pipeMaxMs = maxMs;
  const stallCount = items.filter((p) => { const s = stallInfo(p, now); return s && s.label === "停滞"; }).length;
  const dwellCount = items.filter((p) => { const s = stallInfo(p, now); return s && s.label === "滞留"; }).length;
  if (el("pipeStall")) {
    el("pipeStall").textContent = (stallCount || dwellCount)
      ? `（停滞 ${stallCount} · 滞留 ${dwellCount}）` : "";
    el("pipeStall").className = (stallCount || dwellCount) ? "count stall-count" : "count";
  }

  let table = wrap.querySelector("table");
  if (!table) {
    wrap.innerHTML = "";
    table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>trace_id</th><th>状态</th><th>进度</th><th>暂停</th><th>起始时间</th><th>原始需求</th></tr></thead>";
    table.appendChild(document.createElement("tbody"));
    wrap.appendChild(table);
  }
  const tbody = table.querySelector("tbody");
  const prevScroll = wrap.scrollTop;

  const seen = new Set();
  items.forEach((p, idx) => {
    seen.add(p.trace_id);
    let rec = listRows.get(p.trace_id);
    if (!rec) {
      rec = buildPipelineRow(p);
      listRows.set(p.trace_id, rec);
    }
    updatePipelineRow(rec, p);
    const atPos = tbody.children[idx];
    if (atPos !== rec.row) tbody.insertBefore(rec.row, atPos || null);
  });
  Array.from(tbody.children).forEach((row) => {
    if (!seen.has(row.dataset.trace)) row.remove();
  });
  for (const key of Array.from(listRows.keys())) {
    if (!seen.has(key)) listRows.delete(key);
  }
  wrap.scrollTop = prevScroll;
}

function buildPipelineRow(p) {
  const row = document.createElement("tr");
  row.className = "clickable";
  row.dataset.trace = p.trace_id;
  const cells = {};
  ["trace", "status", "rail", "pause", "time", "demand"].forEach((k) => {
    cells[k] = document.createElement("td");
    row.appendChild(cells[k]);
  });
  cells.trace.appendChild(text(p.trace_id, "mono"));
  row.addEventListener("click", () => loadDetail(p.trace_id));
  return { row, cells, last: { trace: p.trace_id } };
}

function updatePipelineRow(rec, p) {
  const now = Date.now();
  const stall = stallInfo(p, now);
  // 状态 + 超时章 + 时间轴一起判签名：变了才重画，没变不折腾 DOM
  const sig = [p.overall_status, p.start_time, p.end_time, stall ? stall.label : "",
    stall ? Math.floor(stall.ms / 60000) : 0].join("|");
  if (rec.last.cellSig !== sig) {
    rec.last.cellSig = sig;
    rec.cells.status.innerHTML = "";
    rec.cells.status.appendChild(tag(p.overall_status));
    if (stall) {
      const stamp = document.createElement("span");
      stamp.className = "tag warn stall-tag";
      stamp.textContent = stall.label + " " + fmtDur(stall.ms);
      stamp.title = stall.label === "停滞"
        ? `该在跑却已 ${fmtDur(stall.ms)} 没动静（阈值 ${Math.round(STALL_MS / 60000)} 分钟）`
        : `建单后 ${fmtDur(stall.ms)} 一直没被领走（阈值 ${Math.round(STALL_MS * 3 / 60000)} 分钟）`;
      rec.cells.status.appendChild(stamp);
    }
    rec.cells.rail.innerHTML = "";
    rec.cells.rail.appendChild(buildRail(p, now, state.pipeMaxMs || 0));
  }
  rec.row.classList.toggle("stalled", !!stall);
  const pause = p.pause_flag ? "已暂停" : "正常";
  if (rec.last.pause !== pause) {
    rec.last.pause = pause;
    setCellText(rec.cells.pause, pause);
  }
  if (rec.last.time !== p.start_time) {
    rec.last.time = p.start_time;
    setCellText(rec.cells.time, fmtStamp(p.start_time));
    rec.cells.time.title = p.start_time || "";
  }
  if (rec.last.demand !== p.original_demand) {
    rec.last.demand = p.original_demand;
    setCellText(rec.cells.demand, p.original_demand);
  }
  const selected = p.trace_id === state.trace;
  if (rec.last.selected !== selected) {
    rec.last.selected = selected;
    rec.row.classList.toggle("selected", selected);
  }
}

async function loadPipelines() {
  const query = new URLSearchParams();
  if (state.status) query.set("status", state.status);
  if (state.q) query.set("q", state.q);
  query.set("sort", state.sort);
  query.set("page", String(state.page));
  query.set("size", String(state.size));
  try {
    const r = await api(ROUTES.pipelines + "?" + query.toString());
    if (!r.ok) {
      el("pipeTableWrap").innerHTML = `<div class="empty">加载失败：${r.status} ${JSON.stringify(r.body)}</div>`;
      listRows.clear();
      return;
    }
    renderPipelines(r.body);
  } catch (e) {
    el("pipeTableWrap").innerHTML = `<div class="empty">请求异常：${e}</div>`;
    listRows.clear();
  }
}

function renderDetail(p) {
  state.trace = p.trace_id;
  el("detailPanel").hidden = false;
  el("detailTrace").textContent = p.trace_id;
  el("exportJson").href = ROUTES.exportJson(p.trace_id);
  el("exportCsv").href = ROUTES.exportCsv(p.trace_id);
  el("exportJson").setAttribute("download", "");
  el("exportCsv").setAttribute("download", "");

  const kv = el("detailKv");
  kv.innerHTML = "";
  const rows = [
    ["整体状态", null],
    ["暂停标记", p.pause_flag ? "已暂停（派发被拦截）" : "未暂停"],
    ["原始需求", p.original_demand],
    ["归档路径", p.archive_path],
  ];
  rows.forEach(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    if (k === "整体状态") dd.appendChild(tag(p.overall_status));
    else dd.appendChild(text(v));
    kv.appendChild(dt);
    kv.appendChild(dd);
  });

  const wrap = el("taskTableWrap");
  const tasks = p.tasks || [];
  el("taskCount").textContent = `共 ${tasks.length} 个`;
  if (!tasks.length) {
    wrap.innerHTML = '<div class="empty">该流水线还没有任务</div>';
    taskRows.clear();
    return;
  }

  let table = wrap.querySelector("table");
  if (!table) {
    wrap.innerHTML = "";
    table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>task_id</th><th>状态</th><th>执行方</th><th>派发时间</th><th>重试</th><th>依赖</th><th>原因</th></tr></thead>";
    table.appendChild(document.createElement("tbody"));
    wrap.appendChild(table);
  }
  const tbody = table.querySelector("tbody");
  const prevScroll = wrap.scrollTop;

  const seen = new Set();
  tasks.forEach((t, idx) => {
    seen.add(t.task_id);
    let rec = taskRows.get(t.task_id);
    if (!rec) {
      rec = buildTaskRow(t);
      taskRows.set(t.task_id, rec);
    }
    updateTaskRow(rec, t);
    const atPos = tbody.children[idx];
    if (atPos !== rec.row) tbody.insertBefore(rec.row, atPos || null);
  });
  Array.from(tbody.children).forEach((row) => {
    if (!seen.has(row.dataset.task)) row.remove();
  });
  for (const key of Array.from(taskRows.keys())) {
    if (!seen.has(key)) taskRows.delete(key);
  }
  wrap.scrollTop = prevScroll;
}

function buildTaskRow(t) {
  const row = document.createElement("tr");
  row.dataset.task = t.task_id;
  const cells = {};
  ["id", "status", "worker", "dispatch", "retry", "deps", "reason"].forEach((k) => {
    cells[k] = document.createElement("td");
    row.appendChild(cells[k]);
  });
  cells.id.appendChild(text(t.task_id, "mono"));
  return { row, cells, last: { id: t.task_id } };
}

function updateTaskRow(rec, t) {
  if (rec.last.status !== t.status) {
    rec.last.status = t.status;
    rec.cells.status.innerHTML = "";
    rec.cells.status.appendChild(tag(t.status));
  }
  const worker = t.target_worker;
  if (rec.last.worker !== worker) { rec.last.worker = worker; setCellText(rec.cells.worker, worker); }
  if (rec.last.dispatch !== t.dispatch_time) {
    rec.last.dispatch = t.dispatch_time;
    setCellText(rec.cells.dispatch, fmtStamp(t.dispatch_time));
    rec.cells.dispatch.title = t.dispatch_time || "";
  }
  const retry = `${t.remaining_retry}/${t.max_retry}`;
  if (rec.last.retry !== retry) { rec.last.retry = retry; setCellText(rec.cells.retry, retry); }
  const deps = (t.depends_on || []).join(", ") || "—";
  if (rec.last.deps !== deps) { rec.last.deps = deps; setCellText(rec.cells.deps, deps); }
  const reason = t.abort_reason || t.reject_reason || "—";
  if (rec.last.reason !== reason) { rec.last.reason = reason; setCellText(rec.cells.reason, reason); }
}

/* ---------- 执行方在线视图（Batch12）
   不新增接口：抓最近 100 条任务，按 target_worker 聚合。
   在线判断靠 last_heartbeat_time，超过阈值算静默。 */
function aggregateWorkers(items) {
  const map = new Map();
  (items || []).forEach((t) => {
    const key = t.target_worker || "（未标注）";
    let rec = map.get(key);
    if (!rec) {
      rec = { worker: key, total: 0, running: 0, waiting: 0, failed: 0, lastHb: null };
      map.set(key, rec);
    }
    rec.total += 1;
    if (t.status === "执行中") rec.running += 1;
    else if (t.status === "待验收") rec.waiting += 1;
    else if (t.status === "执行失败" || t.status === "验收驳回") rec.failed += 1;
    if (t.last_heartbeat_time) {
      const ms = Date.parse(t.last_heartbeat_time);
      if (!Number.isNaN(ms) && (rec.lastHb === null || ms > rec.lastHb)) rec.lastHb = ms;
    }
  });
  return Array.from(map.values()).sort((a, b) => b.running - a.running || b.total - a.total);
}

function workerState(rec) {
  if (rec.lastHb === null) return { label: "未知", cls: "idle" };
  const age = Date.now() - rec.lastHb;
  if (age <= WORKER_ONLINE_MS) return { label: "在线", cls: "ok" };
  return { label: "静默", cls: "warn" };
}

function renderWorkers(items) {
  const wrap = el("workerTableWrap");
  if (!wrap) return;
  const rows = aggregateWorkers(items);
  el("workerCount").textContent = rows.length ? `（${rows.length}）` : "";
  if (!rows.length) {
    wrap.innerHTML = '<div class="empty">还没有任何任务，暂无执行方</div>';
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["执行方", "任务总数", "在跑", "待验收", "失败/驳回", "最近心跳", "状态"].forEach((h) => {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  });
  table.appendChild(head);
  rows.forEach((rec) => {
    const tr = document.createElement("tr");
    tr.appendChild(td(rec.worker, "mono"));
    tr.appendChild(td(String(rec.total)));
    tr.appendChild(td(String(rec.running)));
    tr.appendChild(td(String(rec.waiting)));
    tr.appendChild(td(String(rec.failed)));
    tr.appendChild(td(rec.lastHb === null ? "—" : new Date(rec.lastHb).toLocaleTimeString()));
    const st = workerState(rec);
    const cell = document.createElement("td");
    const span = document.createElement("span");
    span.className = "tag " + st.cls;
    span.textContent = st.label;
    cell.appendChild(span);
    tr.appendChild(cell);
    table.appendChild(tr);
  });
  wrap.innerHTML = "";
  wrap.appendChild(table);
}

function td(content, cls) {
  const cell = document.createElement("td");
  cell.appendChild(text(content, cls));
  return cell;
}

/* 时间显示：表里只给「月-日 时:分」，完整值放 tooltip。
   台账要看的是顺序和间隔，年份与秒只在溯源时才需要。 */
function fmtStamp(v, withSec) {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  const p2 = (n) => String(n).padStart(2, "0");
  const base = `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
  return withSec ? `${base}:${p2(d.getSeconds())}` : base;
}

/* ---------- Batch26：停滞判定 / 心跳 / 单行时间轴（都只用已有字段，不扩接口）---------- */
function fmtAgo(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s 前`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m 前`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h${String(m % 60).padStart(2, "0")}m 前`;
  return `${Math.floor(h / 24)}d 前`;
}

function fmtDur(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d${String(h % 24).padStart(2, "0")}h`;
}

// 起止都来自 aspect：start_time 建单、end_time 收尾；未收尾就按"到现在"算已耗
function pipeSpan(p, now) {
  const start = p.start_time ? new Date(p.start_time).getTime() : NaN;
  const end = p.end_time ? new Date(p.end_time).getTime() : now;
  if (!Number.isFinite(start)) return { ms: NaN, open: !p.end_time };
  return { ms: Math.max(0, end - start), open: !p.end_time };
}

function isStalled(p, now) {
  return stallInfo(p, now) !== null;
}

// running 超 10 分钟=停滞（该在跑却不动）；pending 超 30 分钟=滞留（一直没人领）
// 两者分开命名，免得"排着队"被误读成"卡死了"
function stallInfo(p, now) {
  if (!p.start_time || p.end_time) return null;
  const span = pipeSpan(p, now);
  if (!Number.isFinite(span.ms)) return null;
  if (p.overall_status === "running" && span.ms >= STALL_MS) return { label: "停滞", ms: span.ms };
  if (p.overall_status === "pending" && span.ms >= STALL_MS * 3) return { label: "滞留", ms: span.ms };
  return null;
}

// 单行时间轴：条长按本页最长耗时归一到 100%，所以同页能横向比"谁拖得久"
function buildRail(p, now, maxMs) {
  const holder = document.createElement("div");
  holder.className = "rail-box";
  const span = pipeSpan(p, now);
  let pct = 8;
  if (Number.isFinite(span.ms) && maxMs > 0) pct = Math.max(8, Math.min(100, (span.ms / maxMs) * 100));
  const rail = document.createElement("span");
  rail.className = "rail";
  const tone = !p.end_time ? "open" : (p.overall_status === "success" ? "done" : "halt");
  const fill = document.createElement("i");
  fill.className = "rail-fill " + tone;
  fill.style.width = pct + "%";
  rail.appendChild(fill);
  const dot = document.createElement("b");
  dot.className = "rail-dot " + tone;
  dot.style.left = pct + "%";
  rail.appendChild(dot);
  rail.title = `建单 ${fmtStamp(p.start_time, true)}` + (p.end_time
    ? ` → 收尾 ${fmtStamp(p.end_time, true)}，耗时 ${fmtDur(span.ms)}`
    : ` → 至今 ${fmtDur(span.ms)}（未收尾）`);
  holder.appendChild(rail);
  const lab = document.createElement("span");
  lab.className = "rail-lab num";
  lab.textContent = fmtDur(span.ms);
  holder.appendChild(lab);
  return holder;
}

// 心跳：显示"多久之前"，超时直接红字（判断口径与后端 online 一致：120s）
function heartbeatNode(w, now) {
  const last = w.last_seen ? new Date(w.last_seen).getTime() : NaN;
  if (!Number.isFinite(last)) return text("—", "muted");
  const ageMs = Math.max(0, now - last);
  const node = document.createElement("span");
  node.className = "hb" + (ageMs > HEARTBEAT_WARN_MS ? " late" : "");
  node.textContent = fmtAgo(ageMs);
  node.title = `最近上报 ${fmtStamp(w.last_seen, true)}（超过 ${Math.round(HEARTBEAT_WARN_MS / 1000)}s 判失联）`;
  return node;
}

// 并发刻度：一个点 = 一个正在跑的槽位，空圈 = 还没用上的槽位
function concurNode(w) {
  const total = Math.max(0, Number(w.concurrency) || 0);
  const used = Math.max(0, Math.min(total, Number(w.in_flight) || 0));
  const box = document.createElement("span");
  box.className = "dots";
  const cap = Math.min(total, 8);
  for (let i = 0; i < cap; i += 1) {
    const d = document.createElement("i");
    d.className = i < used ? "on" : "off";
    box.appendChild(d);
  }
  if (total > cap) {
    const more = document.createElement("span");
    more.className = "muted";
    more.textContent = `+${total - cap}`;
    box.appendChild(more);
  }
  box.title = `在跑 ${used} / 并发上限 ${total}`;
  const lab = document.createElement("span");
  lab.className = "num dots-lab";
  lab.textContent = `${used}/${total}`;
  const holder = document.createElement("span");
  holder.className = "dots-box";
  holder.appendChild(box);
  holder.appendChild(lab);
  return holder;
}

function workerRegistryState(w) {
  if (w.status === "stopped") return { label: "停机", cls: "idle" };
  if (w.status === "stopping") return { label: "收尾中", cls: "warn" };
  if (w.online) return { label: "在线", cls: "ok" };
  return { label: "静默", cls: "bad" };
}

function syncWorkerFilterOptions(workers) {
  const sel = el("workerFilter");
  if (!sel) return;
  const ids = Array.from(new Set(workers.map((w) => w.worker_id))).sort();
  const current = state.workerFilter;
  sel.innerHTML = '<option value="">全部执行方</option>';
  ids.forEach((id) => {
    const op = document.createElement("option");
    op.value = id;
    op.textContent = id;
    sel.appendChild(op);
  });
  sel.value = ids.indexOf(current) >= 0 ? current : "";
  if (sel.value !== current) state.workerFilter = sel.value;
}

function filteredWorkers(workers) {
  const kw = state.workerInstance.trim().toLowerCase();
  return workers.filter((w) => {
    if (state.workerFilter && w.worker_id !== state.workerFilter) return false;
    if (state.workerOnlineOnly && !w.online) return false;
    if (kw && !String(w.instance_id || "default").toLowerCase().includes(kw)) return false;
    return true;
  });
}

async function setWorkerLimit(w, raw) {
  const limit = String(raw || "").trim();
  const body = {
    command: "master:worker-limit",
    worker_id: w.worker_id,
    instance_id: w.instance_id || "default",
    limit: limit === "" ? 0 : Number(limit),
    reason: "看板手动下发",
  };
  if (!Number.isFinite(body.limit) || body.limit < 0 || body.limit > 64) {
    showResult("workerCmdResult", false, "期望并发必须是 0~64 的整数");
    return;
  }
  const r = await api(ROUTES.commands, { method: "POST", body: JSON.stringify(body) });
  if (r.ok) {
    const d = r.body.desired_limit;
    showResult("workerCmdResult", true, d === 0
      ? `已撤销 ${body.worker_id}/${body.instance_id} 的配额，Worker 下次上报回到自己的配置`
      : `已下发 ${body.worker_id}/${body.instance_id} 期望并发=${d}；Worker 下次上报才生效，这里会显示"未生效"直到它按新值跑`);
  } else {
    showResult("workerCmdResult", false, `下发失败：${r.status} ${JSON.stringify(r.body)}`);
  }
  loadWorkers();
  loadAudit({ reset: true });
}

function renderRegistry(workers) {
  const wrap = el("workerTableWrap");
  if (!wrap) return;
  syncWorkerFilterOptions(workers);
  const shown = filteredWorkers(workers);
  el("workerCount").textContent = workers.length
    ? `（在线 ${workers.filter((w) => w.online).length} / 共 ${workers.length} 个实例${shown.length !== workers.length ? `，当前显示 ${shown.length}` : ""}）`
    : "";
  if (!workers.length) {
    wrap.innerHTML = '<div class="empty">还没有 Worker 上报过（启动 Worker 并接入 /api/workers 即可）</div>';
    return;
  }
  if (!shown.length) {
    wrap.innerHTML = '<div class="empty">当前筛选条件下没有实例（清空筛选可看全部）</div>';
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["执行方", "实例", "状态", "并发", "期望上限", "配额下发", "累计完成", "最近上报", "备注"].forEach((h) => {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  });
  table.appendChild(head);
  shown.forEach((w) => {
    const tr = document.createElement("tr");
    tr.appendChild(td(w.worker_id, "mono"));
    tr.appendChild(td(w.instance_id || "default", "mono"));
    const st = workerRegistryState(w);
    const stCell = document.createElement("td");
    const span = document.createElement("span");
    span.className = "tag " + st.cls;
    span.textContent = st.label;
    stCell.appendChild(span);
    tr.appendChild(stCell);
    // Batch26：并发改成刻度（点=在跑的槽位，空圈=剩余槽位），一个格子同时给点和数字
    const concCell = document.createElement("td");
    concCell.appendChild(concurNode(w));
    tr.appendChild(concCell);
    // Batch22：期望值下发后 Worker 还没按它跑，就地标出来，不用等审计翻页
    const wantCell = td(w.desired_limit ? String(w.desired_limit) : "—", w.desired_limit ? "num" : "muted");
    if (w.desired_limit && w.concurrency !== w.desired_limit) {
      const warn = document.createElement("span");
      warn.className = "tag warn";
      warn.textContent = "未生效";
      warn.title = `期望 ${w.desired_limit}，Worker 自报 ${w.concurrency}`;
      warn.style.marginLeft = "6px";
      wantCell.appendChild(warn);
    }
    tr.appendChild(wantCell);
    const quotaCell = document.createElement("td");
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.max = "64";
    input.value = w.desired_limit ? String(w.desired_limit) : "";
    input.placeholder = "0~64";
    input.style.width = "68px";
    const btn = document.createElement("button");
    btn.textContent = "下发";
    btn.className = "ghost";
    btn.addEventListener("click", () => setWorkerLimit(w, input.value));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); setWorkerLimit(w, input.value); }
    });
    quotaCell.appendChild(input);
    quotaCell.appendChild(btn);
    tr.appendChild(quotaCell);
    tr.appendChild(td(String(w.handled), "num"));
    // Batch26：最近上报给"N 前"，超过心跳超时直接红字（比一串时钟时间好读）
    const hbCell = document.createElement("td");
    hbCell.appendChild(heartbeatNode(w, Date.now()));
    tr.appendChild(hbCell);
    tr.appendChild(td(w.note || "—", "muted"));
    table.appendChild(tr);
  });
  wrap.innerHTML = "";
  wrap.appendChild(table);
  const last = wrap.getAttribute("data-last-cmd");
  if (last) {
    const note = document.createElement("div");
    note.className = "empty";
    note.textContent = last;
    wrap.appendChild(note);
  }
}

let lastWorkers = [];
let lastPipes = { items: [], meta: {} };   // Batch26：快照导出要用当前页数据

async function loadWorkers() {
  // Batch13：优先读执行方注册表；平台老版本（没有该接口）时退回按任务聚合。
  try {
    const reg = await api(ROUTES.workers);
    if (reg.ok && Array.isArray(reg.body)) {
      lastWorkers = reg.body;
      renderRegistry(reg.body);
      return;
    }
  } catch (e) {
    /* 落到下面的兜底 */
  }
  try {
    const r = await api(`${ROUTES.tasks}?page=1&size=${WORKER_VIEW_SIZE}`);
    if (!r.ok) {
      const wrap = el("workerTableWrap");
      if (wrap) wrap.innerHTML = `<div class="empty">执行方视图读取失败：${r.status}</div>`;
      return;
    }
    renderWorkers(r.body.items || []);
  } catch (e) {
    const wrap = el("workerTableWrap");
    if (wrap) wrap.innerHTML = `<div class="empty">执行方视图请求异常：${e}</div>`;
  }
}


/* ---------- 运维审计（Batch18） ---------- */
let lastAudit = null;

function auditEffective(v) {
  if (v === true) return { label: "已生效", cls: "ok" };
  if (v === false) return { label: "未生效", cls: "bad" };
  return { label: "—", cls: "idle" };
}

function renderAudit(view, opts = {}) {
  const wrap = el("auditTableWrap");
  if (!wrap) return;
  lastAudit = view;
  const items = (view && view.items) || [];
  const kw = state.auditKeyword.trim().toLowerCase();
  const shown = items.filter((it) => {
    if (!kw) return true;
    const target = `${it.worker_id}/${it.instance_id}`.toLowerCase();
    return target.includes(kw) || (it.reason || "").toLowerCase().includes(kw) || it.command.includes(kw);
  });
  const meta = (view && view.meta) || { total: 0 };
  el("auditCount").textContent = items.length
    ? `（本页 ${shown.length} 条）` : "";
  renderAuditPager(view, !!opts.filtered);
  if (!items.length) {
    wrap.innerHTML = '<div class="empty">还没有运维动作记录</div>';
    return;
  }
  if (!shown.length) {
    wrap.innerHTML = '<div class="empty">没有匹配的记录</div>';
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["时间", "操作者", "动作", "目标", "旧→新", "是否生效", "原因"].forEach((h) => {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  });
  table.appendChild(head);
  shown.forEach((it) => {
    const tr = document.createElement("tr");
    const timeCell = td(fmtStamp(it.created_at, true));
    timeCell.title = it.created_at || "";
    tr.appendChild(timeCell);
    tr.appendChild(td(it.operator || "—", it.operator ? "mono" : "muted"));
    tr.appendChild(td(it.command.replace("master:", ""), "mono"));
    tr.appendChild(td(`${it.worker_id}/${it.instance_id}`, "mono"));
    const isQuota = it.command === "master:worker-limit";
    tr.appendChild(td(isQuota ? `${it.prev_limit}→${it.desired_limit}` : "—", isQuota ? "num" : "muted"));
    const eff = auditEffective(isQuota ? it.effective : null);
    const cell = document.createElement("td");
    const span = document.createElement("span");
    span.className = "tag " + eff.cls + (eff.cls === "ok" ? " is-stamp" : "");
    span.textContent = eff.label;
    cell.appendChild(span);
    tr.appendChild(cell);
    tr.appendChild(td(it.reason || "—", "muted"));
    table.appendChild(tr);
  });
  const cur = (view && view.current) || {};
  const keys = Object.keys(cur);
  wrap.innerHTML = "";
  wrap.appendChild(table);
  if (keys.length) {
    const note = document.createElement("div");
    note.className = "note";
    note.textContent = "注册表当前：" + keys.map((k) =>
      `${k} 期望=${cur[k].desired_limit || "—"} 并发=${cur[k].concurrency} ${cur[k].online ? "在线" : "静默"}`
    ).join("；");
    wrap.appendChild(note);
  }
}

async function loadAudit(opts = {}) {
  const wrap = el("auditTableWrap");
  if (!wrap) return;
  if (opts.reset) state.auditPage = 1;
  const kw = state.auditKeyword.trim();
  // 有关键字时退化成"扫最近 200 条再当页过滤"——服务端只支持精确过滤，没法做子串
  const limit = kw ? 200 : AUDIT_PAGE;
  const offset = kw ? 0 : (state.auditPage - 1) * AUDIT_PAGE;
  try {
    const r = await api(`${ROUTES.audit}?limit=${limit}&offset=${offset}`);
    if (!r.ok) {
      wrap.innerHTML = `<div class="empty">审计读取失败：${r.status} ${JSON.stringify(r.body)}</div>`;
      return;
    }
    renderAudit(r.body, { filtered: !!kw });
  } catch (e) {
    wrap.innerHTML = `<div class="empty">审计请求异常：${e}</div>`;
  }
}

function renderAuditPager(view, filtered) {
  const info = el("auditPageInfo");
  const total = el("auditTotal");
  const prev = el("auditPrev");
  const next = el("auditNext");
  if (!info || !prev || !next) return;
  const meta = (view && view.meta) || { total: 0 };
  state.auditTotal = meta.total || 0;
  state.auditPages = filtered ? 1 : Math.max(1, Math.ceil(state.auditTotal / AUDIT_PAGE));
  info.textContent = filtered ? `搜索：最近 ${meta.shown} 条里命中 ${(view.items || []).length} 条`
                             : `第 ${state.auditPage} / ${state.auditPages} 页`;
  if (total) {
    total.textContent = `共 ${state.auditTotal} 条` + (filtered ? "（搜索时只看最近 200 条）" : "");
  }
  prev.disabled = filtered || state.auditPage <= 1;
  next.disabled = filtered || state.auditPage >= state.auditPages;
}

async function loadDetail(traceId, opts = {}) {
  const silent = !!opts.silent;
  if (!silent) clearResult("cmdResult");
  try {
    const r = await api(ROUTES.pipelineDetail(traceId));
    if (!r.ok) {
      el("detailPanel").hidden = false;
      el("detailTrace").textContent = traceId;
      el("detailKv").innerHTML = "";
      el("taskTableWrap").innerHTML = `<div class="empty">读取失败：${r.status} ${JSON.stringify(r.body)}</div>`;
      return;
    }
    renderDetail(r.body);
    if (!silent) loadPipelines();
  } catch (e) {
    el("taskTableWrap").innerHTML = `<div class="empty">请求异常：${e}</div>`;
  }
}

/* ---------- 自动刷新 ----------
   用 setTimeout 逐个排期而不是 setInterval：请求慢时不会堆积。
   页面隐藏（切到别的标签）时直接跳过，不白白打后端。 */
let autoTimer = null;
let refreshing = false;

function markRefreshed() {
  const node = el("lastRefresh");
  if (!node) return;
  node.textContent = "上次刷新 " + new Date().toLocaleTimeString();
}

async function refreshOnce() {
  if (document.hidden || refreshing) return;
  refreshing = true;
  try {
    await checkHealth();
    await loadWorkers();
    await loadAudit();
    await loadPipelines();
    if (state.trace) await loadDetail(state.trace, { silent: true });
    markRefreshed();
  } finally {
    refreshing = false;
  }
}

function scheduleAuto() {
  if (autoTimer) {
    clearTimeout(autoTimer);
    autoTimer = null;
  }
  if (!state.autoMs) return;
  autoTimer = setTimeout(async () => {
    await refreshOnce();
    scheduleAuto();
  }, state.autoMs);
}

function setAutoRefresh(ms) {
  state.autoMs = Math.max(0, Number(ms) || 0);
  localStorage.setItem(AUTO_KEY, String(state.autoMs));
  const sel = el("autoRefresh");
  if (sel) sel.value = String(state.autoMs);
  const node = el("lastRefresh");
  if (node) node.textContent = state.autoMs ? "等待首次自动刷新…" : "自动刷新已关闭";
  scheduleAuto();
}

async function sendCommand(command) {
  if (!state.trace) {
    showResult("cmdResult", false, "先选一条流水线");
    return;
  }
  try {
    const r = await api(ROUTES.commands, {
      method: "POST",
      body: JSON.stringify({ command, trace_id: state.trace, reason: "从看板下发" }),
    });
    if (r.ok) showResult("cmdResult", true, r.body);
    else showResult("cmdResult", false, `${r.status} ${JSON.stringify(r.body)}`);
    loadDetail(state.trace);
  } catch (e) {
    showResult("cmdResult", false, String(e));
  }
}

const TEMPLATES = {
  task_dispatch: (ts) => ({
    trace_id: "demo-trace-001", task_id: "demo-task-001", msg_type: "task_dispatch",
    sender: "master", timestamp: ts, version: "v1.0",
    payload: { target_worker: "workbuddy", task_goal: "示例：平台自检任务", acceptance_criteria: ["产出可读", "无删除操作"] },
  }),
  task_heartbeat: (ts) => ({
    trace_id: "demo-trace-001", task_id: "demo-task-001", msg_type: "task_heartbeat",
    sender: "workbuddy", timestamp: ts, version: "v1.0", payload: { progress: "50%" },
  }),
  task_return: (ts) => ({
    trace_id: "demo-trace-001", task_id: "demo-task-001", msg_type: "task_return",
    sender: "workbuddy", timestamp: ts, version: "v1.0", payload: { result: "示例产出" },
  }),
  task_reject: (ts) => ({
    trace_id: "demo-trace-001", task_id: "demo-task-001", msg_type: "task_reject",
    sender: "workbuddy", timestamp: ts, version: "v1.0", payload: { reject_reason: "示例驳回原因" },
  }),
  task_abort: (ts) => ({
    trace_id: "demo-trace-001", task_id: "demo-task-001", msg_type: "task_abort",
    sender: "master", timestamp: ts, version: "v1.0", payload: { abort_reason: "示例中止原因" },
  }),
  human_confirm_req: (ts) => ({
    trace_id: "demo-trace-001", task_id: "demo-task-001", msg_type: "human_confirm_req",
    sender: "master", timestamp: ts, version: "v1.0", payload: { question: "是否继续？" },
  }),
  human_confirm_resp: (ts) => ({
    trace_id: "demo-trace-001", task_id: "demo-task-001", msg_type: "human_confirm_resp",
    sender: "user", timestamp: ts, version: "v1.0", payload: { answer: "同意" },
  }),
  task_summary: (ts) => ({
    trace_id: "demo-trace-001", task_id: "demo-task-001", msg_type: "task_summary",
    sender: "master", timestamp: ts, version: "v1.0", payload: { summary: "示例总结" },
  }),
};

function nowIso() {
  const d = new Date();
  return d.toISOString().replace("Z", "+00:00");
}

async function sendMessage() {
  const raw = el("msgBody").value.trim();
  if (!raw) {
    showResult("msgResult", false, "报文为空");
    return;
  }
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (e) {
    showResult("msgResult", false, "JSON 解析失败：" + e.message);
    return;
  }
  try {
    const r = await api(ROUTES.messages, { method: "POST", body: JSON.stringify(payload) });
    if (r.ok) {
      showResult("msgResult", true, `${r.status} 已受理\n` + JSON.stringify(r.body, null, 2));
      loadPipelines();
    } else {
      showResult("msgResult", false, `${r.status} 被拒\n` + JSON.stringify(r.body, null, 2));
    }
  } catch (e) {
    showResult("msgResult", false, String(e));
  }
}

async function queryArchive() {
  const trace = el("archiveTrace").value.trim();
  if (!trace) return;
  try {
    const r = await api(ROUTES.archive(trace));
    const node = el("archiveResult");
    if (r.ok) {
      node.className = "";
      node.innerHTML = "";
      const dl = document.createElement("dl");
      dl.className = "kv";
      [["索引行数", r.body.index_rows], ["JSONL 路径", r.body.jsonl_path]].forEach(([k, v]) => {
        const dt = document.createElement("dt"); dt.textContent = k;
        const dd = document.createElement("dd"); dd.appendChild(text(v, "mono"));
        dl.appendChild(dt); dl.appendChild(dd);
      });
      node.appendChild(dl);
    } else {
      showResult("archiveResult", false, `${r.status} ${JSON.stringify(r.body)}`);
    }
  } catch (e) {
    showResult("archiveResult", false, String(e));
  }
}

/* ---------- Batch26：存一份自带数据的快照（单文件 HTML，双击就能看） ---------- */
function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function snapshotHtml() {
  const now = Date.now();
  const items = (lastPipes.items || []);
  const meta = lastPipes.meta || {};
  const filters = [
    state.status ? `状态=${state.status}` : "状态=全部",
    state.q ? `关键字=${state.q}` : "关键字=无",
    state.sort === "start_time_asc" ? "时间：最早在前" : "时间：最新在前",
  ].join("；");
  let maxMs = 0;
  items.forEach((p) => { const s = pipeSpan(p, now); if (Number.isFinite(s.ms) && s.ms > maxMs) maxMs = s.ms; });
  const stalled = items.filter((p) => stallInfo(p, now));

  const pipeRows = items.map((p) => {
    const s = pipeSpan(p, now);
    const st = stallInfo(p, now);
    return "<tr" + (st ? ' class="bad"' : "") + ">"
      + `<td class="mono">${esc(p.trace_id)}</td>`
      + `<td>${esc(p.overall_status)}${st ? ' <span class="stamp">' + esc(st.label) + " " + fmtDur(st.ms) + "</span>" : ""}</td>`
      + `<td class="num">${p.end_time ? fmtDur(s.ms) : fmtDur(s.ms) + "（未收尾）"}</td>`
      + `<td class="num">${esc(fmtStamp(p.start_time))}</td>`
      + `<td class="num">${esc(fmtStamp(p.end_time))}</td>`
      + `<td>${esc(p.pause_flag ? "已暂停" : "正常")}</td>`
      + `<td>${esc(p.original_demand)}</td></tr>`;
  }).join("");

  const workers = (lastWorkers || []).filter((w) => {
    if (state.workerFilter && w.worker_id !== state.workerFilter) return false;
    if (state.workerOnlineOnly && !w.online) return false;
    return true;
  });
  const workerRows = workers.map((w) => {
    const last = w.last_seen ? new Date(w.last_seen).getTime() : NaN;
    const age = Number.isFinite(last) ? now - last : NaN;
    const late = Number.isFinite(age) && age > HEARTBEAT_WARN_MS;
    return "<tr>" + `<td class="mono">${esc(w.worker_id)}</td><td class="mono">${esc(w.instance_id || "default")}</td>`
      + `<td>${esc(workerRegistryState(w).label)}</td>`
      + `<td class="num">${esc(w.in_flight)}/${esc(w.concurrency)}</td>`
      + `<td class="num">${esc(w.handled)}</td>`
      + `<td class="${late ? "late" : ""}">${esc(fmtAgo(age))}</td></tr>`;
  }).join("");

  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>调度台账快照 · ${esc(new Date(now).toISOString().slice(0, 10))}</title>
<style>
body{margin:0;padding:28px 32px;background:#f1f2ec;color:#171a15;
  font:14px/1.6 "Microsoft YaHei","PingFang SC","Hiragino Sans GB",sans-serif;}
h1{margin:0 0 4px;font-size:20px;letter-spacing:.06em;font-weight:700;}
h2{margin:26px 0 6px;font-size:13px;letter-spacing:.08em;font-weight:700;
  border-bottom:2px solid #171a15;padding-bottom:4px;}
.meta{color:#6e7265;font-size:12.5px;border-top:2px solid #171a15;padding-top:8px;margin-bottom:2px;}
table{width:100%;border-collapse:collapse;font-size:12.5px;}
th{text-align:left;font-size:11.5px;letter-spacing:.02em;padding:6px 8px;
  border-top:1px solid #b9bdb0;border-bottom:1px solid #b9bdb0;}
td{padding:7px 8px;border-bottom:1px dotted #cfd3c6;color:#464a40;vertical-align:top;}
tr.bad td{box-shadow:inset 2px 0 0 #8a5a12;}
.mono{font-family:Consolas,"Cascadia Mono",Menlo,monospace;font-size:12px;}
.num{text-align:right;white-space:nowrap;}
.stamp{border:1px solid #8a5a12;border-radius:2px;padding:0 5px;font-size:11px;color:#8a5a12;}
.late{color:#8b2e22;}
footer{margin-top:22px;color:#6e7265;font-size:12px;border-top:1px solid #b9bdb0;padding-top:8px;}
</style></head><body>
<h1>调度台账快照</h1>
<div class="meta">生成时间 ${esc(fmtStamp(new Date(now).toISOString(), true))}　·　生成人 ${esc(state.operator || "（未填）")}
　·　筛选 ${esc(filters)}　·　第 ${esc(meta.page || 1)}/${esc(meta.pages || 1)} 页，共 ${esc(meta.total || items.length)} 条</div>
<h2>流水线 ${stalled.length ? `（超时 ${stalled.length} 条）` : ""}</h2>
<table><thead><tr><th>trace_id</th><th>状态</th><th>耗时</th><th>起始</th><th>收尾</th><th>暂停</th><th>原始需求</th></tr></thead>
<tbody>${pipeRows || '<tr><td colspan="7">没有符合条件的流水线</td></tr>'}</tbody></table>
<h2>执行方</h2>
<table><thead><tr><th>执行方</th><th>实例</th><th>状态</th><th>在跑/并发</th><th>累计完成</th><th>最近上报</th></tr></thead>
<tbody>${workerRows || '<tr><td colspan="6">没有执行方上报</td></tr>'}</tbody></table>
<footer>本文件自带数据，不连服务也能看；数据截止上面的生成时间。<br>
由调度台账看板导出，同一份数据的机器可读版本见详情页的「导出 JSON / CSV」。</footer>
</body></html>`;
}

function saveSnapshot() {
  const html = snapshotHtml();
  const blob = new Blob([html], { type: "text/html;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date();
  const p2 = (n) => String(n).padStart(2, "0");
  a.href = url;
  a.download = `调度台账快照-${stamp.getFullYear()}${p2(stamp.getMonth() + 1)}${p2(stamp.getDate())}-${p2(stamp.getHours())}${p2(stamp.getMinutes())}.html`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function bind() {
  el("saveToken").addEventListener("click", () => {
    state.token = el("tokenInput").value.trim();
    localStorage.setItem("mwp_token", state.token);
    checkHealth();
    loadWorkers();
    loadPipelines();
  });
  el("reloadPipes").addEventListener("click", () => { state.page = 1; loadPipelines().then(markRefreshed); });
  if (el("snapPipes")) el("snapPipes").addEventListener("click", saveSnapshot);
  el("autoRefresh").addEventListener("change", (e) => setAutoRefresh(e.target.value));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.autoMs) {
      refreshOnce();
      scheduleAuto();
    }
  });
  // Batch15：执行方筛选（纯客户端过滤，不再打后端）
  el("workerFilter").addEventListener("change", (e) => { state.workerFilter = e.target.value; renderRegistry(lastWorkers); });
  el("workerOnlineOnly").addEventListener("change", (e) => { state.workerOnlineOnly = e.target.checked; renderRegistry(lastWorkers); });
  el("workerInstanceSearch").addEventListener("input", (e) => { state.workerInstance = e.target.value; renderRegistry(lastWorkers); });

  // Batch22：审计筛选与分页。筛选走"最近 200 条内当页过滤"，翻页走服务端 offset
  let auditTimer = null;
  el("auditSearch").addEventListener("input", (e) => {
    clearTimeout(auditTimer);
    const value = e.target.value;
    auditTimer = setTimeout(() => { state.auditKeyword = value; loadAudit({ reset: true }); }, 400);
  });
  el("reloadAudit").addEventListener("click", () => loadAudit({ reset: true }));
  el("auditPrev").addEventListener("click", () => {
    if (state.auditPage > 1) { state.auditPage -= 1; loadAudit(); }
  });
  el("auditNext").addEventListener("click", () => {
    if (state.auditPage < state.auditPages) { state.auditPage += 1; loadAudit(); }
  });

  // Batch22：操作者名字（只记不自证），存本地，随每个请求带 X-Operator
  el("operatorInput").addEventListener("input", (e) => {
    state.operator = e.target.value.trim();
    localStorage.setItem(OPERATOR_KEY, state.operator);
  });

  el("pipeStatus").addEventListener("change", (e) => { state.status = e.target.value; state.page = 1; loadPipelines(); });
  el("pipeSort").addEventListener("change", (e) => { state.sort = e.target.value; state.page = 1; loadPipelines(); });
  let searchTimer = null;
  el("pipeSearch").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const value = e.target.value.trim();
    searchTimer = setTimeout(() => {
      state.q = value;
      state.page = 1;
      loadPipelines();
    }, 400);
  });
  el("pipePrev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadPipelines(); } });
  el("pipeNext").addEventListener("click", () => { if (state.page < state.pages) { state.page += 1; loadPipelines(); } });
  el("btnPause").addEventListener("click", () => sendCommand("master:pause"));
  el("btnResume").addEventListener("click", () => sendCommand("master:resume"));
  el("btnClearBreaker").addEventListener("click", () => sendCommand("master:clear-breaker"));
  el("fillTmpl").addEventListener("click", () => {
    const key = el("tmplSelect").value;
    if (!key) return;
    el("msgBody").value = JSON.stringify(TEMPLATES[key](nowIso()), null, 2);
  });
  el("sendMsg").addEventListener("click", sendMessage);
  el("fmtMsg").addEventListener("click", () => {
    try {
      el("msgBody").value = JSON.stringify(JSON.parse(el("msgBody").value), null, 2);
    } catch (e) {
      showResult("msgResult", false, "JSON 解析失败：" + e.message);
    }
  });
  el("queryArchive").addEventListener("click", queryArchive);
}

state.token = localStorage.getItem("mwp_token") || "";
el("tokenInput").value = state.token;
state.operator = localStorage.getItem(OPERATOR_KEY) || "";
el("operatorInput").value = state.operator;
bind();
checkHealth();
loadWorkers();
loadAudit();
loadPipelines();
setAutoRefresh(localStorage.getItem(AUTO_KEY) || 0);
