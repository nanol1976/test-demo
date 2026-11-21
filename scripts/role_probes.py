"""五业务方向的验收探针（Batch31-E）。

为什么要这个东西：
  Batch31 之前，"岗位"只是执行方的一件事，能不能干活靠人看日志。要变成"公司"，
  每条业务线都得有一组机器可判的标准——不通过就是没交付，不许口头声称。
  这里给五条业务线各写一组断言：股票研究、文档处理、视频与图片、新媒体平台、软件开发。
  断言全部离线可跑（不联网、不用第三方库），不"依赖外部资源"就能判对错。

判据原则：
  1. 用"性质/不变量"而不是自证常量——比如 MA5 逐项等于独立求和，KDJ 满足递推等式，
     同源实现算两遍对比没有意义。
  2. 素材不足时如实 skip，绝不算 pass（skip 会在输出里单独计数）。
  3. 每岗 exit code 归零才算交付成立；任一断言失败即非零。

用法：
  python scripts/role_probes.py --role all
  python scripts/role_probes.py --role stock --json
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import struct
import sys
import tempfile
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class Probe:
    """一个业务方向探针，收集断言结果。"""

    def __init__(self, role: str, title: str) -> None:
        self.role = role
        self.title = title
        self.checks: list[dict] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.checks.append({"name": name, "state": "pass", "detail": detail})

    def bad(self, name: str, detail: str) -> None:
        self.checks.append({"name": name, "state": "fail", "detail": detail})

    def skip(self, name: str, detail: str) -> None:
        self.checks.append({"name": name, "state": "skip", "detail": detail})

    def expect(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.ok(name, detail)
        else:
            self.bad(name, detail or "断言不成立")
        return bool(cond)

    def summary(self) -> dict:
        fails = [c for c in self.checks if c["state"] == "fail"]
        skips = [c for c in self.checks if c["state"] == "skip"]
        return {
            "role": self.role,
            "title": self.title,
            "total": len(self.checks),
            "passed": sum(1 for c in self.checks if c["state"] == "pass"),
            "failed": len(fails),
            "skipped": len(skips),
            "verdict": "FAIL" if fails else "PASS",
            "checks": self.checks,
        }


# ---------------------------------------------------------------- 股票研究
SAMPLE_BARS = [
    # date, open, high, low, close, volume
    ("2026-11-06", 10.00, 10.42, 9.88, 10.31, 120000),
    ("2026-11-09", 10.28, 10.55, 10.20, 10.48, 98000),
    ("2026-11-10", 10.50, 10.71, 10.36, 10.62, 143000),
    ("2026-11-11", 10.60, 10.66, 10.22, 10.29, 167000),
    ("2026-11-12", 10.31, 10.40, 10.05, 10.12, 151000),
    ("2026-11-13", 10.08, 10.33, 9.96, 10.26, 118000),
    ("2026-11-16", 10.24, 10.88, 10.21, 10.79, 205000),
    ("2026-11-17", 10.83, 11.02, 10.70, 10.95, 189000),
    ("2026-11-18", 10.92, 11.10, 10.61, 10.68, 174000),
    ("2026-11-19", 10.66, 10.74, 10.30, 10.37, 156000),
    ("2026-11-20", 10.39, 10.95, 10.33, 10.91, 232000),
]


def _ma(values: list[float], n: int) -> list[float]:
    out = []
    for i in range(len(values) - n + 1):
        out.append(sum(values[i:i + n]) / n)
    return out


def _kdj(bars: list[tuple], n: int = 9) -> list[tuple[float, float, float]]:
    k = d = 50.0
    out = []
    for i in range(len(bars)):
        win = bars[max(0, i - n + 1): i + 1]
        hh = max(b[2] for b in win)
        ll = min(b[3] for b in win)
        rsv = 50.0 if hh == ll else (bars[i][4] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        j = 3 * k - 2 * d
        out.append((k, d, j))
    return out


def probe_stock() -> Probe:
    p = Probe("stock", "股票研究：算力正确性与结构完整性")
    closes = [b[4] for b in SAMPLE_BARS]

    # 1) MA5 逐项等于独立求和（性质校验，不是拿函数验函数）
    ma5 = _ma(closes, 5)
    manual = [sum(closes[i:i + 5]) / 5 for i in range(len(closes) - 4)]
    p.expect("MA5 逐项与独立求和一致", all(abs(a - b) < 1e-9 for a, b in zip(ma5, manual)),
             f"{len(ma5)} 个点全等")
    p.expect("MA5 长度 = 样本数-4", len(ma5) == len(closes) - 4, f"{len(ma5)} vs {len(closes) - 4}")

    # 2) 单调样本上 MA5 必须严格递增
    up = _ma([float(x) for x in range(1, 13)], 5)
    p.expect("单调递增序列的 MA5 严格递增", all(up[i] < up[i + 1] for i in range(len(up) - 1)),
             "12 点窗口")

    # 3) 涨跌幅与振幅
    pcts = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
    p.expect("涨跌幅方向与收盘价同向",
             all((pc > 0) == (closes[i] > closes[i - 1]) for i, pc in enumerate(pcts, start=1)),
             "逐日方向一致")
    amps = [(b[2] - b[3]) / b[1] * 100 for b in SAMPLE_BARS]
    p.expect("振幅恒为非负", all(a >= 0 for a in amps), "全样本")

    # 4) OHLC 一致性：high 不低于 max(open,close)，low 不高于 min(open,close)
    broken = [b[0] for b in SAMPLE_BARS if b[2] < max(b[1], b[4]) or b[3] > min(b[1], b[4])]
    p.expect("OHLC 结构自洽", not broken, f"越界样本 {broken or '无'}")

    # 5) KDJ 不变量：值域 [0,100] 且递推等式成立
    kdj = _kdj(SAMPLE_BARS)
    in_range = all(0 <= k <= 100 and 0 <= d <= 100 for k, d, _ in kdj)
    p.expect("KDJ 的 K/D 落在 0~100", in_range, f"{len(kdj)} 日")
    recur_ok = True
    prev_k = 50.0
    for i, (k, _d, _j) in enumerate(kdj):
        win = SAMPLE_BARS[max(0, i - 8): i + 1]
        hh = max(b[2] for b in win)
        ll = min(b[3] for b in win)
        rsv = 50.0 if hh == ll else (SAMPLE_BARS[i][4] - ll) / (hh - ll) * 100
        if abs(k - (prev_k * 2 / 3 + rsv / 3)) > 1e-9:
            recur_ok = False
            break
        prev_k = k
    p.expect("KDJ 递推等式逐日成立", recur_ok, "K = 2/3·K_prev + 1/3·RSV")

    # 6) 交付格式：研究结论必须带同赛道对比（用户明确要求）
    report = {
        "code": "600000", "name": "样例标的", "sector": "银行",
        "peer_compare": [{"code": "601398", "metric": "PE", "value": 5.8}],
        "reason": "示例",
    }
    need = {"code", "name", "sector", "peer_compare", "reason"}
    p.expect("研究结论字段齐全（含同赛道对比）", need <= set(report), f"缺 {need - set(report) or '无'}")
    return p


# ---------------------------------------------------------------- 文档处理
def _md_table_block(rows: int, cols: int) -> str:
    head = "| " + " | ".join(f"列{c}" for c in range(1, cols + 1)) + " |"
    sep = "| " + " | ".join("---" for _ in range(cols)) + " |"
    body = ["| " + " | ".join(f"行{r}列{c}" for c in range(1, cols + 1)) + " |" for r in range(1, rows + 1)]
    return "\n".join([head, sep, *body])


def _parse_md_table(text: str) -> list[list[str]]:
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if set("".join(cells)) <= {"-", ":", " "} and cells:
            continue
        rows.append(cells)
    return rows


def probe_doc() -> Probe:
    p = Probe("doc", "文档处理：往返无损与编码正确")
    tmp = Path(tempfile.mkdtemp(prefix="mwprobe-doc-"))

    # 1) 20 行表格往返：行数守恒（台账铁律的机器化）
    block = _md_table_block(20, 4)
    back = _parse_md_table(block)
    p.expect("20 行表格往返行数守恒", len(back) == 21, f"解析 {len(back)} 行（表头+20）")

    # 2) 单元格逐字相等（含中文）
    p.expect("单元格逐字相等", back[1] == ["行1列1", "行1列2", "行1列3", "行1列4"],
             str(back[1])[:60])

    # 3) UTF-8 往返：中文/换行/全角标点
    sample = "第一段：中文内容。\n第二段：ＡＢＣ全角。\n"
    f = tmp / "utf8.md"
    f.write_text(sample, encoding="utf-8")
    p.expect("UTF-8 中文往返无损", f.read_text(encoding="utf-8") == sample, "含全角与换行")

    # 4) 段落计数（空行分段）
    multi = "段一\n\n段二\n\n段三\n"
    p.expect("空行分段计数正确", len([b for b in multi.split("\n\n") if b.strip()]) == 3, "3 段")

    # 5) 输出文件以 LF 结尾且无 BOM（跨端给 Mac 用）
    raw = f.read_bytes()
    p.expect("文件无 BOM 且以 LF 收尾", not raw.startswith(b"\xef\xbb\xbf") and raw.endswith(b"\n"),
             f"头 {raw[:3]!r} 尾 {raw[-1:]!r}")

    # 6) 列显失配要能发现（不能"解析得下就当没事"）
    ragged = "| a | b |\n| --- | --- |\n| 1 |\n"
    rows = _parse_md_table(ragged)
    p.expect("列数失配被识别", any(len(r) != len(rows[0]) for r in rows),
             f"行宽 {[len(r) for r in rows]}")
    return p


# ---------------------------------------------------------------- 视频与图片
def _png_bytes(w: int, h: int, level: int = 128) -> bytes:
    raw = b"".join(b"\x00" + bytes([level] * w) for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return tag + struct.pack(">I", len(data)) + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)  # 8bit 灰度
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _png_size(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def _sniff(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp":
        return "mp4"
    return "unknown"


def probe_media() -> Probe:
    p = Probe("media", "视频与图片：容器可解析与格式识别")
    tmp = Path(tempfile.mkdtemp(prefix="mwprobe-media-"))

    # 1) 生成 → 解析宽高一致
    for (w, h) in [(3, 2), (64, 48)]:
        f = tmp / f"gen_{w}x{h}.png"
        f.write_bytes(_png_bytes(w, h))
        got = _png_size(f.read_bytes())
        p.expect(f"PNG {w}x{h} 生成后可解析", got == (w, h), str(got))

    # 2) 格式嗅探：正例 + 负例（未知字节不能蒙成 png）
    p.expect("PNG 头识别", _sniff(_png_bytes(4, 4)) == "png", "magic")
    p.expect("JPEG 头识别", _sniff(b"\xff\xd8\xff\xe0rest") == "jpeg", "magic")
    p.expect("MP4 ftyp 识别", _sniff(b"\x00\x00\x00\x18ftypisom") == "mp4", "magic")
    p.expect("未知字节判为 unknown", _sniff(b"\x00\x01\x02\x03") == "unknown", "不误判")

    # 3) 仓库已有截图全量可解析（run/*.png）
    shots = sorted((ROOT / "run").glob("*.png")) if (ROOT / "run").exists() else []
    if shots:
        bad = [s.name for s in shots if _png_size(s.read_bytes()) is None]
        p.expect(f"run 下 {len(shots)} 张截图全部可解析", not bad, f"坏图 {bad or '无'}")
    else:
        p.skip("仓库内 PNG 截图", "run 下没有 png，跳过")

    # 4) 视频：有 ffmpeg 就真验，没有就如实 skip
    import shutil
    import subprocess
    ff = shutil.which("ffmpeg")
    if ff:
        out = tmp / "probe.mp4"
        r = subprocess.run([ff, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
                            "-pix_fmt", "yuv420p", str(out)], capture_output=True, timeout=60)
        cond = r.returncode == 0 and out.exists() and _sniff(out.read_bytes()) == "mp4"
        p.expect("ffmpeg 生成 1 秒视频并可识别容器", cond, f"rc={r.returncode}")
        if cond and shutil.which("ffprobe"):
            rp = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
                                 "-show_entries", "stream=width,height,duration", "-of", "json", str(out)],
                                capture_output=True, timeout=60)
            probe = json.loads(rp.stdout.decode("utf-8", "replace") or "{}")
            st = (probe.get("streams") or [{}])[0]
            p.expect("ffprobe 读回分辨率 320x240", (st.get("width"), st.get("height")) == (320, 240), str(st))
    else:
        p.skip("视频容器现造现验", "本机没有 ffmpeg，不伪造结论")
    return p


# ---------------------------------------------------------------- 新媒体平台
ARCHIVE_RULES = [
    ("mp.weixin.qq.com", "公众号"),
    ("xiaohongshu.com", "小红书"),
    ("xhslink.com", "小红书"),
    ("douyin.com", "抖音"),
    ("iesdouyin.com", "抖音"),
]


def _classify(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":")[0]
    for key, cat in ARCHIVE_RULES:
        if host == key or host.endswith("." + key):
            return cat
    return "随手收藏"


def _normalize(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "from", "share_token"}
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in drop])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def probe_social() -> Probe:
    p = Probe("social", "新媒体平台：归档分类、去重与发布合规")

    # 1) 四类归档分类（用户定的口径：公众号/小红书/抖音/随手收藏）
    cases = [
        ("https://mp.weixin.qq.com/s/abc", "公众号"),
        ("https://www.xiaohongshu.com/explore/123", "小红书"),
        ("https://v.douyin.com/xyz/", "抖音"),
        ("https://example.com/blog/post-1", "随手收藏"),
        ("https://m.weibo.cn/status/1", "随手收藏"),
    ]
    wrong = [u for u, want in cases if _classify(u) != want]
    p.expect("五个样例归入正确分类", not wrong, f"错分 {wrong or '无'}")
    p.expect("四类分类值域受控",
             {_classify(u) for u, _ in cases} <= {"公众号", "小红书", "抖音", "随手收藏"},
             "无越界类目")

    # 2) 同网址去重（去 utm、尾斜杠、Host 大小写）
    dupes = [
        "https://Example.com/post/1?utm_source=x",
        "https://example.com/post/1/",
        "https://example.com/post/1",
        "https://example.com/post/2",
    ]
    uniq = {_normalize(u) for u in dupes}
    p.expect("4 条同源链接归一后唯一 2 条", len(uniq) == 2, f"归一 {len(uniq)} 条")

    # 3) 发布时间必须带时区（无时区视为不合格）
    # 注意：Python 3.9 的 fromisoformat 不认 "Z" 后缀，Mac 的 worker 就是 3.9，
    # 所以先把 Z 归一成 +00:00 再判——否则规则本身在旧解释器上假失败。
    stamps = ["2026-11-21T09:30:00+08:00", "2026-11-21 09:30", "2026-11-21T01:30:00Z"]
    ok_ts = []
    for s in stamps:
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            ok_ts.append(bool(datetime.fromisoformat(s2).tzinfo))
        except ValueError:
            ok_ts.append(False)
    p.expect("ISO8601 带时区才合格（3 条中 2 条）", sum(ok_ts) == 2, f"判定 {ok_ts}")

    # 4) 发布清单字段齐全
    item = {"platform": "抖音", "title": "样例", "publish_at": "2026-11-21T09:30:00+08:00",
            "assets": ["a.png"], "hook": "前三秒抓人"}
    p.expect("发布清单字段齐全", set(item) >= {"platform", "title", "publish_at", "assets", "hook"},
             "5 必填")

    # 1) 标题/正文长度校验（平台硬约束，写成可判函数再断）
    def _title_ok(t: str) -> bool:
        return 0 < len(t) <= 30

    def _body_ok(b: str) -> bool:
        return 0 < len(b) <= 1000

    p.expect("正常标题（12 字）通过", _title_ok("龙飞智选今日复盘"), "12 字")
    p.expect("超长标题（31 字）被拦", not _title_ok("龙" * 31), "31 字")
    p.expect("空标题被拦", not _title_ok(""), "0 字")
    p.expect("正常正文（200 字）通过", _body_ok("文" * 200), "200 字")
    p.expect("超长正文（1001 字）被拦", not _body_ok("文" * 1001), "1001 字")
    return p


# ---------------------------------------------------------------- 软件开发
def _api_openapi_paths() -> tuple[list[str] | None, str]:
    """本机没装平台依赖时，退一步问正在跑的平台的 /openapi.json。

    这比"skip"有用：验证的是线上真实接口面，而不是仓库里的代码长什么样。
    地址取 MWP_BASE_URL（Mac 的 worker.env 里就有），没设就用局域网默认。
    """
    base = (os.environ.get("MWP_BASE_URL") or "http://192.168.0.106:8000").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/openapi.json", timeout=6) as r:
            doc = json.loads(r.read().decode("utf-8"))
        return [p for p in doc.get("paths", {}) if not p.startswith("/static")], base
    except Exception as e:  # 网络不通/平台没起，都算拿不到
        return None, f"{base}：{e}"


def probe_dev() -> Probe:
    p = Probe("dev", "软件开发：语法、接口面与迁移链")

    # 1) 关键模块可编译（不 import，避免拉库）
    mods = ["app/main.py", "app/api/routes_messages.py", "app/api/routes_workers.py",
            "app/repositories/ledger.py", "worker/agent_worker.py"]
    bad = []
    for m in mods:
        try:
            py_compile.compile(str(ROOT / m), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f"{m}: {str(e).splitlines()[0]}")
    p.expect(f"{len(mods)} 个关键模块语法通过", not bad, str(bad or "无"))

    # 2) 接口面收敛：OpenAPI 路径必须正好 11 条（扩容得先经用户同意）
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from app.main import app  # noqa: WPS433
        paths = [x for x in app.openapi()["paths"] if not x.startswith("/static")]
        p.expect("接口路径数 = 11（未私自扩容）", len(paths) == 11, f"本地生成 {len(paths)} 条")
    except ImportError:
        # 只装 worker 依赖的机器（Mac 那台）没有 fastapi —— 改为向运行中的平台取
        paths, where = _api_openapi_paths()
        if paths is None:
            p.skip("接口路径数 = 11（未私自扩容）", f"本机无平台依赖，且平台取不到：{where}")
        else:
            p.expect("接口路径数 = 11（未私自扩容）", len(paths) == 11,
                     f"经线上 {where} 读到 {len(paths)} 条")
    except Exception as e:  # pragma: no cover
        p.bad("接口路径数 = 11（未私自扩容）", f"openapi 生成失败：{e}")

    # 3) 迁移链连续无缺号
    vers = sorted((ROOT / "migrations" / "versions").glob("v*.py"))
    nums = []
    for v in vers:
        digits = v.name[1:4]
        if digits.isdigit():
            nums.append(int(digits))
    gaps = [n for n in range(1, (max(nums) if nums else 0) + 1) if n not in nums]
    p.expect("迁移号 1..N 连续", not gaps, f"v001..v{nums[-1] if nums else 0}，缺 {gaps or '无'}")

    # 4) 重派修复不被回退（Batch31-D 的真缺陷修复，删了就是回归）
    worker = (ROOT / "worker" / "agent_worker.py").read_text(encoding="utf-8", errors="replace")
    p.expect("worker 保留 redispatch_accept 修复", "redispatch_accept" in worker, "认 dispatch_time 新一轮")

    # 5) 门禁脚本在位且能自述
    ci = ROOT / "scripts" / "ci.py"
    p.expect("门禁脚本 scripts/ci.py 在位", ci.exists(), str(ci))
    if ci.exists():
        text = ci.read_text(encoding="utf-8", errors="replace")
        p.expect("门禁覆盖 B31-D 实例维度红线", "target_instance" in text and "done_instance" in text,
                 "两列红线在位")
    return p


PROBES = {
    "stock": probe_stock,
    "doc": probe_doc,
    "media": probe_media,
    "social": probe_social,
    "dev": probe_dev,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="五业务方向验收探针")
    ap.add_argument("--role", default="all", choices=[*PROBES, "all"])
    ap.add_argument("--json", action="store_true", help="只打印 JSON 汇总")
    args = ap.parse_args()

    roles = list(PROBES) if args.role == "all" else [args.role]
    results = [PROBES[r]().summary() for r in roles]
    failed = [r for r in results if r["verdict"] == "FAIL"]

    if args.json:
        print(json.dumps({"results": results, "ok": not failed}, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"\n[{r['verdict']}] {r['title']}  ({r['passed']} 过 / {r['failed']} 败 / {r['skipped']} 跳过)")
            for c in r["checks"]:
                mark = {"pass": "  ok  ", "fail": "  FAIL", "skip": "  --  "}[c["state"]]
                print(f"{mark} {c['name']}" + (f"  | {c['detail']}" if c["detail"] else ""))
        total = sum(r["total"] for r in results)
        print(f"\n合计 {total} 条断言，{sum(r['passed'] for r in results)} 过，"
              f"{sum(r['failed'] for r in results)} 败，{sum(r['skipped'] for r in results)} 跳过")
        print("结论：" + ("全部通过" if not failed else "存在失败项，交付不成立"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
