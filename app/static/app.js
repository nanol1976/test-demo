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
};

const API_TIMEOUT_MS = 30000;
// Batch12：执行方在线视图的心跳阈值。与平台 heartbeat_timeout_seconds 默认值保持一致口径。
const WORKER_ONLINE_MS = 120000;
const WORKER_VIEW_SIZE = 100;
const RESULT_MAX_CHARS = 20000;
const AUTO_KEY = "mwp_auto_refresh_ms";

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
  if (!items.length) {
    wrap.innerHTML = '<div class="empty">没有符合条件的流水线</div>';
    listRows.clear();
    return;
  }

  let table = wrap.querySelector("table");
  if (!table) {
    wrap.innerHTML = "";
    table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>trace_id</th><th>状态</th><th>暂停</th><th>起始时间</th><th>原始需求</th></tr></thead>";
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
  ["trace", "status", "pause", "time", "demand"].forEach((k) => {
    cells[k] = document.createElement("td");
    row.appendChild(cells[k]);
  });
  cells.trace.appendChild(text(p.trace_id, "mono"));
  row.addEventListener("click", () => loadDetail(p.trace_id));
  return { row, cells, last: { trace: p.trace_id } };
}

function updatePipelineRow(rec, p) {
  if (rec.last.status !== p.overall_status) {
    rec.last.status = p.overall_status;
    rec.cells.status.innerHTML = "";
    rec.cells.status.appendChild(tag(p.overall_status));
  }
  const pause = p.pause_flag ? "已暂停" : "正常";
  if (rec.last.pause !== pause) {
    rec.last.pause = pause;
    setCellText(rec.cells.pause, pause);
  }
  if (rec.last.time !== p.start_time) {
    rec.last.time = p.start_time;
    setCellText(rec.cells.time, p.start_time);
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
    setCellText(rec.cells.dispatch, t.dispatch_time);
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

function workerRegistryState(w) {
  if (w.status === "stopped") return { label: "停机", cls: "idle" };
  if (w.status === "stopping") return { label: "收尾中", cls: "warn" };
  if (w.online) return { label: "在线", cls: "ok" };
  return { label: "静默", cls: "bad" };
}

function renderRegistry(workers) {
  const wrap = el("workerTableWrap");
  if (!wrap) return;
  el("workerCount").textContent = workers.length
    ? `（在线 ${workers.filter((w) => w.online).length} / 共 ${workers.length} 个实例）`
    : "";
  if (!workers.length) {
    wrap.innerHTML = '<div class="empty">还没有 Worker 上报过（启动 Worker 并接入 /api/workers 即可）</div>';
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["执行方", "实例", "状态", "在跑", "并发", "累计完成", "最近上报", "备注"].forEach((h) => {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  });
  table.appendChild(head);
  workers.forEach((w) => {
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
    tr.appendChild(td(`${w.in_flight}`));
    tr.appendChild(td(`${w.limit}/${w.concurrency}`));
    tr.appendChild(td(String(w.handled)));
    tr.appendChild(td(new Date(w.last_seen).toLocaleTimeString()));
    tr.appendChild(td(w.note || "—", "muted"));
    table.appendChild(tr);
  });
  wrap.innerHTML = "";
  wrap.appendChild(table);
}

async function loadWorkers() {
  // Batch13：优先读执行方注册表；平台老版本（没有该接口）时退回按任务聚合。
  try {
    const reg = await api(ROUTES.workers);
    if (reg.ok && Array.isArray(reg.body)) {
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

function bind() {
  el("saveToken").addEventListener("click", () => {
    state.token = el("tokenInput").value.trim();
    localStorage.setItem("mwp_token", state.token);
    checkHealth();
    loadWorkers();
    loadPipelines();
  });
  el("reloadPipes").addEventListener("click", () => { state.page = 1; loadPipelines().then(markRefreshed); });
  el("autoRefresh").addEventListener("change", (e) => setAutoRefresh(e.target.value));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.autoMs) {
      refreshOnce();
      scheduleAuto();
    }
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
bind();
checkHealth();
loadWorkers();
loadPipelines();
setAutoRefresh(localStorage.getItem(AUTO_KEY) || 0);
