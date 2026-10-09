#!/usr/bin/env bash
# 安装 systemd user timer，每日定时运行 worktree 清理脚本。
# 幂等：重复执行只刷新 unit 文件并 reload。
#
# 用法：install-timer.sh [--schedule "daily 13:00" | "mon..fri 09:30" 等 OnCalendar 语法]

set -euo pipefail

SKILL_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLEAN_SCRIPT=$SKILL_DIR/scripts/clean-merged-worktrees.sh
LOG_FILE=/home/wj/project/.scratch/worktree-cleanup.log
UNIT_DIR=$HOME/.config/systemd/user
SERVICE_NAME=worktree-cleanup.service
TIMER_NAME=worktree-cleanup.timer

SCHEDULE="*-*-* 13:00:00"
if [[ ${1:-} == "--schedule" && -n ${2:-} ]]; then
  SCHEDULE=$2
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/$SERVICE_NAME" <<EOF
[Unit]
Description=清理已合入 origin/main 的 git worktree（agent/platform 双仓）

[Service]
Type=oneshot
Environment=WORKTREE_CLEANUP_LOG=$LOG_FILE
ExecStartPre=/usr/bin/mkdir -p $(dirname "$LOG_FILE")
ExecStart=$CLEAN_SCRIPT
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE
EOF

cat > "$UNIT_DIR/$TIMER_NAME" <<EOF
[Unit]
Description=每日定时清理已合入 main 的 worktree

[Timer]
OnCalendar=$SCHEDULE
# 错过触发点（WSL 关机）后，下次可用时补跑一次
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$TIMER_NAME"

# 让 user manager 开机即运行（不依赖有会话在线）；失败不影响定时器本身
loginctl enable-linger "$USER" 2>/dev/null \
  && echo "linger 已启用（WSL 启动即运行 user timer）" \
  || echo "提示：loginctl enable-linger 失败，timer 只在有 WSL 会话在线时生效"

echo
echo "已安装：$UNIT_DIR/$TIMER_NAME（每天 $SCHEDULE 触发，错过补跑）"
echo "日志：  $LOG_FILE"
systemctl --user list-timers "$TIMER_NAME" --no-pager | head -3
