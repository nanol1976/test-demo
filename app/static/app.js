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

const state = { token: "", page: 1, size: 20, pages: 1, total: 0, status: "", trace: null };

const API_TIMEOUT_MS = 30000;
const RESULT_MAX_CHARS = 20000;

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

function renderPipelines(payload) {
  const wrap = el("pipeTableWrap");
  wrap.innerHTML = "";
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
    return;
  }

  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>trace_id</th><th>状态</th><th>暂停</th><th>起始时间</th><th>原始需求</th></tr></thead>";
  const tbody = document.createElement("tbody");
  items.forEach((p) => {
    const row = document.createElement("tr");
    row.className = "clickable";
    if (p.trace_id === state.trace) row.classList.add("selected");
    const c1 = document.createElement("td");
    c1.appendChild(text(p.trace_id, "mono"));
    const c2 = document.createElement("td");
    c2.appendChild(tag(p.overall_status));
    const c3 = document.createElement("td");
    c3.appendChild(text(p.pause_flag ? "已暂停" : "正常"));
    const c4 = document.createElement("td");
    c4.appendChild(text(p.start_time));
    const c5 = document.createElement("td");
    c5.appendChild(text(p.original_demand));
    [c1, c2, c3, c4, c5].forEach((c) => row.appendChild(c));
    row.addEventListener("click", () => loadDetail(p.trace_id));
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
}

async function loadPipelines() {
  const query = new URLSearchParams();
  if (state.status) query.set("status", state.status);
  query.set("page", String(state.page));
  query.set("size", String(state.size));
  try {
    const r = await api(ROUTES.pipelines + "?" + query.toString());
    if (!r.ok) {
      el("pipeTableWrap").innerHTML = `<div class="empty">加载失败：${r.status} ${JSON.stringify(r.body)}</div>`;
      return;
    }
    renderPipelines(r.body);
  } catch (e) {
    el("pipeTableWrap").innerHTML = `<div class="empty">请求异常：${e}</div>`;
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
  wrap.innerHTML = "";
  const tasks = p.tasks || [];
  el("taskCount").textContent = `共 ${tasks.length} 个`;
  if (!tasks.length) {
    wrap.innerHTML = '<div class="empty">该流水线还没有任务</div>';
    return;
  }
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>task_id</th><th>状态</th><th>执行方</th><th>派发时间</th><th>重试</th><th>依赖</th><th>原因</th></tr></thead>";
  const tbody = document.createElement("tbody");
  tasks.forEach((t) => {
    const row = document.createElement("tr");
    const c1 = document.createElement("td"); c1.appendChild(text(t.task_id, "mono"));
    const c2 = document.createElement("td"); c2.appendChild(tag(t.status));
    const c3 = document.createElement("td"); c3.appendChild(text(t.target_worker));
    const c4 = document.createElement("td"); c4.appendChild(text(t.dispatch_time));
    const c5 = document.createElement("td"); c5.appendChild(text(`${t.remaining_retry}/${t.max_retry}`));
    const c6 = document.createElement("td"); c6.appendChild(text((t.depends_on || []).join(", ") || "—"));
    const c7 = document.createElement("td");
    c7.appendChild(text(t.abort_reason || t.reject_reason || "—"));
    [c1, c2, c3, c4, c5, c6, c7].forEach((c) => row.appendChild(c));
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
}

async function loadDetail(traceId) {
  clearResult("cmdResult");
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
    loadPipelines();
  } catch (e) {
    el("taskTableWrap").innerHTML = `<div class="empty">请求异常：${e}</div>`;
  }
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
    loadPipelines();
  });
  el("reloadPipes").addEventListener("click", () => { state.page = 1; loadPipelines(); });
  el("pipeStatus").addEventListener("change", (e) => { state.status = e.target.value; state.page = 1; loadPipelines(); });
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
loadPipelines();
