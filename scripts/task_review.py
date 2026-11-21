"""规则复核脚本（Batch32-1）。

它不是"另一个模型来评活干得好不好"，而是**机器判据**：验收标准有没有被逐条答满。

输入：`--review-b64 <base64>`，解出 JSON：
    {"review_of": "t-xxx", "criteria": ["标准1", "标准2", ...],
     "artifacts": [{"path": "...", "exists": true}], "self_check": "..."}

判定口径（写死，不许在这里加启发式）：
- 标准第 i 条 ↔ 产物第 i 个（编号 c1..cN）：产物必须存在（exists=true）；
- 标准条数多于产物条数 → 缺项，逐条列 id（如 c3），结论 fail；
- 全部对齐 → pass。

产出：stdout 只打**一行 JSON**（Worker 原样塞进回执 logs，平台按整段解析）：
    {"review_of": "t-xxx", "verdict": "pass|fail",
     "items": [{"id": "c1", "ok": true, "evidence": "<path>"}],
     "missing": ["c3"], "note": "..."}

退出码：0 = 已给出结论（pass 或 fail 都算结论）；2 = 输入不合法（平台会当失败处理）。
只用标准库，可在任何一台装了 python3 的机器上跑（含 Mac 的 3.9）。
"""
import argparse
import base64
import json
import sys


def decode_context(raw: str) -> dict:
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 输入坏了就是坏，给清楚原因
        raise ValueError(f"--review-b64 解不出来：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("--review-b64 解出来不是对象")
    return data


def judge(ctx: dict) -> dict:
    criteria = ctx.get("criteria") or []
    artifacts = ctx.get("artifacts") or []
    if not isinstance(criteria, list) or not isinstance(artifacts, list):
        raise ValueError("criteria / artifacts 必须是数组")
    ids = [f"c{i + 1}" for i in range(len(criteria))]
    items = []
    missing = []
    for idx, cid in enumerate(ids):
        art = artifacts[idx] if idx < len(artifacts) else None
        path = (art or {}).get("path") if isinstance(art, dict) else None
        exists = bool((art or {}).get("exists")) if isinstance(art, dict) else False
        if exists and path:
            items.append({"id": cid, "ok": True, "evidence": path, "reason": ""})
        else:
            missing.append(cid)
            items.append({
                "id": cid,
                "ok": False,
                "evidence": "",
                "reason": "缺少对应产物" if not path else f"产物不存在：{path}",
            })
    verdict = "pass" if not missing else "fail"
    note = "标准逐条答满" if verdict == "pass" else "缺失项：" + "、".join(missing)
    return {
        "review_of": ctx.get("review_of"),
        "verdict": verdict,
        "items": items,
        "missing": missing,
        "note": note,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="规则复核：验收标准是否被逐条答满")
    parser.add_argument("--review-b64", required=True, help="base64 编码的复核上下文 JSON")
    args = parser.parse_args(argv)
    try:
        ctx = decode_context(args.review_b64)
        result = judge(ctx)
    except ValueError as exc:
        print(json.dumps({"verdict": "fail", "items": [], "note": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
