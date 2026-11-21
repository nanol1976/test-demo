#!/usr/bin/env python3
"""插件沉淀校验器（Batch32-2 只读侧）。

校验 plugins/<name>/ 的元数据、版本递增、CHANGELOG、SKILL.md、probes.py 可跑，
并把 plugins/index.json 的 review_state 在 --apply 时从 draft 推进到 reviewed。

用法：
    python scripts/plugin_review.py --all                 # 校验全部插件（只报，不改）
    python scripts/plugin_review.py --plugin deploy-smoke
    python scripts/plugin_review.py --all --apply         # 通过后写回 index.json
    python scripts/plugin_review.py --root <临时目录> --all   # 校验别处的插件树（验收用）
    python scripts/plugin_review.py --all --json          # stdout 单行 JSON，给机器吃

退出码：0 全过 / 1 有问题 / 2 输入不合法（插件或索引不存在）。
纯标准库，不引第三方依赖。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date

REQUIRED = ("name", "version", "owner", "applies_to", "inputs",
            "outputs", "criteria_template", "reviewer_hint")
STATES = ("draft", "reviewed", "revoked")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
MIN_SKILL_BYTES = 100
PROBE_TIMEOUT = 30


# ---------- 基础件 ----------

def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def semver_key(text):
    m = SEMVER.match(text or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def parse_plugin_md(text):
    """PLUGIN.md 只认 `key: value` 单层结构，值可以是逗号分隔的列表。"""
    meta = {}
    for line in (text or "").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key:
            meta[key] = value.strip()
    return meta


def split_list(value):
    parts = [p.strip() for p in re.split(r"[,，]", value or "") if p.strip()]
    return parts


def split_criteria(value):
    parts = [p.strip() for p in re.split(r"[;；]", value or "") if p.strip()]
    return parts


def load_index(root):
    path = os.path.join(root, "plugins", "index.json")
    if not os.path.exists(path):
        return None, "plugins/index.json 不存在"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return None, "plugins/index.json 解析失败：%s" % exc
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        return None, "plugins/index.json 结构不对（需要顶层 plugins 数组）"
    return data, ""


def save_index(root, data):
    path = os.path.join(root, "plugins", "index.json")
    data["updated_at"] = date.today().isoformat()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------- 单个插件 ----------

def review_plugin(root, name, index=None, run_probes=True):
    """返回 {name, ok, errors, warnings, meta, state}。不写盘。"""
    errors = []
    warnings = []
    plugin_dir = os.path.join(root, "plugins", name)
    result = {"name": name, "ok": False, "errors": errors, "warnings": warnings,
              "meta": {}, "state": ""}

    if not os.path.isdir(plugin_dir):
        errors.append("目录不存在：plugins/%s" % name)
        return result

    meta = parse_plugin_md(read_text(os.path.join(plugin_dir, "PLUGIN.md")))
    result["meta"] = meta

    missing = [k for k in REQUIRED if not meta.get(k)]
    if missing:
        errors.append("PLUGIN.md 缺必填字段：%s" % "、".join(missing))

    if meta.get("name") and meta["name"] != name:
        errors.append("PLUGIN.md 的 name（%s）与目录名（%s）不一致" % (meta["name"], name))

    version = meta.get("version", "")
    if version and not SEMVER.match(version):
        errors.append("version 不是三段 semver：%s" % version)

    criteria = split_criteria(meta.get("criteria_template", ""))
    if meta.get("criteria_template") and len(criteria) < 2:
        errors.append("criteria_template 至少要有 2 条（当前 %d 条）" % len(criteria))

    for field in ("inputs", "outputs"):
        if meta.get(field) and not split_list(meta[field]):
            errors.append("%s 声明了但解析不出条目" % field)

    changelog = read_text(os.path.join(plugin_dir, "CHANGELOG.md"))
    if not changelog.strip():
        errors.append("CHANGELOG.md 为空")
    elif version and ("## %s" % version) not in changelog:
        errors.append("CHANGELOG.md 里没有当前版本条目 ## %s" % version)

    skill = read_text(os.path.join(plugin_dir, "SKILL.md"))
    if len(skill) < MIN_SKILL_BYTES:
        errors.append("SKILL.md 过于空壳（%d 字节，至少要 %d）" % (len(skill), MIN_SKILL_BYTES))

    probes_path = os.path.join(plugin_dir, "probes.py")
    if os.path.exists(probes_path) and run_probes:
        try:
            proc = subprocess.run(
                [sys.executable, probes_path],
                cwd=root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT,
            )
            if proc.returncode != 0:
                tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
                errors.append("probes.py 跑失败（退出码 %d）%s"
                              % (proc.returncode,
                                 ("：" + tail[-1][:120]) if tail else ""))
        except subprocess.TimeoutExpired:
            errors.append("probes.py 超时（> %ds）" % PROBE_TIMEOUT)
        except OSError as exc:
            errors.append("probes.py 起不来：%s" % exc)

    if index is not None:
        entry = next((p for p in index.get("plugins", [])
                      if isinstance(p, dict) and p.get("name") == name), None)
        if entry is None:
            errors.append("index.json 里没有这个插件（未登记）")
        else:
            versions = entry.get("versions") or []
            for v in versions:
                if not SEMVER.match(str(v)):
                    errors.append("index.json versions 含非法版本：%s" % v)
            latest = str(entry.get("latest") or "")
            keys = [semver_key(str(v)) for v in versions if semver_key(str(v))]
            if latest and not SEMVER.match(latest):
                errors.append("index.json latest 不是 semver：%s" % latest)
            if keys and latest and semver_key(latest) != max(keys):
                errors.append("index.json latest（%s）不是 versions 里的最高版本（%s）"
                              % (latest, ".".join(str(x) for x in max(keys))))
            state = str(entry.get("review_state") or "")
            result["state"] = state
            if state not in STATES:
                errors.append("review_state 只能是 %s（当前 %s）" % ("/".join(STATES), state or "空"))
            # 版本口径：已登记 = 正常；没登记但比最新大 = 新版本在审（必须 draft）；
            # 没登记且比最新小 = 版本回退，拦。
            if version and SEMVER.match(version):
                cur = semver_key(version)
                lat = semver_key(latest)
                if lat and cur < lat:
                    errors.append("版本回退：当前 %s 低于 index 里登记的最新 %s"
                                  % (version, latest))
                elif version in [str(v) for v in versions]:
                    pass
                elif keys and cur < max(keys):
                    errors.append("版本回退：当前 %s 低于 index 里已登记的 %s"
                                  % (version, ".".join(str(x) for x in max(keys))))
                elif state != "draft":
                    errors.append("新版本 %s 还没登记，review_state 必须是 draft（新版本要重新走审核），"
                                  "当前是 %s" % (version, state or "空"))
            granted = entry.get("granted_to") or []
            revoked = entry.get("revoked_instances") or []
            roles = entry.get("granted_roles") or []
            for fname, value in (("granted_to", granted), ("granted_roles", roles),
                                 ("revoked_instances", revoked)):
                if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
                    errors.append("index.json %s 必须是字符串数组" % fname)
            bad_roles = [r for r in roles if isinstance(r, str) and not re.match(r"^[a-z][a-z0-9]*$", r)]
            if bad_roles:
                errors.append("index.json granted_roles 只收岗位代号（小写字母开头，如 ops/qa/dev），"
                              "不收中文岗位名：%s" % "、".join(bad_roles))
            overlap = set(x for x in granted if isinstance(x, str)) & \
                      set(x for x in revoked if isinstance(x, str))
            if overlap:
                errors.append("同一实例既在 granted_to 又在 revoked_instances：%s"
                              % "、".join(sorted(overlap)))

    result["ok"] = not errors
    return result


# ---------- 批量与写回 ----------

def list_plugin_names(root):
    pdir = os.path.join(root, "plugins")
    if not os.path.isdir(pdir):
        return []
    return sorted(d for d in os.listdir(pdir)
                  if os.path.isdir(os.path.join(pdir, d)) and not d.startswith("."))


def apply_to_index(root, results):
    """只把通过校验、且当前状态为 draft 的插件推进到 reviewed。返回改动清单。"""
    index, err = load_index(root)
    if index is None:
        return [], [err]
    changed = []
    for res in results:
        if not res["ok"]:
            continue
        entry = next((p for p in index["plugins"] if p.get("name") == res["name"]), None)
        if entry is None:
            continue
        version = res["meta"].get("version", "")
        versions = [str(v) for v in (entry.get("versions") or [])]
        if version and version not in versions:
            versions.append(version)
        versions.sort(key=lambda v: semver_key(v) or (0, 0, 0))
        key_before = (entry.get("latest"), entry.get("versions"), entry.get("review_state"))
        entry["versions"] = versions
        if version:
            entry["latest"] = version
        if entry.get("review_state") == "draft":
            entry["review_state"] = "reviewed"
        for fname in ("granted_to", "granted_roles", "revoked_instances"):
            entry.setdefault(fname, [])
        key_after = (entry.get("latest"), entry.get("versions"), entry.get("review_state"))
        if key_before != key_after:
            changed.append(res["name"])
    if changed:
        save_index(root, index)
    return changed, []


def print_report(results, as_json=False):
    if as_json:
        print(json.dumps({"results": results,
                          "ok": all(r["ok"] for r in results)}, ensure_ascii=False))
        return
    for res in results:
        head = "PASS" if res["ok"] else "FAIL"
        state = res["state"] or "-"
        print("[%s] %s  v%s  state=%s" % (head, res["name"],
                                          res["meta"].get("version", "?"), state))
        for e in res["errors"]:
            print("   ✗ " + e)
        for w in res["warnings"]:
            print("   ! " + w)


def main(argv=None):
    ap = argparse.ArgumentParser(description="插件沉淀校验器")
    ap.add_argument("--root", default=".", help="仓库根（默认当前目录）")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="校验全部插件")
    group.add_argument("--plugin", help="只校验一个插件（目录名）")
    ap.add_argument("--apply", action="store_true",
                    help="通过后写回 index.json（draft → reviewed）")
    ap.add_argument("--json", action="store_true", help="stdout 单行 JSON")
    ap.add_argument("--no-probes", action="store_true", help="不跑 probes.py（只在排查时用）")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(os.path.join(root, "plugins")):
        print("输入不合法：%s 下没有 plugins/ 目录" % root, file=sys.stderr)
        return 2

    index, err = load_index(root)
    if index is None:
        print("输入不合法：%s" % err, file=sys.stderr)
        return 2

    if args.all:
        names = list_plugin_names(root)
        if not names:
            print("输入不合法：plugins/ 下一个插件目录都没有", file=sys.stderr)
            return 2
    else:
        if not os.path.isdir(os.path.join(root, "plugins", args.plugin)):
            print("输入不合法：没有 plugins/%s" % args.plugin, file=sys.stderr)
            return 2
        names = [args.plugin]

    results = [review_plugin(root, n, index, run_probes=not args.no_probes) for n in names]
    ok = all(r["ok"] for r in results)

    changed = []
    if ok and args.apply:
        changed, apply_err = apply_to_index(root, results)
        for e in apply_err:
            print("写回失败：" + e, file=sys.stderr)
            ok = False

    print_report(results, as_json=args.json)
    if changed and not args.json:
        print("index.json 已更新：%s → reviewed" % "、".join(changed))
    if not args.json:
        print("结果：%s（%d 个插件，%d 个问题）"
              % ("全部通过" if ok else "有问题", len(results),
                 sum(len(r["errors"]) for r in results)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
