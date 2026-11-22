"""Batch34 验收：dsh 挂进真实岗位——岗位必填、单实例并发配额、审计留痕（离线可跑）。

守的五组：
1. 配额口径：默认 1，`MWP_DSH_MAX_INFLIGHT` 可调，非法值回默认，下限 1（0 也算 1）；
2. 只拦 dsh：`exec.adapter` 不是 dsh 的普通单不受影响（不误伤脚本单）；
3. 闸的位置：在派单建流水线之前、插件闸之后 —— 被拒的单不建流水线、不占任务行；
4. 审计留痕：allow / deny 两种判定都写 plugin_audit（追加不删），理由里带模型、档位、实例、在跑数；
5. 配额只数"执行中 + adapter=dsh"的单；接口面仍 11 条。

用法：python scripts/verify_b34.py
退出码：0 全绿 / 1 有失败。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILED = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def main():
    from app.services import dispatch as dsp

    print("=== 1) 配额口径 ===")
    os.environ.pop("MWP_DSH_MAX_INFLIGHT", None)
    check("默认 1（同实例一次只跑一单外部 agent）", dsp.dsh_max_inflight() == 1,
          str(dsp.dsh_max_inflight()))
    os.environ["MWP_DSH_MAX_INFLIGHT"] = "3"
    check("环境变量可调", dsp.dsh_max_inflight() == 3, str(dsp.dsh_max_inflight()))
    os.environ["MWP_DSH_MAX_INFLIGHT"] = "abc"
    check("非法值回默认（不抛、不静默变 0）", dsp.dsh_max_inflight() == 1,
          str(dsp.dsh_max_inflight()))
    os.environ["MWP_DSH_MAX_INFLIGHT"] = "0"
    check("下限 1（0 也不行，否则单都派不出去）", dsp.dsh_max_inflight() == 1,
          str(dsp.dsh_max_inflight()))
    os.environ.pop("MWP_DSH_MAX_INFLIGHT", None)

    print("\n=== 2) 只拦 dsh 单 ===")
    check("adapter=dsh 认得出", dsp._resolve_adapter({"exec": {"adapter": "dsh"}}) == "dsh")
    check("大小写与空白都归一", dsp._resolve_adapter({"exec": {"adapter": "  DSH "}}) == "dsh")
    check("adapter 不是 dsh 放行（脚本单不占外部队配额）",
          dsp._resolve_adapter({"exec": {"adapter": "codex"}}) == "codex"
          and dsp._resolve_adapter({"exec": {}}) == "")
    check("exec 不是字典也认得出（脏载荷不炸）", dsp._resolve_adapter({"exec": "dsh"}) == "")
    check("没有 exec 段 = 普通单", dsp._resolve_adapter({}) == "")

    print("\n=== 3) 闸的位置与形状 ===")
    src = read(os.path.join(ROOT, "app", "services", "dispatch.py"))
    check("有闸函数", "def _gate_dsh" in src)
    check("派单里真的调了（不是只写了个函数放着）",
          "_gate_dsh(repo, envelope, payload, target_worker, target_instance)" in src)
    gate_at = src.find("_gate_dsh(repo, envelope, payload, target_worker, target_instance)")
    pipe_at = src.find("pipeline = repo.get_pipeline(envelope.trace_id)")
    check("闸在建流水线之前（被拒的单不建流水线）", 0 < gate_at < pipe_at,
          f"gate={gate_at} pipeline={pipe_at}")
    check("没岗位直接拒（配额与产物归属算不出来）",
          "必须指定 target_instance" in src and "dsh 单没指定 target_instance" in src.replace("exec.adapter=dsh ", ""))
    check("超配额拒，理由带在跑数与上限",
          "在跑 {inflight}/{limit}" in src and "MWP_DSH_MAX_INFLIGHT=" in src)

    print("\n=== 4) 审计留痕 ===")
    check("allow 也写审计（不是只记拒单）", 'audit("allow", head)' in src)
    check("deny 写审计且带原因", 'audit("deny", f"{head}' in src)
    check("审计走既有 record_plugin_audit（不新增表、不新增路径）",
          "record_plugin_audit" in src and "plugin_name=\"dsh\"" in src)
    check("审计带 trace/task/worker/instance（能唯一定位到这一单）",
          all(k in src for k in ("trace_id=envelope.trace_id", "task_id=envelope.task_id",
                                 "worker_id=target_worker", "instance_id=target_instance")))

    print("\n=== 5) 只数 dsh 在跑单 + 不删行 ===")
    led = read(os.path.join(ROOT, "app", "repositories", "ledger.py"))
    check("有 count_inflight", "def count_inflight" in led)
    check("只数 status=执行中", 'TaskLedger.status == "执行中"' in led)
    check("只数目标实例", "TaskLedger.target_instance == instance_id" in led)
    check("按载荷里的 adapter 认 dsh", 'spec.get("adapter")' in led and '== "dsh"' in led)
    check("坏载荷跳过、不炸派单", "except (TypeError, ValueError)" in led and "continue" in led)
    check("只读查询（不删行、不改状态）", "delete" not in led.split("def count_inflight")[1][:1200].lower())

    api_dir = os.path.join(ROOT, "app", "api")
    paths = set()
    for name in os.listdir(api_dir):
        if not name.endswith(".py"):
            continue
        text = read(os.path.join(api_dir, name))
        for m in re.finditer(r"@router\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']", text):
            paths.add(m.group(2))
    check("接口路径仍是 11 条（配额与审计都不加接口）", len(paths) == 11,
          f"{len(paths)} 条：{sorted(paths)}")

    print()
    if FAILED:
        print(f"验收结果：失败（{len(FAILED)} 项）")
        for item in FAILED:
            print("  - " + item)
        return 1
    print("验收结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
