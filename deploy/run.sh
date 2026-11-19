#!/usr/bin/env bash
# macOS / Linux 一键启动脚本
# 用法：bash deploy/run.sh            （默认 8000 端口）
#      PORT=8800 TOKEN=xxx bash deploy/run.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"

PORT="${PORT:-8000}"
TOKEN="${TOKEN:-}"

echo "工作目录：$ROOT"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "未找到 .venv，创建中…"
  python3 -m venv .venv
fi
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "创建虚拟环境失败，请确认已安装 python3"
  exit 1
fi

echo "安装依赖…"
"$PY" -m pip install -q -r requirements.txt

echo "执行迁移…"
"$PY" deploy/migrate.py

[ -n "$TOKEN" ] && export API_TOKEN="$TOKEN"
export DATABASE_URL="${DATABASE_URL:-sqlite:///./data/mvp.db}"
export ARCHIVE_ROOT="${ARCHIVE_ROOT:-./archive}"
mkdir -p "$ROOT/data"

echo ""
echo "看板地址：http://127.0.0.1:${PORT}/"
echo "接口文档：http://127.0.0.1:${PORT}/docs"
echo "按 Ctrl+C 停止"
echo ""

exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
