#!/usr/bin/env python3
"""
每日快照生成器：拉取平台接口，生成单文件 HTML 台账快照。
只使用标准库，无第三方依赖。
"""

import argparse
import html
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta


# ──────────────────────────────────────────────────────────────────────
# 常量与配置
# ──────────────────────────────────────────────────────────────────────
STALL_MINUTES = 10          # 停滞判定阈值（分钟）
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_PIPELINE_LIMIT = 50
DEFAULT_AUDIT_LIMIT = 20

# 台账配色（参考 style.css 的 --sheet / --ink / --rule / --stamp / --bad / --warn）
SHEET = "#f1f2ec"
INK = "#171a15"
INK2 = "#464a40"
MUTED = "#6e7265"
RULE = "#b9bdb0"
RULE2 = "#cfd3c6"
STAMP = "#1c5a45"
STAMP_INK = "#123a2d"
WARN = "#8a5a12"
BAD = "#8b2e22"
MONO = 'Consolas, "Cascadia Mono", "SF Mono", Menlo, "Courier New", monospace'


def load_env_token() -> str:
    """从仓库根的 .env 读 API_TOKEN。

    计划任务/无人值守场景不会继承交互式环境变量，所以这里回退到 .env；
    只认 API_TOKEN=（API_TOKEN_READONLY= 前缀不同，不会被误取）。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_root, ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("API_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return ""
    return ""


# ──────────────────────────────────────────────────────────────────────
# HTTP 工具
# ──────────────────────────────────────────────────────────────────────
def http_get(url: str, token: str) -> tuple[int, dict]:
    """发起 GET 请求，返回 (status_code, json_data)。"""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("X-API-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.reason}
    except Exception as e:
        return 0, {"error": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 数据处理
# ──────────────────────────────────────────────────────────────────────
def parse_iso(ts: str) -> datetime | None:
    """解析 ISO 格式时间字符串，统一转为本地【naive】时间。

    接口返回的时间可能带时区（如 2026-11-20T05:12:33+00:00 或 ...Z），
    而 datetime.now() 是 naive 的，两者直接相减会抛 TypeError。
    因此在入口处一次性归一到本地时间并去掉 tzinfo，下游统一可比较、可显示。
    """
    if not ts:
        return None
    try:
        # 兼容带/不带毫秒、带/不带时区
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def fmt_dt(dt: datetime | None) -> str:
    """格式化为本地时间字符串，None 返回 —。"""
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_date(dt: datetime | None) -> str:
    """仅日期部分。"""
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d")


def is_stalled(status: str, started_at: str) -> tuple[bool, str]:
    """判断是否停滞：pending/running 且开始超 10 分钟。返回 (是否停滞, 显示文本)。"""
    if status not in ("pending", "running"):
        return False, ""
    dt = parse_iso(started_at)
    if dt is None:
        return False, ""
    now = datetime.now()
    if now - dt > timedelta(minutes=STALL_MINUTES):
        minutes = int((now - dt).total_seconds() // 60)
        return True, f"停滞 {minutes}m"
    return False, ""


STATUS_LABEL = {
    "pending": ("pending", MUTED),
    "running": ("running", INK),
    "success": ("success", STAMP),
    "failed": ("failed", BAD),
    "aborted": ("aborted", WARN),
}


def status_badge(status: str) -> str:
    """渲染状态徽章：文字 + 颜色，不只靠颜色区分。"""
    label, color = STATUS_LABEL.get(status, (status, INK))
    return f'<span class="badge" style="color:{color}">{html.escape(label)}</span>'


# ──────────────────────────────────────────────────────────────────────
# HTML 渲染
# ──────────────────────────────────────────────────────────────────────
def render_html(
    pipelines: list[dict],
    workers: list[dict],
    audit: list[dict],
    base_url: str,
    gen_time: datetime,
) -> str:
    """渲染完整的单文件 HTML。"""

    # —— 流水线表格行 ——
    pipe_rows = []
    for p in pipelines:
        trace_id = html.escape(str(p.get("trace_id", "—")))
        status = str(p.get("overall_status", "—"))
        started = html.escape(fmt_dt(parse_iso(p.get("start_time"))))
        ended = html.escape(fmt_dt(parse_iso(p.get("end_time"))))
        stalled, stall_text = is_stalled(status, p.get("start_time", ""))
        stall_cell = f'<span class="stall" style="color:{BAD}">{html.escape(stall_text)}</span>' if stalled else "—"
        pipe_rows.append(f"""
        <tr{' class="stalled"' if stalled else ''}>
            <td class="mono">{trace_id}</td>
            <td>{status_badge(status)}</td>
            <td class="mono">{started}</td>
            <td class="mono">{ended}</td>
            <td class="mono">{stall_cell}</td>
        </tr>""")

    # —— 执行方表格行 ——
    worker_rows = []
    for w in workers:
        worker_id = html.escape(str(w.get("worker_id", "—")))
        instance = html.escape(str(w.get("instance_id", "—")))
        status = str(w.get("status", "—"))
        concurrency = html.escape(str(w.get("concurrency", "—")))
        limit = html.escape(str(w.get("limit", "—")))
        last_seen_raw = w.get("last_seen")
        last_seen = html.escape(fmt_dt(parse_iso(last_seen_raw)))
        # 心跳秒数
        hb_sec = "—"
        hb_class = ""
        if last_seen_raw:
            dt = parse_iso(last_seen_raw)
            if dt:
                diff = int((datetime.now() - dt).total_seconds())
                hb_sec = str(diff)
                if diff > 120:  # 超过心跳超时
                    hb_class = "late"
        worker_rows.append(f"""
        <tr>
            <td class="mono">{worker_id}</td>
            <td class="mono">{instance}</td>
            <td>{status_badge(status)}</td>
            <td class="mono" style="text-align:right">{concurrency}</td>
            <td class="mono" style="text-align:right">{limit}</td>
            <td class="mono">{last_seen}</td>
            <td class="mono hb {hb_class}" style="text-align:right">{hb_sec}s</td>
        </tr>""")

    # —— 审计表格行 ——
    audit_rows = []
    for a in audit:
        ts = html.escape(fmt_dt(parse_iso(a.get("created_at"))))
        operator = html.escape(str(a.get("operator") or "—"))
        action = html.escape(str(a.get("command") or "—"))
        worker_id = str(a.get("worker_id") or "—")
        instance_id = str(a.get("instance_id") or "")
        target = html.escape(f"{worker_id} / {instance_id}" if instance_id else worker_id)
        # 只有配额类指令真有 prev/desired；pause/resume 之类接口也会给 0/0，直接显示会误导
        if str(a.get("command") or "") == "master:worker-limit":
            old_val = "—" if a.get("prev_limit") is None else str(a.get("prev_limit"))
            new_val = "—" if a.get("desired_limit") is None else str(a.get("desired_limit"))
        else:
            old_val = new_val = "—"
        eff = a.get("effective")
        if eff is None:
            effective = "—"
        else:
            effective = "已生效" if bool(eff) else "未生效"
        old_new = f"{old_val} → {new_val}" if old_val != "—" or new_val != "—" else "—"
        audit_rows.append(f"""
        <tr>
            <td class="mono">{ts}</td>
            <td>{operator}</td>
            <td>{action}</td>
            <td>{target}</td>
            <td class="mono" style="text-align:right">{old_new}</td>
            <td>{effective}</td>
        </tr>""")

    # —— 组装完整 HTML ——
    date_str = gen_time.strftime("%Y-%m-%d")
    time_str = gen_time.strftime("%Y-%m-%d %H:%M:%S")
    total_line = f"共 {len(pipelines)} 条流水线 / {len(workers)} 个执行方 / {len(audit)} 条审计"

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>台账快照 · {html.escape(date_str)} · {html.escape(time_str)}</title>
<style>
:root {{
  --sheet: {SHEET};
  --ink: {INK};
  --ink-2: {INK2};
  --muted: {MUTED};
  --rule: {RULE};
  --rule-2: {RULE2};
  --stamp: {STAMP};
  --stamp-ink: {STAMP_INK};
  --warn: {WARN};
  --bad: {BAD};
  --mono: {MONO};
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --sheet: #12140f;
    --pan: #1b1e17;
    --pan-2: #171a13;
    --ink: #e8eae1;
    --ink-2: #b9bdb0;
    --muted: #8b8f81;
    --rule: #3a3f33;
    --rule-2: #2a2e25;
    --stamp: #6fbf9b;
    --stamp-ink: #0e241b;
    --warn: #d0a05a;
    --bad: #d48376;
  }}
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0;
  background: var(--sheet);
  color: var(--ink);
  font: 14px/1.6 "PingFang SC", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
  font-variant-numeric: tabular-nums;
  -webkit-font-smoothing: antialiased;
}}
::selection {{ background: var(--stamp); color: var(--sheet); }}
.mono {{ font-family: var(--mono); }}
header {{
  display: flex; align-items: flex-end; gap: 16px;
  padding: 22px 32px 12px;
  max-width: 1320px; margin: 0 auto;
  border-bottom: 3px solid var(--ink);
}}
header h1 {{
  font-size: 26px; margin: 0; font-weight: 700; letter-spacing: .02em; line-height: 1.1;
}}
header h1::after {{
  content: "离线存档 · 发人可看";
  display: block; margin-top: 6px;
  font-size: 11.5px; font-weight: 400; letter-spacing: .08em;
  color: var(--muted);
}}
main {{
  max-width: 1320px; margin: 0 auto;
  padding: 0 32px 48px;
}}
.panel {{
  border-top: 2px solid var(--ink);
  padding: 12px 0 26px;
  margin: 0;
}}
.panel + .panel {{ margin-top: 6px; }}
.panel h2 {{
  display: flex; align-items: baseline; gap: 10px;
  font-size: 15px; margin: 0 0 12px; font-weight: 700; letter-spacing: .02em;
}}
.panel h2 .count {{
  margin-left: auto;
  font-size: 11.5px; font-weight: 400; color: var(--muted);
}}
table {{
  width: 100%; border-collapse: collapse; font-size: 13px;
}}
th, td {{
  padding: 8px 10px; vertical-align: top; text-align: left;
}}
thead th {{
  color: var(--muted); font-weight: 600; font-size: 12px;
  border-bottom: 2px solid var(--ink);      /* 表头双线：上粗下细 */
  background: transparent;
}}
thead th::after {{
  content: ""; display: block; height: 1px; margin-top: 2px;
  background: var(--rule);
}}
tbody tr {{ border-bottom: 1px solid var(--rule-2); }}
tbody tr:last-child {{ border-bottom: 2px solid var(--ink); }}  /* 表尾重线 */
tbody tr.stalled td:first-child::before {{
  content: ""; display: inline-block; width: 4px; height: 100%;
  background: var(--bad); margin-right: 8px; vertical-align: top;
}}
.badge {{ display: inline-block; padding: 1px 6px; border-radius: 2px; font-size: 12px; }}
.stall {{ font-weight: 700; }}
.hb.late {{ color: var(--bad); font-weight: 700; }}
footer {{
  max-width: 1320px; margin: 24px auto 0; padding: 12px 32px;
  color: var(--muted); font-size: 12px; border-top: 1px solid var(--rule-2);
}}
@media (max-width: 640px) {{
  header {{ padding: 14px; }}
  main {{ padding: 0 14px 32px; }}
  table {{ min-width: 700px; }}
  [id$="TableWrap"] {{ overflow-x: auto; }}
}}
</style>
</head>
<body>
<header>
  <h1>台账快照 · {html.escape(date_str)}</h1>
</header>
<main>
  <section class="panel" id="pipelines">
    <h2>流水线<span class="count mono">{len(pipelines)}</span></h2>
    <div id="pipelinesTableWrap">
      <table>
        <thead>
          <tr>
            <th>trace_id</th>
            <th>状态</th>
            <th>开始</th>
            <th>结束</th>
            <th>停滞</th>
          </tr>
        </thead>
        <tbody>
{''.join(pipe_rows) if pipe_rows else '<tr><td colspan="5" class="empty">暂无数据</td></tr>'}
        </tbody>
      </table>
    </div>
  </section>

  <section class="panel" id="workers">
    <h2>执行方<span class="count mono">{len(workers)}</span></h2>
    <div id="workersTableWrap">
      <table>
        <thead>
          <tr>
            <th>worker_id</th>
            <th>实例</th>
            <th>状态</th>
            <th style="text-align:right">并发</th>
            <th style="text-align:right">限额</th>
            <th>最近上报</th>
            <th style="text-align:right">心跳秒数</th>
          </tr>
        </thead>
        <tbody>
{''.join(worker_rows) if worker_rows else '<tr><td colspan="7" class="empty">暂无数据</td></tr>'}
        </tbody>
      </table>
    </div>
  </section>

  <section class="panel" id="audit">
    <h2>运维审计<span class="count mono">{len(audit)}</span></h2>
    <div id="auditTableWrap">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>操作者</th>
            <th>动作</th>
            <th>目标</th>
            <th style="text-align:right">旧→新</th>
            <th>是否生效</th>
          </tr>
        </thead>
        <tbody>
{''.join(audit_rows) if audit_rows else '<tr><td colspan="6" class="empty">暂无数据</td></tr>'}
        </tbody>
      </table>
    </div>
  </section>
</main>
<footer>
  生成时间 {html.escape(time_str)}　数据来源 {html.escape(base_url)}　{html.escape(total_line)}
</footer>
</body>
</html>"""
    return html_content


