"""Batch26 验收：看板台账增强（停滞 / 时间轴 / 心跳 / 快照），10 项。

守的是什么：
- 停滞判定必须有常量、有阈值，且"停滞"（该在跑却没动）与"滞留"（一直没人领）分开命名；
- 单行时间轴必须按本页最长耗时归一，否则一行一格没有可比性；
- 心跳必须是"N 前"且超时能红字，不能退回一串时钟时间；
- 快照必须前端本地生成（单文件、无脚本、无外链），且不许借它去扩接口面；
- 前端零外部依赖这条红线不许被这批改动突破。
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def check(label, ok, detail=""):
    if ok:
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" —— {detail}" if detail else ""))
        FAILED.append(label)


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


appjs = read("app/static/app.js")
html = read("app/static/index.html")
css = read("app/static/style.css")

print("=== A 停滞判定 ===")
check("有独立阈值常量 STALL_MS", "const STALL_MS" in appjs)
check("停滞/滞留分开命名", "stallInfo" in appjs and '"停滞"' in appjs and '"滞留"' in appjs)
check("pending 的阈值比 running 更宽（排队不算卡死）",
      "STALL_MS * 3" in appjs)
check("停滞行有行首标记（不靠颜色深浅猜）", 'classList.toggle("stalled"' in appjs and "tr.stalled" in css)

print("=== B 单行时间轴 ===")
check("按本页最长耗时归一", "pipeMaxMs" in appjs and "maxMs" in appjs)
check("未收尾按「到现在」算，且端点空心", 'rail-dot.open' in css and '"open"' in appjs)
check("收尾端点用实心（成功绿、失败红）", "rail-dot.done" in css and "rail-dot.halt" in css)

print("=== C 心跳与并发 ===")
check("最近上报显示「N 前」", "function heartbeatNode" in appjs and "fmtAgo" in appjs)
check("超时红字阈值与后端同口径", "HEARTBEAT_WARN_MS = WORKER_ONLINE_MS" in appjs)
check("并发用刻度（实心=在跑槽位）", "function concurNode" in appjs and ".dots i.on" in css)

print("=== D 快照导出 ===")
snap = appjs.split("function snapshotHtml")[1].split("function saveSnapshot")[0]
check("快照单文件、无外链、无脚本", "<script" not in snap and "http" not in snap)
check("快照有转义（原文里的尖括号不许穿透）", "function esc" in appjs and "&lt;" in appjs)
check("下载走 Blob，不借后端", "URL.createObjectURL" in appjs and "download" in appjs)

print("=== E 红线没被破 ===")
check("看板入口有停滞计数与存快照按钮", 'id="pipeStall"' in html and 'id="snapPipes"' in html)
check("index.html 仍零外部依赖",
      not re.search(r"https?://", html)
      and all(s.startswith("/static/") for s in re.findall(r'<script[^>]+src="([^"]+)"', html)))
check("接口路由表仍 11 个", len(re.findall(r"^\s{2}\w+:", appjs.split("};")[0], re.M)) == 11)

print()
if FAILED:
    print(f"结果：{len(FAILED)} 项失败 —— {FAILED}")
    sys.exit(1)
print("结果：全部通过（0 项失败）")
