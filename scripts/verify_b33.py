"""Batch33 验收：DSH 适配层（adapter=dsh），离线可跑、不碰真 API。

守的九条（每条都能红）：
1. NVIDIA patch 在仓库里，且有 baseURL / apiKeyEnv / 默认模型；
2. 凭证只从 .credentials.yaml 的 refs 段取，缺失就报错（不许猜、不许从别处凑）；
3. 命令行带 --profile headless 与 --patch；
4. 桩程序退出 0 → exit_code 0、stdout 原样带回来；
5. 桩程序退出 3 → exit_code 3（不吞错）；
6. 没 key → DshError 明确说"没找到"，而不是起不来个莫名的错；
7. 超时 → DshError（不是挂住）；
8. Worker 里 adapter 分支排在 script 解析之前（dsh 不需要 script）；
9. 仓库里没有硬编码的 nvapi key（不许把 key 提交进 git）。

用法：python scripts/verify_b33.py
退出码：0 全绿 / 1 有失败。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "worker"))

import dsh_adapter  # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def main():
    print("=== 1) 仓库内的 NVIDIA patch ===")
    patch = os.path.join(ROOT, "worker", "dsh", "nvidia-patch.yml")
    text = open(patch, encoding="utf-8").read()
    check("patch 在仓库内（不走 ~/.dsh）", os.path.isfile(patch))
    check("patch 指向 NVIDIA 的 OpenAI 兼容端点",
          "https://integrate.api.nvidia.com/v1" in text)
    check("patch 用 apiKeyEnv=NVIDIA_API_KEY（key 不进仓库）",
          "apiKeyEnv: NVIDIA_API_KEY" in text)
    check("patch 默认模型与 dsh_adapter 里的默认一致",
          dsh_adapter.DEFAULT_MODEL in text)

    print("\n=== 2) 凭证只认 refs ===")
    # 这台机器的 TEMP 正好指在仓库根上，桩文件直接落 run/ 里（run/ 不进 git，也免被第 9 组扫）
    tmp_base = os.path.join(ROOT, "run", "tmp_b33")
    os.makedirs(tmp_base, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="b33-", dir=tmp_base)
    # 假 key 拼出来，不在源码里留一段完整的 key 形状，免得第 9 组扫描自己扫自己
    fake_key = "nvapi" + "-TESTKEY-123456"
    fake_wrong = "nvapi" + "-WRONG-SECTION"
    good = write(os.path.join(tmp, "cred_good.yaml"),
                 f"version: 1\nrecords:\n  client/browser-session:\n    kind: grant\nrefs:\n  NVIDIA_API_KEY: {fake_key}\n")
    bad = write(os.path.join(tmp, "cred_bad.yaml"),
                f"version: 1\nrecords:\n  client/browser-session:\n    payload:\n      NVIDIA_API_KEY: {fake_wrong}\n")
    check("refs 段里的 key 能读到", dsh_adapter.read_nvidia_key(good) == fake_key)
    check("records 段里的同名键不算（不瞎抓）", dsh_adapter.read_nvidia_key(bad) is None)
    check("文件不存在返回 None", dsh_adapter.read_nvidia_key(os.path.join(tmp, "nope.yaml")) is None)

    print("\n=== 3) 命令行形状 ===")
    # 这台机器不一定装了 dsh，命令形状只验「拼出来的东西」，先指到解释器自身
    os.environ["MWP_DSH_CMD"] = f'"{sys.executable}"' if os.name == "nt" else sys.executable
    cmd = dsh_adapter.build_command("跑个冒烟", patch_path=patch)
    check("带 --profile headless", "--profile" in cmd and "headless" in cmd)
    check("带 --patch 指向仓库 patch", "--patch" in cmd and patch in cmd)
    check("goal 在最后一个参数", cmd[-1] == "跑个冒烟")

    print("\n=== 4) 桩程序：退出 0 ===")
    stub_ok = write(os.path.join(tmp, "stub_ok.py"),
                    "import os, sys\n"
                    "print('2')\n"
                    "print('keylen=%d' % len(os.environ.get('NVIDIA_API_KEY', '')))\n")
    os.environ["MWP_DSH_CMD"] = f'"{sys.executable}" {stub_ok}' if os.name == "nt" else f"{sys.executable} {stub_ok}"
    os.environ["MWP_DSH_CREDENTIALS"] = good
    res = dsh_adapter.run("1+1", timeout=30)
    check("退出码 0", res["exit_code"] == 0, f"code={res['exit_code']}")
    check("stdout 带回来", res["stdout"].splitlines()[0] == "2")
    check("key 确实进了子进程 env（长度 > 0）", "keylen=0" not in res["stdout"])

    print("\n=== 5) 桩程序：退出 3 ===")
    stub_bad = write(os.path.join(tmp, "stub_bad.py"), "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
    os.environ["MWP_DSH_CMD"] = f'"{sys.executable}" {stub_bad}' if os.name == "nt" else f"{sys.executable} {stub_bad}"
    res = dsh_adapter.run("x", timeout=30)
    check("退出码原样返回 3", res["exit_code"] == 3)
    check("stderr 留着（拒单理由要用）", "boom" in res["stderr"])

    print("\n=== 6) 没 key 要明确报错 ===")
    os.environ["MWP_DSH_CREDENTIALS"] = bad
    try:
        dsh_adapter.run("x", timeout=30)
        check("没 key 必须抛错", False, "居然跑起来了")
    except dsh_adapter.DshError as exc:
        check("没 key 抛 DshError 且说清「没找到」", "NVIDIA_API_KEY 没找到" in str(exc), str(exc)[:60])

    print("\n=== 7) 超时 ===")
    stub_slow = write(os.path.join(tmp, "stub_slow.py"), "import time\ntime.sleep(5)\nprint('late')\n")
    os.environ["MWP_DSH_CMD"] = f'"{sys.executable}" {stub_slow}' if os.name == "nt" else f"{sys.executable} {stub_slow}"
    os.environ["MWP_DSH_CREDENTIALS"] = good
    try:
        dsh_adapter.run("x", timeout=1)
        check("超时必须抛错", False, "居然等到了")
    except dsh_adapter.DshError as exc:
        check("超时抛 DshError", "超时" in str(exc), str(exc)[:60])

    print("\n=== 8) Worker 接线（adapter 分支在 script 解析之前） ===")
    worker_src = open(os.path.join(ROOT, "worker", "agent_worker.py"), encoding="utf-8").read()
    check("Worker 认得 adapter", "def _handle_adapter" in worker_src)
    check("adapter 分支排在建 cmd / resolve_script 之前",
          worker_src.index('spec.get("adapter")') < worker_src.index("resolve_script(spec.get"))
    check("不认识的 adapter 直接拒", "不认识的 adapter" in worker_src)
    check("dsh 失败按退出码拒单（不当作成功）",
          'f"dsh 退出码 {result' in worker_src)

    print("\n=== 9) 仓库里不许出现真 key ===")
    hits = []
    key_prefix = "nvapi" + "-"
    tmp_abs = os.path.abspath(tmp)
    for base, dirs, files in os.walk(ROOT):
        if any(part in base for part in (".git", "run", "archive", "data", ".venv311")):
            continue
        if os.path.abspath(base).startswith(tmp_abs):
            continue  # 第 2 组自己造假 credentials，别扫自己造的
        for name in files:
            if not name.endswith((".py", ".yml", ".yaml", ".md", ".json", ".ps1", ".sh")):
                continue
            path = os.path.join(base, name)
            try:
                body = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if key_prefix in body:
                hits.append(os.path.relpath(path, ROOT))
    check("没有硬编码 nvapi key", not hits, "、".join(hits[:3]))

    shutil.rmtree(tmp_base, ignore_errors=True)

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
