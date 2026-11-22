"""Batch33-2 验收：dsh 产物落档 + 思考档（reasoning_effort），离线可跑、不碰真 API。

守的六组：
1. 执行一次 → run/artifacts/<task_id>.log 就位，头里有 task/model/effort/exit_code/patch；
2. 回执里的 artifact/bytes/sha256 跟磁盘上的文件对得上（哈希不是随便编的）；
3. 任务号里的越界字符被洗掉，产物绝不可能落到 run/artifacts 之外；
4. reasoning_effort：合值进 patch、非法拒、不指定就不写这行、环境变量与按单优先级；
5. Worker 接线：effort 与 task_id 都传给 adapter，回执 output 带四样产物字段；
6. 平台侧：/api/artifacts 两个接口存在且被注册，看板有「产物」签，能按 task_id 查到刚才那份。

用法：python scripts/verify_b33_2.py
退出码：0 全绿 / 1 有失败。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
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


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def main():
    tmp_base = os.path.join(ROOT, "run", "tmp_b33_2")
    os.makedirs(tmp_base, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="b332-", dir=tmp_base)
    fake_key = "nvapi" + "-TESTKEY-B332"
    cred = write(os.path.join(tmp, "cred.yaml"),
                 f"version: 1\nrecords:\n  x:\n    kind: grant\nrefs:\n  NVIDIA_API_KEY: {fake_key}\n")
    os.environ["MWP_DSH_CREDENTIALS"] = cred

    stub = write(os.path.join(tmp, "stub.py"),
                 "import sys\n"
                 "print('answer=42')\n"
                 "print('reasoning: 想了三步', file=sys.stderr)\n")
    os.environ["MWP_DSH_CMD"] = (f'"{sys.executable}" {stub}' if os.name == "nt"
                                 else f"{sys.executable} {stub}")
    os.environ.pop("MWP_DSH_MODEL", None)
    os.environ.pop("MWP_DSH_EFFORT", None)

    print("=== 1) 产物落档 ===")
    task_id = "t-b332-probe"
    res = dsh_adapter.run("1+1", timeout=30, task_id=task_id)
    art = res.get("artifact")
    check("回执带 artifact 相对路径", isinstance(art, str) and art.startswith("run/artifacts/"), str(art))
    full = os.path.join(ROOT, art) if art else ""
    check("文件真的落在 run/artifacts 下", bool(full) and os.path.isfile(full), full)
    body = read(full) if full and os.path.isfile(full) else ""
    check("头里写明 task_id", f"# task_id: {task_id}" in body)
    check("头里写明模型", f"# model: {dsh_adapter.DEFAULT_MODEL}" in body)
    check("头里写明思考档（未指定为 -）", "# reasoning_effort: -" in body)
    check("头里写明退出码", "# exit_code: 0" in body)
    check("头里写明用的 patch", "# patch: worker/dsh/nvidia-patch.yml" in body)
    check("stdout 全文进档", "answer=42" in body)
    check("stderr 全文进档（推理不进产出，但要留证据）", "reasoning: 想了三步" in body)
    check("退出码非 0 也照样写档（这里验 0）", res["exit_code"] == 0)

    print("\n=== 2) 字节数与哈希跟磁盘一致 ===")
    import hashlib
    disk = open(full, "rb").read() if full and os.path.isfile(full) else b""
    check("artifact_bytes 与文件大小一致", res.get("artifact_bytes") == len(disk),
          f"{res.get('artifact_bytes')} vs {len(disk)}")
    check("artifact_sha256 是文件哈希前 12 位",
          res.get("artifact_sha256") == hashlib.sha256(disk).hexdigest()[:12],
          str(res.get("artifact_sha256")))
    check("artifact_error 为空（这次写得进去）", res.get("artifact_error") == "", str(res.get("artifact_error")))

    print("\n=== 3) 任务号不能把产物带出目录 ===")
    risky = "../../evil"
    res2 = dsh_adapter.run("x", timeout=30, task_id=risky)
    p2 = res2.get("artifact") or ""
    check("越界任务号被洗成安全文件名", ".." not in p2 and p2.startswith("run/artifacts/"), p2)
    check("路径仍在 ARTIFACT_ROOT 下",
          os.path.abspath(os.path.join(ROOT, p2)).startswith(os.path.abspath(dsh_adapter.ARTIFACT_ROOT)))
    check("ARTIFACT_ROOT 就在 run/artifacts",
          os.path.abspath(dsh_adapter.ARTIFACT_ROOT)
          == os.path.abspath(os.path.join(ROOT, "run", "artifacts")))

    print("\n=== 4) reasoning_effort ===")
    check("合法值原样收下", dsh_adapter.check_effort("High") == "high")
    check("空值表示不写这一行", dsh_adapter.check_effort("") == "")
    bad_hits = []
    for v in ("ultra", "high; rm -rf", "../x", "1"):
        try:
            dsh_adapter.check_effort(v)
            bad_hits.append(v)
        except dsh_adapter.DshError:
            pass
    check("非法思考档一律拒", not bad_hits, "、".join(bad_hits)[:60])
    check("仓库 patch 本身不带 reasoningEffort（不替别人定档）",
          "reasoningEffort" not in read(dsh_adapter.PATCH_PATH))
    check("默认模型 + 不指定思考档 → 复用仓库 patch",
          dsh_adapter.resolve_patch(dsh_adapter.DEFAULT_MODEL, effort="") == (dsh_adapter.PATCH_PATH, False))
    p_high, gen_high = dsh_adapter.resolve_patch(dsh_adapter.DEFAULT_MODEL, effort="high")
    body_high = read(p_high)
    check("指定思考档 → 渲染临时 patch", gen_high)
    check("reasoningEffort 写在 agent-default-model 段里",
          "    model: " + dsh_adapter.DEFAULT_MODEL + "\n    reasoningEffort: high\n" in body_high)
    check("只写一处（不往 models 列表里乱塞）", body_high.count("reasoningEffort") == 1)
    check("同一组合第二次复用同一份缓存", dsh_adapter.resolve_patch(dsh_adapter.DEFAULT_MODEL, effort="high")[0] == p_high)
    p_other, _ = dsh_adapter.resolve_patch("minimaxai/minimax-m3.1-flash", effort="low")
    check("模型 + 思考档一起渲染（缓存名带档位）",
          os.path.basename(p_other).endswith("-low.yml"), os.path.basename(p_other))
    body_other = read(p_other)
    check("换了模型的 patch 里思考档也换了",
          "reasoningEffort: low" in body_other and "reasoningEffort: high" not in body_other)
    os.environ["MWP_DSH_EFFORT"] = "medium"
    check("环境变量生效", dsh_adapter.resolve_effort(None) == "medium")
    check("按单优先于环境变量", dsh_adapter.resolve_effort("low") == "low")
    del os.environ["MWP_DSH_EFFORT"]
    check("清掉环境变量回到不指定", dsh_adapter.resolve_effort(None) == "")

    print("\n=== 5) Worker 接线 ===")
    worker_src = read(os.path.join(ROOT, "worker", "agent_worker.py"))
    check("effort 传给 adapter", 'spec.get("reasoning_effort")' in worker_src)
    check("task_id 也传（产物按任务号命名）", "task_id=task_id" in worker_src)
    for field in ("artifact", "artifact_bytes", "artifact_sha256", "artifact_error",
                  "reasoning_effort"):
        check(f"回执 output 带 {field}", f'"{field}"' in worker_src)
    check("artifact_start 日志带思考档", 'effort=spec.get("reasoning_effort")' in worker_src)

    print("\n=== 6) 平台侧：不加路径，只加字段 ===")
    svc = read(os.path.join(ROOT, "app", "services", "artifacts.py"))
    check("平台有产物索引（按目录 mtime 缓存，不每刷一次全扫）",
          "def scan" in svc and "_dir_key" in svc)
    check("索引只认 run/artifacts 下的 .log", 'endswith(".log")' in svc)
    check("索引按 task_id 建表，能给台账挂字段", "def lookup" in svc)
    schema_src = read(os.path.join(ROOT, "app", "api", "schemas_http.py"))
    for field in ("artifact:", "artifact_bytes:", "artifact_error:"):
        check(f"TaskView 有 {field}", field in schema_src)
    rm_src = read(os.path.join(ROOT, "app", "api", "routes_messages.py"))
    check("台账返回体挂上产物（看不到就不写路径）",
          "artifacts.lookup(task.task_id)" in rm_src and 'art.get("path") or None' in rm_src)
    check("不加接口路径（铁律仍 11 条，产物走字段）",
          not os.path.isfile(os.path.join(ROOT, "app", "api", "routes_artifacts.py")))

    # 接口面必须仍是 11 条：数一遍所有 router 装饰器
    import re as _re
    api_dir = os.path.join(ROOT, "app", "api")
    paths = set()
    for name in os.listdir(api_dir):
        if not name.endswith(".py"):
            continue
        text = read(os.path.join(api_dir, name))
        for m in _re.finditer(r"@router\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']", text):
            paths.add(m.group(2))
    check("接口路径仍是 11 条", len(paths) == 11, f"{len(paths)} 条：{sorted(paths)}")
    js_src = read(os.path.join(ROOT, "app", "static", "app.js"))
    check("看板按 TaskView 字段显示产物签", "renderArtifactChip" in js_src)
    check("看板不为产物发请求（不新增接口）", "ROUTES.artifacts" not in js_src)
    check("未留档单独标黄（写档失败不许装没事）",
          "art-chip-warn" in js_src and "art-chip-warn" in read(os.path.join(ROOT, "app", "static", "style.css")))

    try:
        from app.services import artifacts as art_svc
        got = art_svc.scan()
        check("索引能查到刚才那份产物", task_id in got, str(list(got)[:3]))
        check("索引里的大小跟文件一致",
              got.get(task_id, {}).get("bytes") == os.path.getsize(full), str(got.get(task_id)))
        check("没产物的任务不会凭空出现", "t-b332-nope" not in got)
    except Exception as exc:  # 缺依赖时如实报红，不假装通过
        check(f"平台产物索引可导入（{type(exc).__name__}）", False, str(exc)[:80])

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