# ──────────────────────────────────────────────────────────────────────
# 正常模式：拉取接口并生成
# ──────────────────────────────────────────────────────────────────────
def run_normal(args) -> int:
    token = args.token or os.environ.get("API_TOKEN", "") or load_env_token()

    # 拉取三个接口
    # 注意：pipelines 是 {meta, items} 分页包裹，且分页参数是 size（不是 limit）；
    #      audit 是 {meta, current, items} 包裹，参数是 limit。
    endpoints = [
        ("pipelines", f"{args.base_url}/api/pipelines?size={args.pipeline_limit}"),
        ("workers", f"{args.base_url}/api/workers"),
        ("audit", f"{args.base_url}/api/audit?limit={args.audit_limit}"),
    ]

    data = {}
    for name, url in endpoints:
        code, resp = http_get(url, token)
        if code != 200:
            if code in (401, 403):
                print(f"[FAIL] GET {url} => {code}  口令不对或没给（看板右上角那枚 API_TOKEN）")
            else:
                print(f"[FAIL] GET {url} => {code}")
            return 2
        # 归一：分页接口取 items；裸列表接口直接用
        if isinstance(resp, dict):
            data[name] = resp.get("items") or []
        elif isinstance(resp, list):
            data[name] = resp
        else:
            data[name] = []

    # 生成 HTML
    gen_time = datetime.now()
    html_out = render_html(
        pipelines=data["pipelines"],
        workers=data["workers"],
        audit=data["audit"],
        base_url=args.base_url,
        gen_time=gen_time,
    )

    # 写文件
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_out)

    abs_path = os.path.abspath(args.out)
    print(f"已生成：{abs_path}（流水线 {len(data['pipelines'])} 条 / 执行方 {len(data['workers'])} 个 / 审计 {len(data['audit'])} 条）")
    return 0


