#!/usr/bin/env bash
# Batch31：在 Mac 上把「执行岗 + 质检岗」装成常驻服务（launchd）。
#
# 背景：Batch30 之前，Mac 上的 worker 是我用 nohup 起的，机器一睡就没了，
# 看板第二行、第三行随之消失 —— 那不是员工，是临时工。这一批把它们变成 launchd 托管：
# 登录自启、进程退出自动拉起、停止时给 90 秒优雅收尾。
#
# 两个设计取舍：
#   1) 一个岗位一个 plist（launchd 一个 job 只能管一个进程），靠循环生成，不手工改文件；
#   2) plist 不放口令 —— 只传「我是哪个岗位」，口令从 ~/.mwp/worker.env 读（chmod 600）。
#
# 用法：
#   deploy/install-mac-workers.sh                 # 默认装 ops-mac 与 qa-mac
#   MWP_INSTANCES="ops-mac dev-mac" deploy/install-mac-workers.sh
#   deploy/install-mac-workers.sh --uninstall     # 卸掉上面这些岗位
#   deploy/install-mac-workers.sh --status        # 看它们活着没
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$REPO/deploy/run-mw-worker.sh"
AGENTS="$HOME/Library/LaunchAgents"
CONF="${MWP_CONF:-$HOME/.mwp/worker.env}"
INSTANCES="${MWP_INSTANCES:-ops-mac qa-mac}"
MODE="install"

case "${1:-}" in
  --uninstall) MODE="uninstall" ;;
  --status)    MODE="status" ;;
  "")          ;;
  *) echo "未知参数：$1（可用 --uninstall / --status）" >&2; exit 64 ;;
esac

labels() {
  for inst in $INSTANCES; do echo "com.mwp.worker.$inst"; done
}

if [ "$MODE" = "status" ]; then
  for inst in $INSTANCES; do
    label="com.mwp.worker.$inst"
    line="$(launchctl list | grep -F "$label" || true)"
    if [ -n "$line" ]; then
      pid="$(echo "$line" | awk '{print $1}')"
      code="$(echo "$line" | awk '{print $2}')"
      echo "$label  在册  pid=$pid  上次退出码=$code"
    else
      echo "$label  未在册"
    fi
    [ -f "$REPO/run/$inst.log" ] && tail -n 3 "$REPO/run/$inst.log" | sed 's/^/    /'
  done
  exit 0
fi

mkdir -p "$AGENTS" "$REPO/run"
chmod +x "$WRAPPER" 2>/dev/null || true

if [ "$MODE" = "uninstall" ]; then
  for inst in $INSTANCES; do
    label="com.mwp.worker.$inst"
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "已卸载 $label"
  done
  exit 0
fi

if [ ! -f "$CONF" ]; then
  mkdir -p "$(dirname "$CONF")"
  cat > "$CONF" <<EOF
# Batch31 执行方配置。口令只放这里：不进 plist、不进 git、不贴聊天。
# 填好 MWP_TOKEN_WORKER 再重跑 deploy/install-mac-workers.sh
MWP_BASE_URL=http://192.168.0.106:8000
MWP_REPO=$REPO
MWP_WORKER_ID=workbuddy
MWP_TOKEN_WORKER=
EOF
  chmod 600 "$CONF"
  echo "已生成配置模板 $CONF"
  echo "把平台 .env 里的 API_TOKEN_WORKER 那一枚填进 MWP_TOKEN_WORKER，然后重跑本脚本。"
  exit 78
fi

chmod 600 "$CONF"

if ! grep -qE '^MWP_TOKEN_WORKER=.+' "$CONF"; then
  echo "配置里的 MWP_TOKEN_WORKER 还是空的，先把执行方口令填上" >&2
  exit 78
fi

for inst in $INSTANCES; do
  label="com.mwp.worker.$inst"
  plist="$AGENTS/$label.plist"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Batch31 生成件：改参数请改 ~/.mwp/worker.env 或重跑 deploy/install-mac-workers.sh，别手改这里 -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$WRAPPER</string>
        <string>$inst</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$REPO</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>ExitTimeOut</key>
    <integer>90</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$REPO/run/$inst.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO/run/$inst.err.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$plist"
  launchctl enable "gui/$UID/$label"
  launchctl kickstart -k "gui/$UID/$label"
  echo "已装载 $label"
done

echo "全部装载完成。核对：deploy/install-mac-workers.sh --status"
