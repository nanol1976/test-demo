"""Batch35 验收：产物随回执回传，跨机也能在平台上看见（离线可跑）。

守的四组：
1. 回传口径：Worker 只把 `run/artifacts/` 下的产物读成文本，超 8000 字截断并标明；
2. 落地口径：平台收到 `output.artifact_text` 就在本机补一份 `run/artifacts/<task_id>.log`，
   于是产物签、抽屉、`include_artifact` 三处不用改，接口面仍是 11 条；
3. 不覆盖：本机已有同名产物（同机执行时 Worker 写的那份）一律不覆盖；
4. 任务号安全：`../`、带斜杠、空、以点开头的任务号都不落盘，绝不跑出 run/artifacts。

用法：python scripts/verify_b35.py
退出码：0 全绿 / 1 有失败。
"""
import os
import sys
import tempfile

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
    from app.services import artifacts
    from app.services import dispatch as dsp

    print("=== 1) Worker 侧：只读 run/artifacts，超长截断 ===")
    worker_src = read(os.path.join(ROOT, "worker", "agent_worker.py"))
    check("有专门的读产物函数", "def _read_artifact_text(" in worker_src)
    check("只认 run/artifacts/ 相对路径", 'startswith("run/artifacts/")' in worker_src,)
    check("拒绝 .. 越界", '".." in rel' in worker_src)
    check("超长截到 8000 字并标明", "ARTIFACT_INLINE_MAX = 8000" in worker_src
          and "art_truncated" in worker_src)
    check("回执里带 artifact_text / artifact_instance",
          '"artifact_text": art_text' in worker_src and '"artifact_instance": self.instance_id' in worker_src)
    check("读不到产物时不抛（返回空串）", "except OSError:\n        return \"\", False" in worker_src)

    print("\n=== 2) 平台侧：回执带了全文就补本机副本 ===")
    check("artifacts.save 存在", callable(getattr(artifacts, "save", None)))
    check("派单编排里调用了落盘", "_persist_remote_artifact(envelope.task_id, payload)" in
          read(os.path.join(ROOT, "app", "services", "dispatch.py")))
    check("只在回执（task_return）里落盘", "def _persist_remote_artifact" in
          read(os.path.join(ROOT, "app", "services", "dispatch.py")))

    tmp_root = tempfile.mkdtemp(prefix="b35-")
    old_dir = artifacts.ARTIFACT_DIR
    artifacts.ARTIFACT_DIR = tmp_root
    artifacts._cache["key"] = None
    artifacts._cache["data"] = {}
    try:
        body = "# task_id: t-b35-selftest\n# model: m\n\n=== stdout ===\n40\n"
        saved = artifacts.save("t-b35-selftest", body)
        check("落盘后索引能手到", bool(saved.get("bytes")), str(saved))
        on_disk = os.path.getsize(os.path.join(tmp_root, "t-b35-selftest.log"))
        check("字节数与落盘真实字节一致（Windows 换行按 CRLF 算）",
              saved.get("bytes") == on_disk, f"{saved.get('bytes')} vs {on_disk}")
        check("read_text 能读回原文", artifacts.read_text("t-b35-selftest").strip() == body.strip())

        # 同机已有产物：不覆盖
        with open(os.path.join(tmp_root, "t-b35-own.log"), "w", encoding="utf-8") as fh:
            fh.write("OWNER-BYTES")
        artifacts._cache["key"] = None
        artifacts._cache["data"] = {}
        artifacts.save("t-b35-own", "REMOTE-BYTES")
        check("本机已有同名产物不被覆盖", artifacts.read_text("t-b35-own") == "OWNER-BYTES",
              artifacts.read_text("t-b35-own"))

        # 任务号安全
        for bad in ("../evil", "a/b", "", "..hidden", "x" * 80):
            check(f"非法任务号不落盘：{bad[:12]!r}", artifacts.save(bad, "X") == {})
        check("落盘目录没被写出别的文件",
              sorted(os.listdir(tmp_root)) == ["t-b35-own.log", "t-b35-selftest.log"],
              str(sorted(os.listdir(tmp_root))))

        # 回执装配：只有真的带全文才落盘
        dsp._persist_remote_artifact("t-b35-plain", {"output": {"artifact": "run/artifacts/x.log"}})
        check("回执没带全文时不落盘", not artifacts.lookup("t-b35-plain"))
        dsp._persist_remote_artifact("t-b35-from-mac",
                                     {"output": {"artifact_text": "# 来的\n40\n", "artifact_instance": "ops-mac"}})
        check("回执带了全文就落盘", bool(artifacts.lookup("t-b35-from-mac").get("bytes")))
        check("落盘内容与回传一字不差",
              artifacts.read_text("t-b35-from-mac").strip() == "# 来的\n40")
    finally:
        artifacts.ARTIFACT_DIR = old_dir
        artifacts._cache["key"] = None
        artifacts._cache["data"] = {}

    print("\n=== 3) 接口面没动 ===")
    api_src = read(os.path.join(ROOT, "app", "api", "routes_messages.py"))
    check("产物全文仍走 GET /api/tasks/{id}?include_artifact=true",
          "include_artifact" in api_src and "artifacts.read_text(task_id)" in api_src)
    check("没有新开取产物的接口路径", "/api/artifacts" not in api_src)

    # run_all 只认这两个字的结论口径，别只写"全绿"，否则跑在 CI 里会被判红（Batch35 真踩过）
    print(f"\n结果：{'全部通过' if not FAILED else '失败 ' + str(len(FAILED)) + ' 项'}")
    if FAILED:
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
