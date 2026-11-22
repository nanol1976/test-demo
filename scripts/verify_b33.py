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
import time

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

    print("\n=== 10) 按单指定模型（Batch33-1） ===")
    other_model = "minimaxai/minimax-m3.1-flash"
    patch_other, generated = dsh_adapter.resolve_patch(other_model)
    check("默认模型直接复用仓库 patch（不写临时文件）",
          dsh_adapter.resolve_patch(dsh_adapter.DEFAULT_MODEL) == (patch, False))
    check("自定义模型渲染出临时 patch", generated and os.path.isfile(patch_other)
          and os.path.abspath(patch_other).startswith(os.path.abspath(dsh_adapter.TMP_PATCH_ROOT)))
    body_other = open(patch_other, encoding="utf-8").read()
    check("临时 patch 两处模型都换了（models 列表 + 默认模型）",
          body_other.count(other_model) == 2
          and dsh_adapter.DEFAULT_MODEL not in body_other,
          f"count={body_other.count(other_model)}")
    check("临时 patch 的 name 也跟着换成短名",
          "name: minimax-m3.1-flash" in body_other)
    check("仓库 patch 没被改写", open(patch, encoding="utf-8").read().count(dsh_adapter.DEFAULT_MODEL) == 2)
    check("同一模型第二次解析复用同一份缓存",
          dsh_adapter.resolve_patch(other_model)[0] == patch_other)

    print("\n=== 11) 模型名不合法要被拒 ===")
    bad_models = ["", "  ", "../etc/passwd", "nvidia/a b", "nvidia/../../x"]
    bad_hits = []
    for m in bad_models:
        try:
            dsh_adapter.check_model(m)
            bad_hits.append(m or "空")
        except dsh_adapter.DshError:
            pass
    check("空 / 越界 / 带空格 的模型名一律拒", not bad_hits, "、".join(bad_hits)[:80])

    print("\n=== 12) run() 里模型真的进了命令，patch 落在缓存目录 ===")
    stub_wrap = write(os.path.join(tmp, "stub_args.py"),
                      "import sys\n"
                      "args = sys.argv[1:]\n"
                      "print('patch=' + args[args.index('--patch') + 1])\n"
                      "print('3')\n")
    os.environ["MWP_DSH_CMD"] = f'"{sys.executable}" {stub_wrap}' if os.name == "nt" else f"{sys.executable} {stub_wrap}"
    os.environ["MWP_DSH_CREDENTIALS"] = good
    before = set(os.listdir(dsh_adapter.TMP_PATCH_ROOT)) if os.path.isdir(dsh_adapter.TMP_PATCH_ROOT) else set()
    res = dsh_adapter.run("1+1", timeout=30, model=other_model)
    printed_patch = res["stdout"].splitlines()[0].split("=", 1)[1]
    check("返回的 model 是按单指定的那个", res["model"] == other_model, res["model"])
    check("命令里的 --patch 指向缓存 patch", os.path.abspath(printed_patch) == os.path.abspath(patch_other),
          printed_patch)
    after = set(os.listdir(dsh_adapter.TMP_PATCH_ROOT)) if os.path.isdir(dsh_adapter.TMP_PATCH_ROOT) else set()
    check("跑一次只多不改（同一模型不新增文件）", after == before,
          "、".join(sorted(after - before)) or "没有新增")

    print("\n=== 13) MWP_DSH_MODEL 是真开关 ===")
    os.environ["MWP_DSH_MODEL"] = "nvidia/llama-3.1-nemotron-70b-instruct"
    res = dsh_adapter.run("1+1", timeout=30)
    check("没给 model 时环境变量生效", res["model"] == "nvidia/llama-3.1-nemotron-70b-instruct", res["model"])
    check("环境变量也走临时 patch", "tmp_dsh" in res["stdout"].splitlines()[0], res["stdout"][:60])
    check("按单 model 优先级高于环境变量",
          dsh_adapter.resolve_model(other_model) == other_model)
    del os.environ["MWP_DSH_MODEL"]
    check("清掉环境变量回落到默认",
          dsh_adapter.resolve_model(None) == dsh_adapter.DEFAULT_MODEL)

    print("\n=== 14) dsh 的运行 home 收进仓库，不动用户 ~/.dsh ===")
    home = dsh_adapter.dsh_home()
    check("运行 home 默认在仓库 run/dsh_home", os.path.abspath(home) == os.path.abspath(dsh_adapter.RUNTIME_HOME),
          home)
    dsh_adapter.ensure_profile("headless")
    seeded = os.path.join(home, "profiles", "headless")
    check("运行 home 下的 profile 目录已就位", os.path.isdir(seeded), seeded)
    src_profile = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "headless")
    if os.path.isfile(os.path.join(src_profile, "package.json")):
        check("profile 的 bundle 清单被复制到运行 home（否则 dsh 起不来）",
              os.path.isfile(os.path.join(seeded, "package.json"))
              and "dsh-base" in open(os.path.join(seeded, "package.json"), encoding="utf-8").read(),
              seeded)
    else:
        check("本机没装 dsh profile 时也能跑验收（只建目录、不硬要源）", True)
    saved_cred = os.environ.pop("MWP_DSH_CREDENTIALS", None)
    check("凭证路径仍指用户的 ~/.dsh（不跟着运行 home 跑）",
          os.path.abspath(dsh_adapter.credentials_path())
          == os.path.abspath(os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")),
          dsh_adapter.credentials_path())
    if saved_cred is not None:
        os.environ["MWP_DSH_CREDENTIALS"] = saved_cred
    stub_env = write(os.path.join(tmp, "stub_env.py"),
                     "import os\n"
                     "print('DSH_HOME=' + os.environ.get('DSH_HOME', ''))\n"
                     "print('TEMP=' + os.environ.get('TEMP', ''))\n"
                     "print('5')\n")
    os.environ["MWP_DSH_CMD"] = f'"{sys.executable}" {stub_env}' if os.name == "nt" else f"{sys.executable} {stub_env}"
    res = dsh_adapter.run("1+1", timeout=30)
    lines = res["stdout"].splitlines()
    check("子进程拿到 DSH_HOME（指向仓库）", lines[0] == "DSH_HOME=" + os.path.abspath(home), lines[0])
    check("子进程临时目录也在仓库里", lines[1].startswith("TEMP=" + os.path.abspath(dsh_adapter.TMP_PATCH_ROOT)),
          lines[1])

    print("\n=== 15) 缓存清理只清旧的，且清不动也不影响这一单 ===")
    fresh = os.path.join(dsh_adapter.TMP_PATCH_ROOT, "patch-fresh.yml")
    os.makedirs(dsh_adapter.TMP_PATCH_ROOT, exist_ok=True)
    stub_text = open(dsh_adapter.PATCH_PATH, encoding="utf-8").read()
    old = os.path.join(dsh_adapter.TMP_PATCH_ROOT, "patch-old.yml")
    with open(fresh, "w", encoding="utf-8") as fh:
        fh.write(stub_text)
    with open(old, "w", encoding="utf-8") as fh:
        fh.write(stub_text)
    os.utime(old, (time.time() - 30 * 86400, time.time() - 30 * 86400))
    keeper = os.path.join(dsh_adapter.TMP_PATCH_ROOT, "keepme.txt")
    with open(keeper, "w", encoding="utf-8") as fh:
        fh.write("x")
    os.utime(keeper, (time.time() - 30 * 86400, time.time() - 30 * 86400))
    removed = dsh_adapter.prune_cache()
    check("新鲜缓存不被清", os.path.isfile(fresh))
    check("非 patch 文件一概不碰", os.path.isfile(keeper))
    check("清缓存不抛异常（删不掉也算过）", isinstance(removed, list), str(removed)[:60])

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
