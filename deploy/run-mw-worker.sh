#!/usr/bin/env bash
# Batch31：Mac 执行岗／质检岗的启动器（launchd 调用它，不直接调 python）。
#
# 存在的理由：launchd 的 plist 是明文躺在 ~/Library/LaunchAgents 里的，
# 把口令写进 plist 等于把钥匙挂在门上。所以 plist 只传「我是哪个岗位」，
# 口令、地址、仓库路径一律从 ~/.mwp/worker.env（chmod 600）读。
#
# 用法：run-mw-worker.sh <instance_id>
#   instance_id 形如「角色-主机」（ops-mac / qa-mac / dev-win）：
#   角色前缀决定默认并发与可操作目录 —— 质检岗只能碰 scripts，改不了业务代码。
set -euo pipefail

INSTANCE="${1:?用法: run-mw-worker.sh <instance_id>（如 ops-mac / qa-mac）}"
CONF="${MWP_CONF:-$HOME/.mwp/worker.env}"

if [ ! -f "$CONF" ]; then
  echo "[fatal] 缺少配置文件 $CONF（跑一次 deploy/install-mac-workers.sh 会生成模板）" >&2
  exit 78
fi

set -a
# shellcheck disable=SC1090
. "$CONF"
set +a

: "${MWP_BASE_URL:?worker.env 里缺 MWP_BASE_URL}"
: "${MWP_REPO:?worker.env 里缺 MWP_REPO}"
: "${MWP_TOKEN_WORKER:?worker.env 里缺 MWP_TOKEN_WORKER（执行方口令，不是全权口令）}"

ROLE="${INSTANCE%%-*}"
CONC="${MWP_CONCURRENCY:-}"
ALLOW_ROOT="$MWP_REPO"
WORK_DIR="$MWP_REPO"

# 质检岗的默认姿态：单并发、只许碰 scripts（验收脚本在这里，业务代码不该被它摸到）
if [ "$ROLE" = "qa" ]; then
  [ -z "$CONC" ] && CONC=1
  ALLOW_ROOT="$MWP_REPO/scripts"
  WORK_DIR="$MWP_REPO/scripts"
fi
[ -z "$CONC" ] && CONC=2

PY="${MWP_PYTHON:-}"
if [ -z "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
[ -x "$PY" ] || { echo "[fatal] 找不到 python3（可在 worker.env 里用 MWP_PYTHON 指定）" >&2; exit 78; }

mkdir -p "$MWP_REPO/run"

echo "[run] instance=$INSTANCE role=$ROLE concurrency=$CONC allow_root=$ALLOW_ROOT"
exec "$PY" "$MWP_REPO/worker/agent_worker.py" \
  --base-url "$MWP_BASE_URL" \
  --worker-id "${MWP_WORKER_ID:-workbuddy}" \
  --instance-id "$INSTANCE" \
  --concurrency "$CONC" \
  --grace-seconds "${MWP_GRACE_SECONDS:-30}" \
  --status-file "$MWP_REPO/run/${INSTANCE}_status.json" \
  --state-file "$MWP_REPO/run/${INSTANCE}_done.json" \
  --allow-root "$ALLOW_ROOT" \
  --work-dir "$WORK_DIR" \
  --token "$MWP_TOKEN_WORKER"
