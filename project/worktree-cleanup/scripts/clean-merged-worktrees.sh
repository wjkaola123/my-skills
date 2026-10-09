#!/usr/bin/env bash
# 清理已合入 origin/main 的 git worktree 及其分支。
#
# 安全规则（定时无人值守也安全）：
#   1. 只删「分支 tip 是 origin/main 祖先」的 worktree（先 fetch --prune）。
#   2. worktree 里有未提交改动或非 node_modules 的未跟踪文件 → 跳过并报告，
#      绝不 --force 硬删（node_modules 视为一次性产物，可随 worktree 删除）。
#   3. 主检出（仓库根 worktree）、detached HEAD、locked 的 worktree 一律不碰。
#   4. worktree 目录下的孤儿目录（未注册到 git）只报告不删除——里面可能有 .env。
#   5. 本地与远端同步：已合入的 ag-* 远程分支 push --delete 删除；已合入、
#      未挂任何 worktree 的 ag-* 本地裸分支一并删除。feat/*、develop 等
#      非 ag-* 分支即使已合入也不碰。
#
# 用法：
#   clean-merged-worktrees.sh [--dry-run]
#   REPOS="/path/to/repo1 /path/to/repo2" 可覆盖默认仓库列表（空格分隔）。

set -u

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# 定时任务通过 WORKTREE_CLEANUP_LOG 指定日志文件；超过 1MB 只留末 200 行
if [[ -n ${WORKTREE_CLEANUP_LOG:-} && -f ${WORKTREE_CLEANUP_LOG:-} ]] \
   && (( $(stat -c%s "$WORKTREE_CLEANUP_LOG") > 1048576 )); then
  tail -n 200 "$WORKTREE_CLEANUP_LOG" > "$WORKTREE_CLEANUP_LOG.tmp" && mv "$WORKTREE_CLEANUP_LOG.tmp" "$WORKTREE_CLEANUP_LOG"
fi

# 默认双仓
DEFAULT_REPOS=(/home/wj/project/agent /home/wj/project/platform)
read -r -a REPO_LIST <<< "${REPOS:-${DEFAULT_REPOS[*]}}"

log() { printf '%s\n' "$*"; }