# ──────────────────────────────────────────────────────────────────────
# Selftest：内置样例数据，离线自检
# ──────────────────────────────────────────────────────────────────────
SAMPLE_PIPELINES = [
    {"trace_id": "pipe-001", "overall_status": "success", "start_time": "2026-11-20T10:00:00", "end_time": "2026-11-20T10:05:00"},
    {"trace_id": "pipe-002", "overall_status": "running", "start_time": "2026-11-20T10:10:00", "end_time": None},
    {"trace_id": "pipe-003", "overall_status": "pending", "start_time": "2026-11-20T08:00:00", "end_time": None},  # 超 10 分钟 -> 停滞
    {"trace_id": "pipe-004", "overall_status": "failed", "start_time": "2026-11-20T09:00:00", "end_time": "2026-11-20T09:30:00"},
    {"trace_id": "<script>alert(1)</script>", "overall_status": "success", "start_time": "2026-11-20T11:00:00", "end_time": "2026-11-20T11:01:00"},  # XSS 测试
]

SAMPLE_WORKERS = [
    {"worker_id": "worker-a", "instance_id": "host-1", "status": "online", "concurrency": 3, "limit": 5, "last_seen": "2026-11-20T10:25:00"},
    {"worker_id": "worker-b", "instance_id": "host-2", "status": "offline", "concurrency": 0, "limit": 4, "last_seen": "2026-11-20T08:00:00"},  # 心跳超时
    {"worker_id": "worker-c", "instance_id": "host-3", "status": "online", "concurrency": 2, "limit": 2, "last_seen": "2026-11-20T22:28:33+00:00"},  # 带时区，验时间归一
]

