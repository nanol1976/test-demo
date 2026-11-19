"""一键跑全量验收：verify_b2 / b3 / b4 / b5 + 真实起服务冒烟。

用法：
    python scripts/run_all.py            # 全跑
    python scripts/run_all.py --no-smoke # 跳过真实起服务（CI 无端口权限时用）

退出码：0 全绿 / 1 有失败。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCRIPTS = [
    ("Batch2 消息总线", "verify_b2.py"),
    ("Batch3 查询/导出/鉴权", "verify_b3.py"),
    ("Batch4 超时/熔断/暂停/DAG", "verify_b4.py"),
    ("Batch5 极简前端", "verify_b5.py"),
    ("Batch7 迁移框架/自动刷新/CI", "verify_b7.py"),
    ("Batch8 查询增强/差量刷新/迁移接管", "verify_b8.py"),
    ("Batch9 真 Worker 端到端（轮询/执行/心跳/回执）", "verify_b9.py"),
    ("Batch10 越权强校验/重放拦截", "verify_b10.py"),
]


def run_one(label, script):
    path = os.path.join(HERE, script)
    print(f"\n{'=' * 60}\n▶ {label}  ({script})\n{'=' * 60}", flush=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    proc = subprocess.run(
        [PY, path],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = [ln for ln in out.splitlines() if ln.strip().startswith("[") or "结果：" in ln]
    for ln in tail[-6:]:
        print("  " + ln.strip(), flush=True)
    ok = proc.returncode == 0 and "全部通过" in out
    print(f"  → {'通过' if ok else '失败'}", flush=True)
    if not ok:
        print(out[-3000:], flush=True)
    return ok


def run_smoke():
    print(f"\n{'=' * 60}\n▶ 真实起服务冒烟  (smoke_http.py)\n{'=' * 60}", flush=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    proc = subprocess.run(
        [PY, os.path.join(HERE, "smoke_http.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    for ln in out.splitlines():
        print("  " + ln, flush=True)
    ok = proc.returncode == 0
    print(f"  → {'通过' if ok else '失败'}", flush=True)
    return ok


def main():
    results = []
    for label, script in SCRIPTS:
        results.append((label, run_one(label, script)))
    if "--no-smoke" not in sys.argv:
        results.append(("真实起服务冒烟", run_smoke()))
    else:
        print("\n（已跳过真实起服务冒烟）")

    print(f"\n{'=' * 60}\n总结果\n{'=' * 60}")
    for label, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    failed = [label for label, ok in results if not ok]
    if failed:
        print(f"\n失败项：{failed}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
