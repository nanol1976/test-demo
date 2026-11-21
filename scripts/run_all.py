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

# 验收永远跑「无鉴权」模式（Batch27）：仓库里有 .env（正式 token）时，
# 各 verify 脚本的请求否则会被 401 打回，红的是环境不是代码。
# Batch28：只读口令同样算鉴权开关，两枚都要清空。
os.environ["API_TOKEN"] = ""
os.environ["API_TOKEN_READONLY"] = ""

SCRIPTS = [
    ("Batch2 消息总线", "verify_b2.py"),
    ("Batch3 查询/导出/鉴权", "verify_b3.py"),
    ("Batch4 超时/熔断/暂停/DAG", "verify_b4.py"),
    ("Batch5 极简前端", "verify_b5.py"),
    ("Batch7 迁移框架/自动刷新/CI", "verify_b7.py"),
    ("Batch8 查询增强/差量刷新/迁移接管", "verify_b8.py"),
    ("Batch9 真 Worker 端到端（轮询/执行/心跳/回执）", "verify_b9.py"),
    ("Batch10 越权强校验/重放拦截", "verify_b10.py"),
    ("Batch11 Worker 并发/优先级/优雅退出/守护模板", "verify_b11.py"),
    ("Batch12 执行方在线视图/弹性并发", "verify_b12.py"),
    ("Batch13 执行方注册表/精确在线", "verify_b13.py"),
    ("Batch14 多实例标识（worker_id + instance_id）", "verify_b14.py"),
    ("Batch15 实例并发配额下发/看板筛选", "verify_b15.py"),
    ("Batch16 并发配额下发审计（append-only）", "verify_b16.py"),
    ("Batch17 Windows 计划任务静默化（不弹窗）", "verify_b17.py"),
    ("Batch18 审计覆盖全部指令 + 只读查询接口", "verify_b18.py"),
    ("Batch22 审计真分页 + 操作者留痕 + 配额可见反馈", "verify_b22.py"),
    ("Batch23 Windows 部署件入库 + 一键注册三任务", "verify_b23.py"),
    ("Batch24 默认库地址改回 mvp.db + data/ 卫生", "verify_b24.py"),
    ("Batch25 explorer 守护 + data/ 每日巡检", "verify_b25.py"),
    ("Batch26 看板：停滞/时间轴/心跳/快照", "verify_b26.py"),
    ("Batch27 手机访问：绑定可配 + token + 窄屏", "verify_b27.py"),
    ("Batch28 只读口令：能看不能动", "verify_b28.py"),
    ("Batch29 优雅停止：标记文件 + worker 认领", "verify_b29.py"),
    ("Batch32-1 异构复核：派生/逐项/闸门/只写一次", "verify_b32.py"),
    ("Batch32-2A 插件沉淀：目录规范/元数据/版本递增/授权字段", "verify_b32_2a.py"),
    ("Batch32-2B 插件授权：实例>岗位>撤权 + 拒单审计", "verify_b32_2b.py"),
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