for repo in "${REPO_LIST[@]}"; do
  if [[ ! -d $repo/.git ]]; then
    log "[$repo] 不是 git 仓库（或为 worktree 检出），跳过"
    continue
  fi

  log "=== $repo ==="

  # 判定基准是远端 main（MR 在 GitLab 上合，本地 main 可能落后）。
  # fetch 失败时用上次的 origin/main 继续——只会少删不会多删。
  if ! timeout 90 git -C "$repo" fetch origin --prune --quiet 2>&1; then
    log "  [WARN] git fetch 失败，按上次 fetch 的 origin/main 判断"
  fi

  if ! git -C "$repo" rev-parse --verify -q origin/main >/dev/null; then
    log "  [WARN] 无 origin/main 引用，跳过该仓库"
    continue
  fi

  # 解析 porcelain 块 → 每行 "path<TAB>branch<TAB>state"
  while IFS=$'\t' read -r wt_path wt_branch wt_state; do
    # 主检出不碰；detached / locked 只报告
    if [[ $wt_path == "$repo" ]]; then
      continue
    elif [[ $wt_state != "-" ]]; then
      log "  [SKIP] $wt_path（$wt_state）"
      continue
    fi

    branch=${wt_branch#refs/heads/}

    if ! git -C "$repo" merge-base --is-ancestor "$branch" origin/main 2>/dev/null; then
      log "  [KEEP] $wt_path（$branch 未合入 origin/main）"
      continue
    fi

    # 脏检查：过滤掉 node_modules（含软链/尾斜杠写法）后仍有内容则跳过
    dirty=$(git -C "$wt_path" status --porcelain | grep -v -E '^\?\? node_modules/?$' || true)
    if [[ -n $dirty ]]; then
      log "  [SKIP] $wt_path（$branch 已合入，但有未提交/未跟踪改动：）"
      printf '%s\n' "$dirty" | sed 's/^/         /'
      continue
    fi

    if [[ $DRY_RUN == 1 ]]; then
      log "  [DRY-RUN] 将删除 $wt_path（$branch 已合入 origin/main）"
      continue
    fi

    # node_modules 未跟踪文件需要 --force 才能移除
    if git -C "$repo" worktree remove --force "$wt_path" 2>&1; then
      # 分支祖先关系已自行验证（-d 会拿本地 main 判断，可能误拒），用 -D
      if git -C "$repo" branch -D "$branch" 2>&1; then
        log "  [REMOVED] $wt_path + 分支 $branch"
      else
        log "  [WARN] worktree 已删但分支 $branch 删除失败，请手动处理"
      fi
    else
      log "  [SKIP] $wt_path（git worktree remove 失败，请手动检查）"
    fi
  done < <(
    git -C "$repo" worktree list --porcelain | awk '
      /^worktree /  { path = substr($0, 10); wt[++n] = path; state[n] = "" }
      /^branch /    { br[n] = substr($0, 19) }
      /^detached$/  { state[n] = "detached" }
      /^locked/     { state[n] = "locked" }
      END { for (i = 1; i <= n; i++) printf "%s\t%s\t%s\n", wt[i], (br[i] ? br[i] : "-"), (state[i] ? state[i] : "-") }
    '
  )

  # 孤儿目录扫描：已注册 worktree 的父目录里，不属于任何注册项的子目录。
  # 排除主检出的父目录（工作区根，下面是 agent/ platform/ 等正经目录，
  # 不是 worktree 容器）。
  repo_root_parent=$(dirname "$repo")
  while IFS= read -r parent; do
    [[ $parent == "$repo_root_parent" || ! -d $parent ]] && continue
    registered=$(git -C "$repo" worktree list --porcelain | awk '/^worktree /{print substr($0,10)}')
    for d in "$parent"/*/; do
      d=${d%/}
      [[ -d $d ]] || continue
      [[ $(basename "$d") == node_modules ]] && continue
      if ! grep -qx "$d" <<<"$registered"; then
        log "  [ORPHAN] 未注册目录 $d（可能含 .env 等，只报告不删除）"
      fi
    done
  done < <(git -C "$repo" worktree list --porcelain | awk '/^worktree /{print substr($0,10)}' | xargs -r -n1 dirname | sort -u)

  # 远程分支同步：MR 合完后 GitLab 上的 ag-* 分支会累积。只删
  # 「tip 是 origin/main 祖先」的 ag-* 远程分支；push 失败仅告警不中断。
  while IFS= read -r rb; do
    [[ -z $rb ]] && continue
    if [[ $DRY_RUN == 1 ]]; then
      log "  [DRY-RUN] 将删除远程分支 $rb"
      continue
    fi
    if git -C "$repo" push origin --delete "${rb#origin/}" 2>&1; then
      log "  [REMOVED] 远程分支 $rb"
    else
      log "  [WARN] 远程分支 $rb 删除失败（网络/凭据？），下次再试"
    fi
  done < <(git -C "$repo" branch -r --merged origin/main \
             | awk '$1 !~ /\/(main|HEAD)$/ && $1 ~ /^origin\/ag-/ {print $1}')

  # 本地裸分支同步：已合入、未挂任何 worktree 的 ag-* 分支
  # （挂载分支带 + 前缀、当前分支带 * 前缀，均跳过——脏 worktree 的分支不受影响）。
  while IFS= read -r lb; do
    [[ -z $lb ]] && continue
    if [[ $DRY_RUN == 1 ]]; then
      log "  [DRY-RUN] 将删除本地裸分支 $lb"
      continue
    fi
    git -C "$repo" branch -D "$lb" >/dev/null 2>&1 \
      && log "  [REMOVED] 本地裸分支 $lb" \
      || log "  [WARN] 本地裸分支 $lb 删除失败，请手动处理"
  done < <(git -C "$repo" branch --merged origin/main \
             | awk '{ b = ($1 == "*" || $1 == "+") ? $2 : $1; if (b == "main" || b == "") next; if (b ~ /^ag-/) print b }')
done

log "=== 完成$([[ $DRY_RUN == 1 ]] && echo '（dry-run，未实际删除）') ==="
