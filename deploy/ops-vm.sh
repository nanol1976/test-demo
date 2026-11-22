#!/usr/bin/env bash
# Batch34：云主机（ops-vm）这台"第三台机器"的启动器。
#
# 现状（如实写在脚本里，不靠口口相传）：
#   - dsh CLI 已装（全局 npm 包 @deepseek-ai/dsh，`dsh --version` 有数）；
#   - headless 是 dsh 自带的 shipped profile，不需要自己建；nvidia 路由靠 `--patch worker/dsh/nvidia-patch.yml` 叠加，
#     已在 Linux 上 `--dump-config` 验过；
#   - 适配层在这台机器上跑过离线单（桩命令）：产物落档、哈希回读、思考档都正常。
#
# 还差两样才能真接单，缺一样这个脚本都只会停在"注册不上"：
#   1) 平台可达：云主机到你的平台（Windows 上 8000 端口）没有直连路由。
#      要么 Tailscale 组网，要么在 Windows 上开一条隧道把 8000 暴露出来；
#   2) NVIDIA key：云主机上没有 ~/.dsh/.credentials.yaml，真跑模型要先放一份
#      （或用 MWP_DSH_CREDENTIALS 指到别处）。
#
# 用法：ops-vm.sh（读 $MWP_CONF，默认 ~/.mwp/worker.env）
set -euo pipefail

CONF="${MWP_CONF:-$HOME/.mwp/worker.env}"
if [ ! -f "$CONF" ]; then
  echo "[fatal] 缺少 $CONF —— 先按 deploy/ops-vm.env.example 填一份（chmod 600）" >&2
  exit 78
fi

set -a
# shellcheck disable=SC1090
. "$CONF"
set +a

: "${MWP_BASE_URL:?worker.env 缺 MWP_BASE_URL（云主机现在连不到 192.168.0.106，先解决通道）}"
: "${MWP_REPO:?worker.env 缺 MWP_REPO}"
: "${MWP_TOKEN_WORKER:?worker.env 缺 MWP_TOKEN_WORKER（执行方口令，不是全权口令）}"

INSTANCE="${MWP_INSTANCE:-ops-vm}"
ROLE="${INSTANCE%%-*}"
CONC="${MWP_CONCURRENCY:-1}"   # 外部 agent 岗默认单并发，与平台 MWP_DSH_MAX_INFLIGHT=1 对齐

# 外部 agent 要碰的就是仓库本身（读文档、跑脚本），但只给它看仓库，不给看家目录
ALLOW_ROOT="$MWP_REPO"
WORK_DIR="$MWP_REPO"

PY="${MWP_PYTHON:-$(command -v python3 || true)}"
[ -x "$PY" ] || { echo "[fatal] 找不到 python3，可在 worker.env 里用 MWP_PYTHON 指定" >&2; exit 78; }

# dsh 的运行 home 收进仓库（与适配层一致），绝不写用户的 ~/.dsh
export DSH_HOME="${MWP_DSH_HOME:-$MWP_REPO/run/dsh_home}"

# 云主机上凭证放在仓库里（run/ 已 gitignore）：没显式指定就默认用它，省一步手配
if [ -z "${MWP_DSH_CREDENTIALS:-}" ] && [ -f "$MWP_REPO/run/dsh_cred.yaml" ]; then
  export MWP_DSH_CREDENTIALS="$MWP_REPO/run/dsh_cred.yaml"
fi

# 命令不用额外指定：适配层在 Linux 上会退到 PATH 里的 dsh（全局 npm 装的那个）
echo "[run] instance=$INSTANCE role=$ROLE concurrency=$CONC dsh_home=$DSH_HOME cred=${MWP_DSH_CREDENTIALS:-（用户 ~/.dsh）}"
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