SAMPLE_AUDIT = [
    {"created_at": "2026-11-20T10:00:00+08:00", "operator": "alice", "command": "master:worker-limit", "worker_id": "worker-a", "instance_id": "", "prev_limit": 5, "desired_limit": 3, "effective": True},
    {"created_at": "2026-11-20T09:30:00+08:00", "operator": "bob", "command": "master:pause", "worker_id": "master", "instance_id": "trace:t-1", "prev_limit": None, "desired_limit": None, "effective": None},
    {"created_at": "2026-11-20T09:00:00+08:00", "operator": "carol", "command": "master:clear-breaker", "worker_id": "master", "instance_id": "trace:t-2", "prev_limit": None, "desired_limit": None, "effective": False},
]


def run_selftest() -> int:
    """使用内置样例数据渲染到临时文件，逐条断言。"""
    gen_time = datetime.now()
    html_out = render_html(
        pipelines=SAMPLE_PIPELINES,
        workers=SAMPLE_WORKERS,
        audit=SAMPLE_AUDIT,
        base_url="http://127.0.0.1:8000",
        gen_time=gen_time,
    )

    # 写临时文件
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html_out)
        tmp_path = f.name

    try:
        with open(tmp_path, "r", encoding="utf-8") as f:
            content = f.read()

        checks = [
            ("文件已落盘", os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0),
            ("含 meta charset=utf-8", '<meta charset="utf-8">' in content),
            ("含三个区块标题", all(s in content for s in ["流水线", "执行方", "运维审计"])),
            ("含停滞字样", "停滞" in content),
            ("含执行方 online 与心跳秒数", "online" in content and "心跳秒数" in content),
            ("审计行含旧→新", "→" in content),
            ("XSS 被转义", "<script>alert(1)</script>" not in content),
            ("生成时间与日期出现在标题/页脚", gen_time.strftime("%Y-%m-%d") in content and gen_time.strftime("%H:%M:%S") in content),
            # —— 以下四条按真实接口 schema 断言，防止字段名漂移导致整表渲染成破折号 ——
            ("流水线按真实字段渲染（trace_id / overall_status）", "pipe-001" in content and "success" in content),
            ("执行方按真实字段渲染（instance_id）", "host-1" in content),
            ("审计按真实字段渲染（operator / command）", "alice" in content and "master:worker-limit" in content),
            ("审计旧→新取 prev_limit / desired_limit", "5 → 3" in content),
        ]

        all_pass = True
        for label, ok in checks:
            status = "[PASS]" if ok else "[FAIL]"
            print(f"  {status} {label}")
            if not ok:
                all_pass = False

        if all_pass:
            print("全部通过")
            return 0
        else:
            return 1
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="每日快照生成器")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="平台基础 URL（默认 http://127.0.0.1:8000）")
    parser.add_argument("--token", default="", help="API Token（默认读环境变量 API_TOKEN）")
    parser.add_argument("--out", default="", help="输出文件路径（默认 data/snapshots/<今天>.html）")
    parser.add_argument("--pipeline-limit", type=int, default=DEFAULT_PIPELINE_LIMIT, help="流水线取数上限")
    parser.add_argument("--audit-limit", type=int, default=DEFAULT_AUDIT_LIMIT, help="审计取数上限")
    parser.add_argument("--selftest", action="store_true", help="离线自检（不联网）")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(run_selftest())

    # 正常模式：确定输出路径
    if not args.out:
        today = datetime.now().strftime("%Y-%m-%d")
        args.out = f"data/snapshots/{today}.html"

    sys.exit(run_normal(args))


if __name__ == "__main__":
    main()
