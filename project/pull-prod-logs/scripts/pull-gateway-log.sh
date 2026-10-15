#!/usr/bin/env bash
# 拉取生产 gateway 日志切片到本地 .scratch/logs/
# 用法: pull-gateway-log.sh [本地开始] [本地结束] [远端文件名]
# 时间一律北京时间，脚本内部换算 UTC（日志 ts 是 UTC，慢 8 小时）
set -euo pipefail

HOST="wujie@123.57.81.29"
REMOTE_DIR="/opt/platform/logs/gateway"
OUT_ROOT="/home/wj/project/.scratch/logs"

usage() {
  cat <<'EOF'
用法: pull-gateway-log.sh [本地开始] [本地结束] [远端文件名]
  本地开始  '12:00' 或 '2026-10-12 14:00'，缺省 今天 00:00
  本地结束  同上格式或 'now'，缺省 now（只影响本地裁剪与目录命名）
  远端文件  缺省 gateway.log；更早范围传 gateway.1.log / gateway.2.log …
输出: /home/wj/project/.scratch/logs/<YYYYMMDD-HHMM>-<HHMM>/<远端文件名>.log(.gz)
退出码: 0=成功  2=范围内该文件无 JSON 行（换轮转文件）  3=SSH/远端读取失败
EOF
  exit 1
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then usage; fi
if [ "$#" -gt 3 ]; then usage; fi

START_INPUT="${1:-today 00:00}"
END_INPUT="${2:-now}"
REMOTE_FILE="${3:-gateway.log}"

start_epoch=$(date -d "$START_INPUT" +%s) || { echo "✗ 无法解析开始时间: $START_INPUT"; exit 1; }
end_epoch=$(date -d "$END_INPUT" +%s) || { echo "✗ 无法解析结束时间: $END_INPUT"; exit 1; }
if [ "$end_epoch" -lt "$start_epoch" ]; then echo "✗ 结束时间早于开始时间"; exit 1; fi

start_utc=$(date -u -d "@$start_epoch" +%Y-%m-%dT%H:%M:%S)
end_utc=$(date -u -d "@$end_epoch" +%Y-%m-%dT%H:%M:%S)
tag="$(date -d "@$start_epoch" +%Y%m%d-%H%M)-$(date -d "@$end_epoch" +%H%M)"
outdir="$OUT_ROOT/$tag"
mkdir -p "$outdir"

echo "▶ 远端: $HOST:$REMOTE_DIR/$REMOTE_FILE"
echo "▶ 切片: 本地 $(date -d "@$start_epoch" '+%F %T') ~ $(date -d "@$end_epoch" '+%F %T')（UTC $start_utc ~ $end_utc）"

# 远端定位起点：第一条 ts >= start_utc 的 JSON 行。
# ts 取 substr($0,8,19) 即 "ts":" 后固定 19 字符 "YYYY-MM-DDTHH:MM:SS"，ISO 字符串字典序可直接比较。
# 只认带引号的字符串 ts（"ts":1791789991933 这类数值 epoch 的行跳过，不影响定位密度）。
first_line=$(printf '%s' "$start_utc" | ssh -o ConnectTimeout=10 "$HOST" \
  "awk -v s=\"\$(cat)\" '/^\{\"ts\":\"/ { ts=substr(\$0,8,19); if (ts>=s) { print NR\" \"ts; exit } }' '$REMOTE_DIR/$REMOTE_FILE'") \
  || { echo "✗ SSH 或远端读取失败"; exit 3; }

if [ -z "$first_line" ]; then
  file_first=$(ssh -o ConnectTimeout=10 "$HOST" \
    "awk '/^\{\"ts\":\"/ { print substr(\$0,8,19); exit }' '$REMOTE_DIR/$REMOTE_FILE'")
  echo "⚠ 该文件里没有 ts >= $start_utc 的 JSON 行（文件最早 JSON ts = ${file_first:-无}）"
  echo "  更早的日志在轮转文件 gateway.1.log / gateway.2.log …（数字越大越旧），选一个重跑。"
  exit 2
fi

total=$(ssh -o ConnectTimeout=10 "$HOST" "wc -l < '$REMOTE_DIR/$REMOTE_FILE'")
first_ts_at_start=${first_line#* }
first_line=${first_line%% *}
echo "▶ 起点: 第 $first_line / $total 行（起点前的无时间戳文本行不带上）"
if [[ "$first_ts_at_start" > "$end_utc" ]]; then
  echo "⚠ 起点第一条 JSON ts（$first_ts_at_start）已晚于结束时间 $end_utc："
  echo "  该窗口内没有字符串 ts 的 JSON 行（可能只有数值 ts 行或完全静默），裁剪后可能为空。"
fi

ssh -o ConnectTimeout=10 "$HOST" \
  "tail -n +$first_line '$REMOTE_DIR/$REMOTE_FILE' | gzip" > "$outdir/$REMOTE_FILE.gz"
gunzip -kf "$outdir/$REMOTE_FILE.gz"

# 本地裁掉 end_utc 之后的行（若结束时间已过，等于不裁）
if [ "$end_epoch" -lt "$(date +%s)" ]; then
  awk -v e="$end_utc" 'BEGIN{keep=1} /^\{"ts":"/ { ts=substr($0,8,19); if (ts>=e) keep=0 } keep' \
    "$outdir/$REMOTE_FILE" > "$outdir/$REMOTE_FILE.trim" \
    && mv "$outdir/$REMOTE_FILE.trim" "$outdir/$REMOTE_FILE"
fi

lines=$(wc -l < "$outdir/$REMOTE_FILE")
size=$(du -h "$outdir/$REMOTE_FILE" | cut -f1)
first_ts=$(awk '/^\{"ts":"/ { print substr($0,8,19); exit }' "$outdir/$REMOTE_FILE")
echo "▶ 已保存: $outdir/$REMOTE_FILE"
if [ "$lines" -eq 0 ]; then
  echo "  ⚠ 裁剪后 0 行——窗口内没有字符串 ts 的 JSON 行（见上方警告），原件在 $outdir/$REMOTE_FILE.gz"
else
  echo "  $lines 行 / $size / 覆盖 UTC ${first_ts:-?} 起至拉取时刻（.gz 为裁剪前原件）"
fi
