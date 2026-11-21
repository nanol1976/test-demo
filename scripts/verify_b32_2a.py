#!/usr/bin/env python3
"""B32-2 只读侧验收：plugins 目录规范 + plugin_review 校验器。

全部在临时目录里造树、破坏、复验，不写真实仓库（除第一次 --all 只读跑）。
覆盖：必填字段、目录名一致、semver、CHANGELOG 对齐、SKILL 空壳、criteria 条数、
probes 可跑、索引登记/版本递增/latest 口径/状态值域/授权字段互斥、--apply 幂等与 revoked 不复位。

用法：python scripts/verify_b32_2a.py   退出码 0 全绿。
纯标准库 + 复用 scripts/plugin_review.py 的纯函数。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
REVIEW = os.path.join(HERE, "plugin_review.py")

PASS = 0
FAIL = 0


def ok(label, detail=""):
    global PASS
    PASS += 1
    print("[PASS] " + label + ((" — " + detail) if detail else ""))


def bad(label, detail=""):
    global FAIL
    FAIL += 1
    print("[FAIL] " + label + ((" — " + detail) if detail else ""))


def expect(cond, label, detail=""):
    ok(label, detail) if cond else bad(label, detail)


def run(root, *args):
    proc = subprocess.run([PY, REVIEW] + list(args) + ["--root", root],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def fresh_tree(tmp):
    """把真实 plugins/ 拷到临时根（每次一个独立子目录），返回临时根。"""
    root = tempfile.mkdtemp(prefix="case_", dir=tmp)
    shutil.copytree(os.path.join(ROOT, "plugins"), os.path.join(root, "plugins"))
    return root


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def mutate_index(root, fn):
    path = os.path.join(root, "plugins", "index.json")
    data = read_json(path)
    fn(data)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def entry_of(data, name="deploy-smoke"):
    return next(p for p in data["plugins"] if p["name"] == name)


def main():
    if not os.path.isdir(os.path.join(ROOT, "plugins")):
        bad("仓库内 plugins/ 目录在位")
        print("验收结果：失败")
        return 1
    ok("仓库内 plugins/ 目录在位")

    # 0) 真仓库只读跑通
    rc, out = run(ROOT, "--all")
    expect(rc == 0, "真仓库 plugins 全部通过校验", "rc=%d" % rc)

    idx = read_json(os.path.join(ROOT, "plugins", "index.json"))
    e = entry_of(idx)
    expect(all(k in e for k in ("name", "latest", "versions", "review_state",
                                "granted_to", "granted_roles", "revoked_instances")),
           "index.json 五个结构字段齐全（latest/versions/state/授权/撤权）")

    # 1) 真插件四件套在位
    pdir = os.path.join(ROOT, "plugins", "deploy-smoke")
    expect(all(os.path.exists(os.path.join(pdir, f))
               for f in ("PLUGIN.md", "SKILL.md", "CHANGELOG.md", "probes.py")),
           "插件四件套齐全（PLUGIN/SKILL/CHANGELOG/probes）")

    # 2) 探针真能判：给对版本 pass，给错版本 fail（版本号从迁移目录现算，不写死）
    import re as _re
    tops = [int(m.group(1)) for m in (
        _re.match(r"^v(\d+)", fn) for fn in os.listdir(os.path.join(ROOT, "migrations", "versions")))
        if m]
    target = max(tops)
    proc = subprocess.run([PY, os.path.join(pdir, "probes.py"), ROOT, "--schema", str(target)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)
    payload = json.loads((proc.stdout or "{}").strip().splitlines()[-1])
    expect(proc.returncode == 0 and payload["verdict"] == "pass",
           "探针在正确版本上判 pass", f"schema={target}，3 条 items 全 ok")
    proc2 = subprocess.run([PY, os.path.join(pdir, "probes.py"), ROOT, "--schema", "9"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    p2 = json.loads((proc2.stdout or "{}").strip().splitlines()[-1])
    expect(proc2.returncode == 1 and p2["verdict"] == "fail"
           and any(i["id"] == "c2" and not i["ok"] for i in p2["items"]),
           "探针在错误版本上判 fail 且点出 c2")

    tmp = tempfile.mkdtemp(prefix="b32_2a_")
    try:
        # 3) 缺必填字段
        r1 = fresh_tree(tmp)
        write(os.path.join(r1, "plugins", "deploy-smoke", "PLUGIN.md"),
              "name: deploy-smoke\nversion: 1.0.0\n")
        rc, out = run(r1, "--all", "--no-probes")
        expect(rc == 1 and "缺必填字段" in out, "缺必填字段被拦", "只留 name/version")

        # 4) 目录名与 name 不一致
        r2 = fresh_tree(tmp)
        write(os.path.join(r2, "plugins", "deploy-smoke", "PLUGIN.md"),
              "name: deploy-smoke-x\nversion: 1.0.0\nowner: a\napplies_to: 运维\n"
              "inputs: a\noutputs: b\ncriteria_template: 一；二\nreviewer_hint: 质检\n")
        rc, out = run(r2, "--all", "--no-probes")
        expect(rc == 1 and "与目录名" in out, "name 与目录名不一致被拦")

        # 5) 非 semver
        r3 = fresh_tree(tmp)
        write(os.path.join(r3, "plugins", "deploy-smoke", "PLUGIN.md"),
              "name: deploy-smoke\nversion: 1.0\nowner: a\napplies_to: 运维\n"
              "inputs: a\noutputs: b\ncriteria_template: 一；二\nreviewer_hint: 质检\n")
        rc, out = run(r3, "--all", "--no-probes")
        expect(rc == 1 and "semver" in out, "version 非 semver 被拦")

        # 6) CHANGELOG 没有当前版本条目
        r4 = fresh_tree(tmp)
        write(os.path.join(r4, "plugins", "deploy-smoke", "CHANGELOG.md"),
              "# CHANGELOG\n\n## 0.9.0\n\n- 旧版\n")
        rc, out = run(r4, "--all", "--no-probes")
        expect(rc == 1 and "CHANGELOG" in out, "CHANGELOG 缺当前版本条目被拦")

        # 7) SKILL 空壳
        r5 = fresh_tree(tmp)
        write(os.path.join(r5, "plugins", "deploy-smoke", "SKILL.md"), "空\n")
        rc, out = run(r5, "--all", "--no-probes")
        expect(rc == 1 and "空壳" in out, "SKILL.md 空壳被拦")

        # 8) criteria 只有一条
        r6 = fresh_tree(tmp)
        write(os.path.join(r6, "plugins", "deploy-smoke", "PLUGIN.md"),
              "name: deploy-smoke\nversion: 1.0.0\nowner: a\napplies_to: 运维\n"
              "inputs: a\noutputs: b\ncriteria_template: 只有一条\nreviewer_hint: 质检\n")
        rc, out = run(r6, "--all", "--no-probes")
        expect(rc == 1 and "至少要有 2 条" in out, "criteria_template 少于 2 条被拦")

        # 9) probes 跑起来就炸
        r7 = fresh_tree(tmp)
        write(os.path.join(r7, "plugins", "deploy-smoke", "probes.py"),
              "import sys\nsys.exit(3)\n")
        rc, out = run(r7, "--all")
        expect(rc == 1 and "probes.py 跑失败" in out, "probes.py 退出码非 0 被拦")

        # 10) index 未登记
        r8 = fresh_tree(tmp)
        mutate_index(r8, lambda d: d["plugins"].clear())
        rc, out = run(r8, "--all", "--no-probes")
        expect(rc == 1 and "未登记" in out, "index 里没登记被拦")

        # 11) 版本回退（当前版本低于 index 里已登记的最高版本）
        r9 = fresh_tree(tmp)
        def _rollback(d):
            en = entry_of(d)
            en["versions"] = ["1.0.0", "1.1.0"]
            en["latest"] = "1.1.0"
        mutate_index(r9, _rollback)
        rc, out = run(r9, "--all", "--no-probes")
        expect(rc == 1 and "版本回退" in out, "版本回退被拦（1.0.0 低于已登记的 1.1.0）")

        # 12) latest 指旧版本
        r10 = fresh_tree(tmp)
        def _stale(d):
            en = entry_of(d)
            en["versions"] = ["0.9.0", "1.0.0"]
            en["latest"] = "0.9.0"
        mutate_index(r10, _stale)
        rc, out = run(r10, "--all", "--no-probes")
        expect(rc == 1 and "不是 versions 里的最高版本" in out, "latest 不是最高版本被拦")

        # 13) review_state 非法
        r11 = fresh_tree(tmp)
        mutate_index(r11, lambda d: entry_of(d).update({"review_state": "approved"}))
        rc, out = run(r11, "--all", "--no-probes")
        expect(rc == 1 and "review_state 只能是" in out, "review_state 非法值被拦")

        # 14) 授权与撤权冲突
        r12 = fresh_tree(tmp)
        mutate_index(r12, lambda d: entry_of(d).update(
            {"granted_to": ["ops-win"], "revoked_instances": ["ops-win"]}))
        rc, out = run(r12, "--all", "--no-probes")
        expect(rc == 1 and "既在 granted_to 又在 revoked_instances" in out,
               "同一实例既授权又撤权被拦")

        # 14b) 岗位授权只收代号（与看板前缀同一口径）
        r12b = fresh_tree(tmp)
        mutate_index(r12b, lambda d: entry_of(d).update({"granted_roles": ["运维"]}))
        rc, out = run(r12b, "--all", "--no-probes")
        expect(rc == 1 and "只收岗位代号" in out, "granted_roles 写中文岗位名被拦")

        # 15) --apply：draft → reviewed，且幂等
        r13 = fresh_tree(tmp)
        rc, out = run(r13, "--all", "--no-probes", "--apply")
        state1 = entry_of(read_json(os.path.join(r13, "plugins", "index.json")))
        expect(rc == 0 and state1["review_state"] == "reviewed",
               "--apply 把 draft 推进到 reviewed")
        rc, out = run(r13, "--all", "--no-probes", "--apply")
        state2 = entry_of(read_json(os.path.join(r13, "plugins", "index.json")))
        expect(rc == 0 and state2["versions"] == ["1.0.0"] and state2["latest"] == "1.0.0",
               "--apply 幂等（versions 不重复追加）", str(state2["versions"]))

        # 16) revoked 不被 --apply 复位
        r14 = fresh_tree(tmp)
        mutate_index(r14, lambda d: entry_of(d).update({"review_state": "revoked"}))
        rc, out = run(r14, "--all", "--no-probes", "--apply")
        expect(rc == 0 and entry_of(read_json(os.path.join(r14, "plugins", "index.json"))
                                    )["review_state"] == "revoked",
               "--apply 不把 revoked 复位成 reviewed")

        # 17) 版本递增：新版本可在审（draft），apply 登记；reviewed 状态不许偷偷升版本
        r15 = fresh_tree(tmp)
        write(os.path.join(r15, "plugins", "deploy-smoke", "PLUGIN.md"),
              "name: deploy-smoke\nversion: 1.1.0\nowner: a\napplies_to: 运维\n"
              "inputs: a\noutputs: b\ncriteria_template: 一；二\nreviewer_hint: 质检\n")
        write(os.path.join(r15, "plugins", "deploy-smoke", "CHANGELOG.md"),
              "# CHANGELOG\n\n## 1.1.0\n\n- 加了第四条\n")
        mutate_index(r15, lambda d: entry_of(d).update({"review_state": "draft"}))
        rc, out = run(r15, "--all", "--no-probes")
        expect(rc == 0, "新版本 1.1.0 未登记但处在 draft，允许在审", "rc=%d" % rc)
        rc, out = run(r15, "--all", "--no-probes", "--apply")
        en = entry_of(read_json(os.path.join(r15, "plugins", "index.json")))
        expect(rc == 0 and en["latest"] == "1.1.0" and en["versions"] == ["1.0.0", "1.1.0"],
               "--apply 登记新版本且保留旧版本", str(en["versions"]))

        r16 = fresh_tree(tmp)
        write(os.path.join(r16, "plugins", "deploy-smoke", "PLUGIN.md"),
              "name: deploy-smoke\nversion: 1.1.0\nowner: a\napplies_to: 运维\n"
              "inputs: a\noutputs: b\ncriteria_template: 一；二\nreviewer_hint: 质检\n")
        write(os.path.join(r16, "plugins", "deploy-smoke", "CHANGELOG.md"),
              "# CHANGELOG\n\n## 1.1.0\n\n- 加了第四条\n")
        mutate_index(r16, lambda d: entry_of(d).update({"review_state": "reviewed"}))
        rc, out = run(r16, "--all", "--no-probes", "--apply")
        expect(rc == 1 and "必须是 draft" in out,
               "reviewed 状态下偷偷升版本被拦（新版本要重新审核）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 18) 输入不合法：rc=2
    tmp2 = tempfile.mkdtemp(prefix="b32_2a_empty_")
    try:
        rc, out = run(tmp2, "--all")
        expect(rc == 2, "没有 plugins/ 目录按输入不合法处理（rc=2）", "rc=%d" % rc)
        rc, out = run(ROOT, "--plugin", "no-such-plugin")
        expect(rc == 2, "不存在的插件名按输入不合法处理（rc=2）", "rc=%d" % rc)
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    # 19) --json 可机读
    rc, out = run(ROOT, "--all", "--json")
    parsed = {}
    try:
        parsed = json.loads([ln for ln in out.splitlines() if ln.strip().startswith("{")][-1])
    except (IndexError, ValueError):
        pass
    expect(parsed.get("ok") is True and len(parsed.get("results", [])) == 1,
           "--json 输出单行 JSON 且可解析")

    # 20) 真仓库 index 没被上面任何一步改脏
    idx2 = read_json(os.path.join(ROOT, "plugins", "index.json"))
    expect(idx2["plugins"][0]["versions"] == ["1.0.0"],
           "验收全程没写脏真仓库 index.json")

    print()
    if FAIL:
        print("验收结果：失败（PASS=%d FAIL=%d）" % (PASS, FAIL))
        return 1
    print("验收结果：全部通过（%d 项）" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
